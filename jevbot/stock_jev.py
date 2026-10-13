"""Jev judges the stock's setup. Code turns the judgments into a cautious label. Nothing here trades."""

from __future__ import annotations

import time
from dataclasses import dataclass


def build_questions(h: int):
    from typesafe_sdk import Choice, Noul, Score

    return {
        "direction": Choice(
            instructions=f"Most likely direction of the closing price over the next {h} trading days",
            criteria={
                "up": "Closes meaningfully higher",
                "down": "Closes meaningfully lower",
                "flat": "Roughly unchanged, within normal noise",
            },
        ),
        "uptrend": Noul(instructions="The stock is in an established uptrend"),
        "overextended": Noul(instructions="Price is stretched far above its recent average and due for a pullback"),
        "vol_regime": Choice(
            instructions="How turbulent is this stock right now compared with its own history",
            criteria={
                "calm": "Moves are smaller than usual",
                "normal": "Typical size of moves",
                "turbulent": "Moves are larger and less predictable than usual",
            },
        ),
        "entry_quality": Score(
            instructions="How favorable is this setup for opening a position now",
            criteria=["Poor", "Mixed", "Decent", "Strong"],
        ),
    }


@dataclass
class StockPolicy:
    """Thresholds live here, not in a prompt. Deliberately strict: most days should say SIT OUT."""

    min_p_direction: float = 0.60
    min_entry_score: float = 2.0
    max_overextended: float = 0.70


def parse(resp, latency_ms: float) -> dict:
    d, v = resp.choices["direction"], resp.choices["vol_regime"]
    e = resp.scores["entry_quality"]
    p = {k: float(x) for k, x in d.probabilities.items()}
    return {
        "direction": d.choice,
        "direction_conf": d.confidence,
        "p_up": p.get("up", 0.0),
        "p_down": p.get("down", 0.0),
        "p_flat": p.get("flat", 0.0),
        "uptrend": resp.nouls["uptrend"].noul,
        "overextended": resp.nouls["overextended"].noul,
        "vol_regime": v.choice,
        "vol_conf": v.confidence,
        "entry_score": e.score,
        "entry_conf": e.confidence,
        "model": resp.model,
        "latency_ms": round(latency_ms),
    }


def lean(a: dict, pol: StockPolicy | None = None) -> tuple[str, str]:
    pol = pol or StockPolicy()
    if a["vol_regime"] == "turbulent":
        return "SIT OUT", "The stock is unusually turbulent, so any call is less reliable."
    if a["p_up"] >= pol.min_p_direction and a["entry_score"] >= pol.min_entry_score:
        if a["overextended"] > pol.max_overextended:
            return "SIT OUT", "Looks up, but the price is stretched. A pullback is likely."
        return "LEAN UP", "Jev sees higher odds of a rise and the setup scores well."
    if a["p_down"] >= pol.min_p_direction:
        return "LEAN DOWN", "Jev sees higher odds of a fall. This is a reason to avoid, not a reason to short."
    return "SIT OUT", "No clear edge. Not trading is a valid answer."


async def ask(client, model: str, snap: dict, h: int) -> dict:
    t0 = time.perf_counter()
    resp = await client.system_one(state={**snap, "horizon_trading_days": h}, questions=build_questions(h), model=model)
    out = parse(resp, (time.perf_counter() - t0) * 1000)
    if out["model"] != model:
        raise RuntimeError(f"pinned {model!r} but server answered with {out['model']!r}")
    return out
