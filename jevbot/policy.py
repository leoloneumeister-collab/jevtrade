"""Policy engine: turns Jev's judgments into an action. Pure code, thresholds from config."""

from __future__ import annotations

from dataclasses import dataclass

from .config import PolicyCfg
from .jev import Decision


@dataclass(frozen=True)
class Action:
    kind: str  # QUOTE_BOTH | QUOTE_WIDE | WIDEN | PULL_QUOTES | STAND_DOWN | HOLD | KILL
    size_mult: float = 1.0
    spread_mult: float = 1.0
    pressure: int = 0  # 0..3 inventory pressure level
    tilt_bps: float = 0.0  # directional reservation shift
    reason: str = ""

    @property
    def quoting(self) -> bool:
        return self.kind in ("QUOTE_BOTH", "QUOTE_WIDE", "WIDEN")


def compose_action(d: Decision, p: PolicyCfg) -> Action:
    # Cheap mistakes get low bars (pull out), expensive ones get high bars (quote tight).
    pressure = max(0, min(3, round(d.pressure_score))) if d.pressure_conf >= 0.5 else 0
    if d.regime == "crisis" and d.regime_conf >= p.crisis_conf:
        return Action("STAND_DOWN", reason="crisis regime")
    if d.toxic > p.toxic_pull:
        return Action("PULL_QUOTES", reason=f"toxic {d.toxic:.2f}")
    if d.stressed > p.stress_widen:
        return Action(
            "WIDEN", size_mult=0.5, spread_mult=p.widen_mult, pressure=pressure, reason=f"stressed {d.stressed:.2f}"
        )

    tilt = 0.0
    if d.direction_conf >= p.direction_min_conf and d.direction != "neutral":
        tilt = p.direction_tilt_bps * (1 if d.direction == "up" else -1)

    if d.quote_score >= p.quote_min_score and d.quote_conf >= p.quote_min_conf:
        return Action("QUOTE_BOTH", pressure=pressure, tilt_bps=tilt)
    if d.quote_score >= p.wide_min_score:
        size = 0.5 if d.quote_conf < p.low_conf else 1.0
        return Action(
            "QUOTE_WIDE",
            size_mult=size,
            spread_mult=p.wide_mult,
            pressure=pressure,
            tilt_bps=tilt,
            reason="not confident enough for tight quotes",
        )
    return Action("STAND_DOWN", reason="quote environment poor")


def rules_only(snap: dict, p: PolicyCfg) -> Action:
    """Fallback when Jev is unavailable. Deliberately dumb and conservative."""
    calm = snap["vol_vs_long"] < p.rules_max_vol_ratio and snap["spread_bps"] < p.rules_max_spread_bps
    balanced = abs(snap["aggressive_buy_ratio"] - 0.5) < p.rules_flow_band
    pressure = min(3, int(abs(snap["inventory"]) // p.rules_pressure_step))
    if calm and balanced:
        return Action("QUOTE_WIDE", size_mult=0.5, spread_mult=p.wide_mult, pressure=pressure, reason="rules-only")
    return Action("STAND_DOWN", reason="rules-only: not calm and balanced")
