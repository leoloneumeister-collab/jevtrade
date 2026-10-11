"""Polymarket market data. READ ONLY: no keys, no orders, no signing. Nothing here can trade.

Schemas follow the open-source `polymarket-apis` client models (book / price_change /
last_trade_price / tick_size_change events on wss://ws-subscriptions-clob.polymarket.com/ws/market).
They have NOT yet been checked against live traffic from this project. Run
`jevbot run --feed polymarket --record runs/raw.jsonl` once and look at the file.

Design: one deterministic core (`StreamCore`) fed with raw messages and timestamps. The live
feed and the replay feed share it, so a recorded session replays to identical ticks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator

from .feed import Tick

log = logging.getLogger("jevbot.polymarket")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
HEARTBEAT_S = 10.0


def _g(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return default


def _f(x: Any) -> float:
    return float(x)


class StreamCore:
    """Local order book + trade tape for ONE outcome token."""

    def __init__(self, token_id: str, block_ms: float = 1000.0, levels: int = 3):
        self.token = token_id
        self.block_s = block_ms / 1000.0
        self.levels = levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.have_book = False
        self.tick_size: float | None = None
        self.min_order_size: float | None = None
        self.trades: list[tuple[float, float, str]] = []
        self.last_alive: float | None = None      # any message, incl. PONG
        self.block = 0
        self.t0: float | None = None
        self.desyncs = 0

    # ---- ingest --------------------------------------------------------
    def alive(self, now: float) -> None:
        self.last_alive = now
        if self.t0 is None:
            self.t0 = now

    def feed_raw(self, raw: str, now: float) -> None:
        self.alive(now)
        if raw in ("PONG", "pong", ""):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("non-json message ignored: %.60s", raw)
            return
        for ev in (data if isinstance(data, list) else [data]):
            if isinstance(ev, dict):
                self._event(ev)

    def _event(self, ev: dict) -> None:
        et = ev.get("event_type")
        if et == "book":
            if ev.get("asset_id") != self.token:
                return
            self.bids = {_f(l["price"]): _f(l["size"]) for l in ev.get("bids", []) if _f(l["size"]) > 0}
            self.asks = {_f(l["price"]): _f(l["size"]) for l in ev.get("asks", []) if _f(l["size"]) > 0}
            if ev.get("tick_size"):
                self.tick_size = _f(ev["tick_size"])
            if ev.get("min_order_size"):
                self.min_order_size = _f(ev["min_order_size"])
            self.have_book = True
        elif et == "price_change":
            for ch in _g(ev, "price_changes", "pc", default=[]):
                if _g(ch, "asset_id", "a") != self.token:
                    continue
                px, sz = _f(_g(ch, "price", "p")), _f(_g(ch, "size", "s"))
                side = _g(ch, "side", "si")
                book = self.bids if side == "BUY" else self.asks
                if sz <= 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
                self._check(ch)
        elif et == "last_trade_price":
            if ev.get("asset_id") == self.token:
                aggressor = "buy" if ev.get("side") == "BUY" else "sell"
                self.trades.append((_f(ev["price"]), _f(ev["size"]), aggressor))
        elif et == "tick_size_change":
            if ev.get("asset_id") == self.token:
                self.tick_size = _f(ev["new_tick_size"])

    def _check(self, ch: dict) -> None:
        """The venue ships its own best bid/ask in each change. Disagreement = our book drifted."""
        bb, ba = _g(ch, "best_bid", "bb"), _g(ch, "best_ask", "ba")
        if bb is None or ba is None or not self.bids or not self.asks:
            return
        if abs(max(self.bids) - _f(bb)) > 1e-9 or abs(min(self.asks) - _f(ba)) > 1e-9:
            self.desyncs += 1
            log.warning("local book disagrees with venue best bid/ask (%s/%s vs %s/%s)",
                        max(self.bids), min(self.asks), bb, ba)

    # ---- snapshot ------------------------------------------------------
    def _side(self, book: dict[float, float], reverse: bool) -> tuple[tuple[float, float], ...]:
        lv = sorted(book.items(), reverse=reverse)[: self.levels]
        step = (self.tick_size or 0.01) * (-1 if reverse else 1)
        while len(lv) < self.levels:                  # pad with empty far levels
            lv.append((round((lv[-1][0] if lv else 0.5) + step, 8), 0.0))
        return tuple(lv)

    def snapshot(self, ts_now: float) -> Tick | None:
        """One tick for the block ending now. None while there is no usable two-sided book."""
        if not (self.have_book and self.bids and self.asks) or max(self.bids) >= min(self.asks):
            return None
        t = Tick(self.block, round(ts_now - (self.t0 or ts_now), 6), self._side(self.bids, True),
                 self._side(self.asks, False), tuple(self.trades), None,
                 data_age_ms=round(((ts_now - self.last_alive) * 1000) if self.last_alive else 0.0, 1))
        self.trades = []
        self.block += 1
        return t


# ---------------------------------------------------------------------------
class ReplayFeed:
    """Replays a recorded session through the same core: deterministic, no network."""

    def __init__(self, path: str, token_id: str | None = None, block_ms: float = 1000.0, max_blocks: int | None = None):
        self.rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
        self.token = token_id or next(r["token"] for r in self.rows if r.get("token"))
        self.core = StreamCore(self.token, block_ms)
        self.max_blocks = max_blocks

    @property
    def tick_size(self) -> float | None:
        return self.core.tick_size

    async def __aiter__(self) -> AsyncIterator[Tick]:
        core, nxt, n = self.core, None, 0
        for r in self.rows:
            if r.get("kind") != "msg":
                continue
            ts = r["t"]
            if nxt is None:
                core.alive(ts)
                nxt = ts + core.block_s
            while ts >= nxt:
                tick = core.snapshot(nxt)
                nxt += core.block_s
                if tick:
                    yield tick
                    n += 1
                    if self.max_blocks and n >= self.max_blocks:
                        return
            core.feed_raw(r["raw"], ts)


class LiveFeed:
    """Live market channel. Reconnects with backoff; while down it keeps emitting the last known
    book with a growing data_age_ms, which the risk engine turns into a veto."""

    def __init__(self, token_id: str, block_ms: float = 1000.0, record: str | None = None,
                 max_blocks: int | None = None):
        self.core = StreamCore(token_id, block_ms)
        self.token, self.block_s, self.max_blocks = token_id, block_ms / 1000.0, max_blocks
        self.rec = None
        if record:
            Path(record).parent.mkdir(parents=True, exist_ok=True)
            self.rec = open(record, "w")
            self.rec.write(json.dumps({"kind": "meta", "token": token_id, "url": WS_URL, "t": time.time()}) + "\n")
        self._stop = False

    @property
    def tick_size(self) -> float | None:
        return self.core.tick_size

    async def _receiver(self) -> None:
        import websockets
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, open_timeout=10) as ws:
                    await ws.send(json.dumps({"assets_ids": [self.token], "type": "market",
                                              "custom_feature_enabled": True}))
                    log.info("subscribed to %s", self.token)
                    backoff = 1.0
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            now = time.time()
                            raw = raw.decode() if isinstance(raw, bytes) else raw
                            if self.rec:
                                self.rec.write(json.dumps({"kind": "msg", "t": now, "token": self.token, "raw": raw}) + "\n")
                            self.core.feed_raw(raw, now)
                    finally:
                        hb.cancel()
            except Exception as e:
                log.warning("market socket down: %r (retry in %.0fs)", e, backoff)
            self.core.have_book = False      # force a fresh snapshot after any reconnect
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await ws.send("PING")

    async def __aiter__(self) -> AsyncIterator[Tick]:
        rx = asyncio.create_task(self._receiver())
        last: Tick | None = None
        n = 0
        try:
            nxt = time.time() + self.block_s
            while True:
                await asyncio.sleep(max(0.0, nxt - time.time()))
                now, nxt = time.time(), nxt + self.block_s
                tick = self.core.snapshot(now)
                if tick is None and last is not None and self.core.last_alive:
                    # socket down: re-emit the last book, but say how old it is
                    tick = Tick(self.core.block, round(now - (self.core.t0 or now), 6), last.bids, last.asks, (), None,
                                data_age_ms=round((now - self.core.last_alive) * 1000, 1))
                    self.core.block += 1
                if tick:
                    last = tick
                    yield tick
                    n += 1
                    if self.max_blocks and n >= self.max_blocks:
                        return
        finally:
            self._stop = True
            rx.cancel()
            if self.rec:
                self.rec.close()


# ---------------------------------------------------------------------------
def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "jevbot/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def list_markets(limit: int = 200, min_days: float = 7.0, mid_lo: float = 0.2, mid_hi: float = 0.8) -> list[dict]:
    """Candidate markets for a quoting bot: open, order-book enabled, mid away from 0/1, not about to resolve,
    spread of at least two ticks (otherwise there is nothing to earn)."""
    from datetime import datetime, timezone
    q = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": limit,
                                "order": "volume24hr", "ascending": "false"})
    out = []
    for m in _get(f"{GAMMA_URL}/markets?{q}"):
        try:
            toks = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
            bb, ba = float(m["bestBid"]), float(m["bestAsk"])
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")) if m.get("endDate") else None
        except (KeyError, TypeError, ValueError):
            continue
        days = (end - datetime.now(timezone.utc)).total_seconds() / 86400 if end else 999
        mid = (bb + ba) / 2
        if not (m.get("enableOrderBook") and toks and mid_lo <= mid <= mid_hi and days >= min_days
                and (ba - bb) >= 2 * tick - 1e-9):
            continue
        out.append({"question": m.get("question"), "token_yes": toks[0], "mid": round(mid, 3),
                    "spread": round(ba - bb, 4), "tick": tick, "vol24h": float(m.get("volume24hr") or 0),
                    "days_left": round(days, 1)})
    return out
