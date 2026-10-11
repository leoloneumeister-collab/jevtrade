"""State engine: pure arithmetic. Everything here is a function of data known at decision time."""
from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections import deque

from .feed import Tick
from .venue import PaperVenue


def _r(x: float, sig: int = 5) -> float:
    return 0.0 if x == 0 or not math.isfinite(x) else float(f"{x:.{sig}g}")


class StateEngine:
    FLOW_WINDOW = 20

    def __init__(self, vol_halflife_blocks: int = 20000, warmup_blocks: int = 120, block_ms: float = 300):
        self.ts: deque[float] = deque()
        self.mids: deque[float] = deque()
        self.rets: deque[float] = deque()
        self.flow: deque[tuple[float, float, int]] = deque(maxlen=self.FLOW_WINDOW)  # buy, sell, n
        self.lat: deque[float] = deque(maxlen=10)
        self.alpha = 1 - 0.5 ** (1 / max(1, vol_halflife_blocks))
        self.long_var: float | None = None
        self.warmup = warmup_blocks
        self.vol_window = max(2, round(300_000 / block_ms))  # blocks in 5 minutes
        self.n = 0

    @property
    def warm(self) -> bool:
        return self.n >= self.warmup

    def record_latency(self, ms: float) -> None:
        self.lat.append(round(ms))

    def _ret_bps(self, now: float, mid: float, secs: float) -> float:
        if not self.ts or self.ts[0] > now - secs:
            return 0.0
        i = min(bisect_left(self.ts, now - secs), len(self.mids) - 1)
        return (mid / self.mids[i] - 1) * 1e4

    def update(self, tick: Tick, venue: PaperVenue) -> dict:
        bb, bs = tick.bids[0]
        ba, as_ = tick.asks[0]
        mid = (bb + ba) / 2
        micro = (bb * as_ + ba * bs) / (bs + as_)
        bid3 = sum(s for _, s in tick.bids[:3])
        ask3 = sum(s for _, s in tick.asks[:3])

        if self.mids:
            r = math.log(mid / self.mids[-1])
            self.rets.append(r)
            self.long_var = r * r if self.long_var is None else (1 - self.alpha) * self.long_var + self.alpha * r * r
        self.ts.append(tick.ts)
        self.mids.append(mid)
        horizon = tick.ts - 1800 - 1
        while self.ts and self.ts[0] < horizon:
            self.ts.popleft()
            self.mids.popleft()
            if self.rets:
                self.rets.popleft()
        self.n += 1

        buys = sum(s for _, s, side in tick.trades if side == "buy")
        sells = sum(s for _, s, side in tick.trades if side == "sell")
        self.flow.append((buys, sells, len(tick.trades)))
        wb = sum(f[0] for f in self.flow)
        ws = sum(f[1] for f in self.flow)
        recent = list(self.rets)[-self.vol_window:]
        vol5 = math.sqrt(sum(x * x for x in recent) / len(recent)) * 1e4 if recent else 0.0
        long_vol = math.sqrt(self.long_var) * 1e4 if self.long_var else 0.0

        snap = {
            "mid": _r(mid),
            "micro_gap_bps": _r((micro / mid - 1) * 1e4),
            "spread_bps": _r((ba - bb) / mid * 1e4),
            "depth_bid3": _r(bid3, 4),
            "depth_ask3": _r(ask3, 4),
            "imbalance": _r(bid3 / (bid3 + ask3), 3),
            "ret_1m_bps": _r(self._ret_bps(tick.ts, mid, 60), 3),
            "ret_5m_bps": _r(self._ret_bps(tick.ts, mid, 300), 3),
            "ret_30m_bps": _r(self._ret_bps(tick.ts, mid, 1800), 3),
            "buy_vol": _r(buys, 4),
            "sell_vol": _r(sells, 4),
            "aggressive_buy_ratio": _r(wb / (wb + ws), 3) if wb + ws else 0.5,
            "trades_per_block": _r(sum(f[2] for f in self.flow) / len(self.flow), 3),
            "vol_5m_bps": _r(vol5, 3),
            "vol_vs_long": _r(vol5 / long_vol, 3) if long_vol else 1.0,
            "ref_gap_bps": _r((mid / tick.ref_mid - 1) * 1e4, 3) if tick.ref_mid else 0.0,
            "inventory": _r(venue.inv, 4),
            "upnl": _r(venue.upnl, 4),
            "drawdown": _r(venue.drawdown, 4),
            "hold_s": _r(venue.hold_seconds(), 4),
            "fill_ratio": _r(venue.fill_ratio(), 3),
            "rejects": venue.rejects,
            "lat_ms": list(self.lat),
        }
        return snap

    @staticmethod
    def approx_tokens(snap: dict) -> int:
        """Crude upper-ish estimate: numeric JSON tokenises at roughly 3 chars per token."""
        return math.ceil(len(json.dumps(snap, separators=(",", ":"))) / 3)
