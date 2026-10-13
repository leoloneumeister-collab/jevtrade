"""Jev decision layer: one battery of atomic questions, one call, typed answers.

Jev never sees an instruction to trade. It only judges the state. Thresholds live in policy.py.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

QUESTION_SPEC = {
    "regime": (
        "choice",
        "What regime is the market in right now",
        {
            "trending": "Persistent move in one direction",
            "mean_reverting": "Oscillating around a stable level",
            "high_vol": "Large moves both ways",
            "crisis": "Disorderly, liquidity vanishing",
        },
    ),
    "direction": (
        "choice",
        "Most likely price bias over the next 10 blocks",
        {"up": "Higher", "down": "Lower", "neutral": "No clear bias"},
    ),
    "toxic_flow": ("noul", "Aggressive flow is likely informed traders, not noise", None),
    "liquidity_stressed": ("noul", "The order book is thinner than its normal level", None),
    "quote_environment": (
        "score",
        "How favorable is this state for providing liquidity",
        ["No", "Marginal", "Standard", "Excellent"],
    ),
    "inventory_pressure": (
        "score",
        "How urgently inventory should be reduced",
        ["None", "Mild", "Skew hard", "Reduce now"],
    ),
}


@dataclass(frozen=True)
class Decision:
    regime: str
    regime_conf: float
    direction: str
    direction_conf: float
    toxic: float  # P(informed flow); Noul has no confidence field, only a probability
    stressed: float  # P(thin book)
    quote_score: float
    quote_conf: float
    pressure_score: float
    pressure_conf: float
    model: str
    latency_ms: float
    source: str = "jev"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Decider(Protocol):
    async def decide(self, snap: dict) -> Decision: ...


def build_questions():
    from typesafe_sdk import Choice, Noul, Score

    out: dict[str, Any] = {}
    for name, (kind, text, crit) in QUESTION_SPEC.items():
        if kind == "choice":
            out[name] = Choice(instructions=text, criteria=crit)  # type: ignore[arg-type]
        elif kind == "noul":
            out[name] = Noul(instructions=text)
        else:
            out[name] = Score(instructions=text, criteria=crit)  # type: ignore[arg-type]
    return out


def decision_from_response(resp: Any, latency_ms: float) -> Decision:
    c, n, s = resp.choices, resp.nouls, resp.scores
    return Decision(
        regime=c["regime"].choice,
        regime_conf=c["regime"].confidence,
        direction=c["direction"].choice,
        direction_conf=c["direction"].confidence,
        toxic=n["toxic_flow"].noul,
        stressed=n["liquidity_stressed"].noul,
        quote_score=s["quote_environment"].score,
        quote_conf=s["quote_environment"].confidence,
        pressure_score=s["inventory_pressure"].score,
        pressure_conf=s["inventory_pressure"].confidence,
        model=resp.model,
        latency_ms=latency_ms,
    )


class ModelDrift(Exception):
    """Server answered with a different model than the pinned one."""


class JevDecider:
    """Direct TypeSafe API. No gateway hop, no retries (a retry blows the block deadline)."""

    def __init__(self, model: str, deadline_ms: float):
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        self.model = model
        self.client = AsyncTypeSafeClient(model=model, retry=RetryPolicy(max_retries=0), timeout=deadline_ms / 1000)
        self.questions = build_questions()

    async def decide(self, snap: dict) -> Decision:
        t0 = time.perf_counter()
        resp = await self.client.system_one(state=snap, questions=self.questions, model=self.model)
        dec = decision_from_response(resp, (time.perf_counter() - t0) * 1000)
        if dec.model != self.model:
            raise ModelDrift(f"pinned {self.model!r} but server answered with {dec.model!r}")
        return dec

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close:
            await close()


class HeuristicDecider:
    """OFFLINE TEST DOUBLE, not Jev. Lets the whole pipeline run without an API key.

    Do not read anything about Jev's quality from results produced with this class.
    """

    def __init__(self, noise: float = 0.0, delay_ms: float = 0.0):
        self.noise, self.delay_ms = noise, delay_ms

    async def decide(self, snap: dict) -> Decision:
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        flow = abs(snap["aggressive_buy_ratio"] - 0.5) * 2
        vr = snap["vol_vs_long"]
        toxic = min(1.0, flow * 1.4 * (1 if abs(snap["ret_1m_bps"]) > 2 else 0.6))
        stressed = min(1.0, max(0.0, (snap["spread_bps"] - 4) / 6) + max(0.0, (vr - 1.5) / 3))
        regime = (
            "crisis"
            if vr > 3.5
            else "high_vol"
            if vr > 1.8
            else "trending"
            if abs(snap["ret_5m_bps"]) > 6
            else "mean_reverting"
        )
        q = 3 - 2.2 * toxic - 1.5 * stressed - (0.8 if regime == "high_vol" else 0)
        pressure = min(3.0, abs(snap["inventory"]) / 12)
        up = snap["ret_1m_bps"]
        return Decision(
            regime=regime,
            regime_conf=0.7,
            direction="up" if up > 3 else "down" if up < -3 else "neutral",
            direction_conf=0.6,
            toxic=toxic,
            stressed=stressed,
            quote_score=max(0.0, q),
            quote_conf=0.85 if abs(q - 1.5) > 0.5 else 0.5,
            pressure_score=pressure,
            pressure_conf=0.8,
            model="heuristic-double",
            latency_ms=self.delay_ms,
            source="double",
        )
