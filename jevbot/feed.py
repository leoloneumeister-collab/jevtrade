"""Market data. `MarketFeed` is the seam where your real venue plugs in.

SyntheticFeed is a deterministic simulator for plumbing tests and fee math. Profits on it
prove nothing about an edge. Replace it with a real feed before judging anything.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Tick:
    block: int
    ts: float  # seconds, event time
    bids: tuple[tuple[float, float], ...]  # (price, size), best first
    asks: tuple[tuple[float, float], ...]
    trades: tuple[tuple[float, float, str], ...]  # (price, size, aggressor side "buy"|"sell")
    ref_mid: float | None = None
    data_age_ms: float = 0.0  # time since the venue last proved it is alive


class MarketFeed(Protocol):
    def __aiter__(self) -> AsyncIterator[Tick]: ...


class SyntheticFeed:
    REGIMES = ("calm", "trending", "chaotic", "informed")

    def __init__(
        self,
        seed: int = 7,
        blocks: int = 20000,
        block_ms: float = 300,
        tick_size: float = 0.0001,
        start: float = 3.4127,
        realtime: bool = False,
    ):
        self.rng = random.Random(seed)
        self.blocks, self.block_ms, self.tick = blocks, block_ms, tick_size
        self.start, self.realtime = start, realtime

    def _q(self, x: float) -> float:
        return round(round(x / self.tick) * self.tick, 8)

    async def _gen(self) -> AsyncIterator[Tick]:
        r = self.rng
        fair, regime, left, drift = self.start, "calm", 0, 0.0
        for b in range(self.blocks):
            if left <= 0:
                regime = r.choices(self.REGIMES, weights=(5, 2, 1.5, 1.5))[0]
                left = r.randint(150, 700)
                drift = r.choice((-1, 1)) * {"calm": 0, "trending": 0.00025, "chaotic": 0, "informed": 0.0004}[regime]
            left -= 1
            vol = {"calm": 0.00025, "trending": 0.0003, "chaotic": 0.0010, "informed": 0.0003}[regime]
            fair *= math.exp(drift + r.gauss(0, vol))
            half = max(self.tick, self._q(fair * (0.00018 + 2.2 * vol * (regime == "chaotic"))))
            bid, ask = self._q(fair - half), self._q(fair + half)
            thin = 0.35 if regime == "chaotic" else 1.0
            bids = tuple((self._q(bid - i * self.tick), round(r.uniform(20, 120) * thin, 1)) for i in range(3))
            asks = tuple((self._q(ask + i * self.tick), round(r.uniform(20, 120) * thin, 1)) for i in range(3))
            p_buy = {"calm": 0.5, "trending": 0.58, "chaotic": 0.5, "informed": 0.5 + (0.28 if drift > 0 else -0.28)}[
                regime
            ]
            trades = []
            for _ in range(r.choices((0, 1, 2, 3, 4), weights=(2, 3, 3, 2, 1))[0]):
                side = "buy" if r.random() < p_buy else "sell"
                size = round(r.expovariate(1 / 8), 1) + 0.5
                walk = self.tick * int(size // 25)
                px = (ask + walk) if side == "buy" else (bid - walk)
                trades.append((self._q(px), size, side))
            ref = fair * (1 + r.gauss(0, 0.00005))
            yield Tick(b, b * self.block_ms / 1000, bids, asks, tuple(trades), ref)

    async def __aiter__(self) -> AsyncIterator[Tick]:
        async for t in self._gen():
            if self.realtime:
                await asyncio.sleep(self.block_ms / 1000)
            yield t
