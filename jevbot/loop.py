"""The block loop and the fallback ladder.

Per block: match old orders -> snapshot -> Jev battery (hard deadline) -> policy -> pricing
-> size clip -> risk veto -> cancel/replace -> journal.

Ladder:
  Jev ok, confident        normal operation
  Jev ok, low confidence   smaller / wider (policy)
  Jev late or one-off fail hold, place nothing new
  Jev down (breaker open)  code-only rules
  hard limit breached      kill: cancel all, flatten, write KILL file, alert
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .config import Config
from .feed import MarketFeed, Tick
from .jev import Decider, Decision
from .journal import Journal
from .policy import Action, compose_action, rules_only
from .pricing import make_quotes
from .risk import RiskEngine
from .state import StateEngine
from .venue import PaperVenue, VenueError

log = logging.getLogger("jevbot")


class Bot:
    def __init__(self, cfg: Config, feed: MarketFeed, venue: PaperVenue, decider: Decider | None,
                 journal: Journal | None = None, mode: str = "jev"):
        self.cfg, self.feed, self.venue, self.decider = cfg, feed, venue, decider
        self.journal = journal or Journal(None)
        self.mode = mode                       # "jev" | "rules"
        self.engine = StateEngine(cfg.run.vol_long_halflife_blocks, cfg.run.warmup_blocks, cfg.jev.block_ms)
        self.risk = RiskEngine(cfg.risk)
        self.jev_fails = 0
        self.venue_errors = 0
        self.killed = False
        self.kill_reasons: list[str] = []
        self.counts: dict[str, int] = {}
        self.alerts: list[str] = []

    # ------------------------------------------------------------------
    def alert(self, msg: str) -> None:
        self.alerts.append(msg)
        log.critical(msg)

    def _kill(self, reasons: list[str]) -> None:
        self.killed = True
        self.kill_reasons = reasons
        self.venue.flatten()
        Path(self.cfg.run.kill_file).write_text("; ".join(reasons))
        self.alert("KILL SWITCH: " + "; ".join(reasons))

    async def _decide(self, snap: dict, block: int) -> tuple[Action, Decision | None, str]:
        """Returns (action, decision, status). Status feeds the journal and the ladder."""
        j = self.cfg.jev
        if self.mode == "rules" or self.decider is None:
            return rules_only(snap, self.cfg.policy), None, "rules"
        breaker_open = self.jev_fails >= j.breaker_after
        if breaker_open and block % j.retry_every != 0:
            return rules_only(snap, self.cfg.policy), None, "rules_breaker"
        t0 = time.perf_counter()
        try:
            dec = await asyncio.wait_for(self.decider.decide(snap), timeout=j.deadline_ms / 1000)
        except asyncio.TimeoutError:
            self.jev_fails += 1
            self.engine.record_latency((time.perf_counter() - t0) * 1000)
            return Action("HOLD", reason="Jev late"), None, "late"
        except Exception as e:  # network, auth, schema, model drift: all treated as "Jev unavailable"
            self.jev_fails += 1
            log.warning("Jev failure %d: %r", self.jev_fails, e)
            if self.jev_fails >= j.breaker_after:
                return rules_only(snap, self.cfg.policy), None, "rules_breaker"
            return Action("HOLD", reason="Jev error"), None, "error"
        self.jev_fails = 0
        self.engine.record_latency((time.perf_counter() - t0) * 1000)
        return compose_action(dec, self.cfg.policy), dec, "ok"

    def _sync_quotes(self, tick: Tick, snap: dict, action: Action) -> list[dict]:
        """Cancel/replace only when it is worth the fee. Every order goes past the risk engine."""
        v, rc = self.venue, self.cfg.run
        placed: list[dict] = []
        quotes = make_quotes(snap, action, tick.bids[0][0], tick.asks[0][0], self.cfg.pricing)
        want = {q.side: q for q in quotes}
        for o in list(v.orders.values()):
            q = want.get(o.side)
            keep = q and abs(q.px - o.px) / snap["mid"] * 1e4 < rc.replace_bps and o.size >= q.size * 0.99
            if keep:
                want.pop(o.side)
            else:
                v.cancel(o.oid)
        for side, q in want.items():
            size = min(q.size, self.risk.room(side, v))
            if size <= 0:
                continue
            q = type(q)(q.side, q.px, round(size, 4))
            why = self.risk.veto_order(q, v)
            if why:
                self.counts["veto"] = self.counts.get("veto", 0) + 1
                placed.append({"side": side, "veto": why})
                continue
            o = v.place(side, q.px, q.size, tick.block, tick)
            placed.append({"side": side, "px": q.px, "size": q.size, "ok": o is not None})
        return placed

    async def step(self, tick: Tick, wall_data_age_ms: float = 0.0) -> None:
        v, cfg = self.venue, self.cfg
        fills = v.process_tick(tick)
        v.expire(tick.block, cfg.run.order_ttl_blocks)
        snap = self.engine.update(tick, v)

        verdict = self.risk.check_global(v, wall_data_age_ms, self.venue_errors)
        if verdict.kill:
            self._kill(verdict.reasons)
            self._log(tick, snap, Action("KILL", reason="; ".join(verdict.reasons)), None, "kill", [], fills)
            return

        if verdict.flatten:
            v.flatten()
            self.counts["flatten"] = self.counts.get("flatten", 0) + 1
            log.warning("flattened: %s", "; ".join(verdict.reasons))

        action, dec, status = await self._decide(snap, tick.block)
        if not self.engine.warm:
            action, status = Action("STAND_DOWN", reason="warming up"), status if dec else "warmup"
        decision_ms = self.engine.lat[-1] if (self.engine.lat and dec) else None
        late = self.risk.check_global(v, wall_data_age_ms, self.venue_errors, decision_ms)
        if late.veto_all:
            action = Action("HOLD", reason="; ".join(late.reasons))

        placed: list[dict] = []
        calls0 = v.calls
        try:
            if action.kind in ("PULL_QUOTES", "STAND_DOWN"):
                v.cancel_all()
            elif action.quoting:
                placed = self._sync_quotes(tick, snap, action)
            if v.calls > calls0:      # only a block that really talked to the venue proves it is healthy
                self.venue_errors = 0
        except VenueError as e:
            self.venue_errors += 1
            log.warning("venue error %d: %s", self.venue_errors, e)

        self.counts[action.kind] = self.counts.get(action.kind, 0) + 1
        self._log(tick, snap, action, dec, status, placed, fills)

    def _log(self, tick: Tick, snap: dict, action: Action, dec: Decision | None, status: str,
             placed: list[dict], fills: list) -> None:
        v = self.venue
        self.journal.write({
            "block": tick.block, "ts": tick.ts, "mode": self.mode, "status": status,
            "snap": snap, "decision": dec.to_dict() if dec else None, "model": dec.model if dec else None,
            "action": {"kind": action.kind, "reason": action.reason, "spread_mult": action.spread_mult,
                       "size_mult": action.size_mult},
            "orders": placed, "fills": [(f.side, f.px, f.size) for f in fills],
            "acct": {"equity": round(v.equity, 4), "inv": round(v.inv, 4), "fees": round(v.fees, 4)},
        })

    async def run(self) -> dict:
        kf = Path(self.cfg.run.kill_file)
        if kf.exists():
            raise SystemExit(f"{kf} exists ({kf.read_text()!r}). Investigate, then delete it to restart.")
        try:
            async for tick in self.feed:
                if self.venue.day_start_equity == 0.0 and tick.block == 0:
                    self.venue.mid = (tick.bids[0][0] + tick.asks[0][0]) / 2
                    self.venue.day_start_equity = self.venue.hwm = self.venue.equity
                await self.step(tick)
                if self.killed:
                    break
        finally:
            self.journal.close()
            if self.decider is not None and hasattr(self.decider, "aclose"):
                await self.decider.aclose()
        return self.summary()

    def summary(self) -> dict:
        v = self.venue
        gross = v.equity + v.fees
        return {
            "mode": self.mode, "killed": self.killed, "kill_reasons": self.kill_reasons,
            "net_pnl": round(v.equity - v.day_start_equity, 4), "fees": round(v.fees, 4),
            "gross_pnl": round(gross - v.day_start_equity, 4), "fills": len(v.fills),
            "fee_per_fill": round(v.fees / len(v.fills), 5) if v.fills else None,
            "orders_sent": v.sent, "fill_ratio": round(v.fill_ratio(), 3), "rejects": v.rejects,
            "max_drawdown": round(v.max_dd, 4), "end_inventory": round(v.inv, 3),
            "actions": dict(sorted(self.counts.items())),
        }
