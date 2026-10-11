"""Risk engine. Reads the venue's own ledger and the clock, never the model's opinion.

Two outcomes: a veto on a single order, or a kill (cancel everything, flatten, stop).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import RiskCfg
from .pricing import Quote
from .venue import PaperVenue


@dataclass
class Verdict:
    kill: bool = False
    flatten: bool = False
    veto_all: bool = False
    reasons: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg

    def check_global(self, v: PaperVenue, data_age_ms: float, venue_errors: int,
                     decision_ms: float | None = None) -> Verdict:
        c, out = self.cfg, Verdict()
        # kill-level breaches
        if v.drawdown > c.max_drawdown:
            out.kill = True; out.reasons.append(f"drawdown {v.drawdown:.2f} > {c.max_drawdown}")
        if -v.daily_pnl > c.max_daily_loss:
            out.kill = True; out.reasons.append(f"daily loss {-v.daily_pnl:.2f} > {c.max_daily_loss}")
        if venue_errors >= c.max_venue_errors:
            out.kill = True; out.reasons.append(f"{venue_errors} venue errors in a row")
        if abs(v.inv) > c.max_position * 1.5:
            out.kill = True; out.reasons.append(f"position {v.inv:.1f} far beyond limit")
        if v.hold_seconds() > c.max_hold_seconds:   # unwind and carry on, not a reason to stop
            out.flatten = True; out.reasons.append(f"held inventory {v.hold_seconds():.0f}s")
        if v.equity > 0 and abs(v.inv) * v.mid / v.equity > c.max_leverage * 1.5:
            out.kill = True; out.reasons.append("leverage far beyond limit")
        # veto-level conditions: no new orders this block
        if data_age_ms > c.max_data_age_ms:
            out.veto_all = True; out.reasons.append(f"stale data {data_age_ms:.0f}ms")
        if decision_ms is not None and decision_ms > c.max_decision_ms:
            out.veto_all = True; out.reasons.append(f"slow decision {decision_ms:.0f}ms")
        if v.equity > 0 and abs(v.inv) * v.mid / v.equity > c.max_leverage:
            out.veto_all = True; out.reasons.append("leverage limit")
        return out

    def veto_order(self, q: Quote, v: PaperVenue) -> str | None:
        """Return a reason string if this order must not be sent."""
        c = self.cfg
        if q.size > c.max_order_size:
            return f"order size {q.size} > {c.max_order_size}"
        worst = v.inv + (v.open_size("buy") + q.size if q.side == "buy" else -(v.open_size("sell") + q.size))
        if abs(worst) > c.max_position and abs(worst) > abs(v.inv):
            return f"worst-case position {worst:.1f} > {c.max_position}"
        return None

    def room(self, side: str, v: PaperVenue) -> float:
        """How much more we may add on this side given inventory and open orders."""
        if side == "buy":
            return max(0.0, self.cfg.max_position - v.inv - v.open_size("buy"))
        return max(0.0, self.cfg.max_position + v.inv - v.open_size("sell"))
