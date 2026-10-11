"""Paper venue and ledger. There is deliberately no real-money venue in this project.

The ledger here is the source of truth for the risk engine, so limits never depend on
anything the model or the policy says.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import PaperCfg
from .feed import Tick


class VenueError(Exception):
    pass


@dataclass
class Order:
    oid: int
    side: str   # "buy" | "sell"
    px: float
    size: float
    block: int


@dataclass
class Fill:
    oid: int
    side: str
    px: float
    size: float
    block: int


class PaperVenue:
    def __init__(self, cfg: PaperCfg):
        self.cfg = cfg
        self.cash = 0.0
        self.inv = 0.0
        self.avg_entry = 0.0
        self.fees = 0.0
        self.orders: dict[int, Order] = {}
        self._oid = 0
        self.mid = 0.0
        self.last_ts = 0.0
        self.hwm = 0.0
        self.max_dd = 0.0
        self.day_start_equity = 0.0
        self.sent = self.filled_orders = self.rejects = 0
        self.fills: list[Fill] = []
        self.slip_bps_sum = 0.0
        self.inv_since: float | None = None
        self._fail = 0
        self.calls = 0              # successful place/cancel calls

    # ---- ledger views -------------------------------------------------
    @property
    def equity(self) -> float:
        return self.cash + self.inv * self.mid

    @property
    def drawdown(self) -> float:
        return max(0.0, self.hwm - self.equity)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def upnl(self) -> float:
        return self.inv * (self.mid - self.avg_entry) if self.inv else 0.0

    def open_size(self, side: str) -> float:
        return sum(o.size for o in self.orders.values() if o.side == side)

    def hold_seconds(self) -> float:
        return 0.0 if self.inv_since is None else self.last_ts - self.inv_since

    # ---- fault injection for tests -----------------------------------
    def fail_next(self, n: int = 1) -> None:
        self._fail = n

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self._fail > 0:
            self._fail -= 1
            raise VenueError("injected venue failure")

    # ---- orders -------------------------------------------------------
    def place(self, side: str, px: float, size: float, block: int, tick: Tick) -> Order | None:
        self._maybe_fail()
        self.sent += 1
        self.cash -= self.cfg.order_cost
        self.fees += self.cfg.order_cost
        best_bid, best_ask = tick.bids[0][0], tick.asks[0][0]
        crosses = (side == "buy" and px >= best_ask) or (side == "sell" and px <= best_bid)
        if crosses or size <= 0:       # post-only: reject instead of taking
            self.rejects += 1
            return None
        self._oid += 1
        o = Order(self._oid, side, px, size, block)
        self.orders[o.oid] = o
        return o

    def cancel(self, oid: int) -> None:
        self._maybe_fail()
        if self.orders.pop(oid, None) is not None:
            self.cash -= self.cfg.order_cost
            self.fees += self.cfg.order_cost

    def cancel_all(self) -> None:
        for oid in list(self.orders):
            self.cancel(oid)

    # ---- market events ------------------------------------------------
    def process_tick(self, tick: Tick) -> list[Fill]:
        """Match trades printed in this block against orders resting from earlier blocks."""
        out: list[Fill] = []
        for t_px, t_size, side in tick.trades:
            avail = t_size * self.cfg.queue_factor
            for o in sorted(self.orders.values(), key=lambda o: o.oid):
                if avail <= 0:
                    break
                hit = (side == "sell" and o.side == "buy" and o.px >= t_px) or \
                      (side == "buy" and o.side == "sell" and o.px <= t_px)
                if not hit or o.block >= tick.block:
                    continue
                q = min(o.size, avail)
                avail -= q
                o.size -= q
                out.append(self._apply(o, q, o.px, tick.block))
                if o.size <= 1e-9:
                    self.orders.pop(o.oid, None)
                    self.filled_orders += 1
        self.mid = (tick.bids[0][0] + tick.asks[0][0]) / 2
        self.last_ts = tick.ts
        if self.hwm == 0.0 and self.day_start_equity == 0.0:
            self.hwm = self.equity
        self.hwm = max(self.hwm, self.equity)
        self.max_dd = max(self.max_dd, self.drawdown)
        return out

    def expire(self, block: int, ttl: int) -> None:
        for o in list(self.orders.values()):
            if block - o.block >= ttl:
                self.cancel(o.oid)

    def _apply(self, o: Order, q: float, px: float, block: int) -> Fill:
        signed = q if o.side == "buy" else -q
        fee = abs(q * px) * self.cfg.maker_fee_bps / 1e4
        self.fees += fee
        self.cash += -signed * px - fee
        self._update_entry(signed, px)
        f = Fill(o.oid, o.side, px, q, block)
        self.fills.append(f)
        return f

    def _update_entry(self, signed: float, px: float) -> None:
        new = self.inv + signed
        if self.inv == 0 or (self.inv > 0) == (signed > 0):
            tot = abs(self.inv) + abs(signed)
            self.avg_entry = (self.avg_entry * abs(self.inv) + px * abs(signed)) / tot if tot else px
        elif abs(signed) > abs(self.inv):
            self.avg_entry = px
        self.inv = 0.0 if abs(new) < 1e-9 else new
        if self.inv == 0:
            self.inv_since = None
        elif self.inv_since is None:
            self.inv_since = self.last_ts

    def flatten(self) -> None:
        """Kill path: cancel everything and exit at mid with adverse slippage."""
        self.orders.clear()
        if abs(self.inv) < 1e-9:
            return
        slip = self.cfg.exit_slippage_bps / 1e4
        px = self.mid * (1 - slip) if self.inv > 0 else self.mid * (1 + slip)
        fee = abs(self.inv * px) * self.cfg.maker_fee_bps / 1e4
        self.cash += self.inv * px - fee
        self.fees += fee
        self.slip_bps_sum += self.cfg.exit_slippage_bps
        self.inv, self.avg_entry, self.inv_since = 0.0, 0.0, None

    def fill_ratio(self) -> float:
        return self.filled_orders / self.sent if self.sent else 0.0
