import asyncio
import json
import random

import pytest

from jevbot.config import load_config
from jevbot.loop import Bot
from jevbot.polymarket import ReplayFeed, StreamCore
from jevbot.venue import PaperVenue

TOK = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
OTHER = "99999999999999999999"
MKT = "0x" + "ab" * 32


def book(bids, asks, tok=TOK, tick="0.01"):
    return {"market": MKT, "asset_id": tok, "timestamp": "1757908892351", "hash": "0xabc", "event_type": "book",
            "bids": [{"price": p, "size": s} for p, s in bids], "asks": [{"price": p, "size": s} for p, s in asks],
            "tick_size": tick, "last_trade_price": "0.50", "min_order_size": "5"}


def pchange(changes, short=False):
    def one(side, px, sz, bb, ba, tok=TOK):
        if short:
            return {"a": tok, "p": px, "s": sz, "si": side, "h": "0x1", "bb": bb, "ba": ba}
        return {"asset_id": tok, "price": px, "size": sz, "side": side, "hash": "0x1", "best_bid": bb, "best_ask": ba}
    key = "pc" if short else "price_changes"
    return {"market": MKT, key: [one(*c) for c in changes], "timestamp": "1757908892400", "event_type": "price_change"} \
        if not short else {"m": MKT, key: [one(*c) for c in changes], "t": "1757908892400", "event_type": "price_change"}


def trade(side, px, sz, tok=TOK):
    return {"market": MKT, "asset_id": tok, "price": px, "size": sz, "side": side, "fee_rate_bps": "0",
            "timestamp": "1757908892500", "event_type": "last_trade_price"}


def core_with_book():
    c = StreamCore(TOK, 1000)
    c.feed_raw(json.dumps(book([("0.48", "30"), ("0.47", "50"), ("0.46", "10")],
                               [("0.52", "25"), ("0.53", "40")])), 100.0)
    return c


# ---- fixtures must match the real client library's models ----------------
def test_fixtures_validate_against_polymarket_apis_models():
    ws = pytest.importorskip("polymarket_apis.types.websockets_types")
    ws.OrderBookSummaryEvent.model_validate(book([("0.48", "30")], [("0.52", "25")]))
    ws.PriceChangeEvent.model_validate(pchange([("BUY", "0.49", "12", "0.49", "0.52")]))
    ws.PriceChangeEvent.model_validate(pchange([("SELL", "0.52", "0", "0.48", "0.53")], short=True))
    ws.LastTradePriceEvent.model_validate(trade("BUY", "0.52", "7"))


# ---- book building --------------------------------------------------------
def test_snapshot_levels_padding_and_tick():
    c = core_with_book()
    t = c.snapshot(101.0)
    assert t.bids[0] == (0.48, 30.0) and t.asks[0] == (0.52, 25.0)
    assert len(t.bids) == 3 and len(t.asks) == 3
    assert t.asks[2][1] == 0.0 and t.asks[2][0] > t.asks[1][0]          # padded far level, empty
    assert c.tick_size == 0.01 and c.min_order_size == 5.0


def test_price_change_updates_and_removes_levels():
    c = core_with_book()
    c.feed_raw(json.dumps(pchange([("BUY", "0.49", "12", "0.49", "0.52")])), 101)
    assert c.bids[0.49] == 12 and c.desyncs == 0
    c.feed_raw(json.dumps(pchange([("BUY", "0.49", "0", "0.48", "0.52")], short=True)), 102)   # size 0 removes
    assert 0.49 not in c.bids and c.desyncs == 0
    c.feed_raw(json.dumps(pchange([("SELL", "0.52", "40", "0.48", "0.52")])), 103)
    assert c.asks[0.52] == 40


def test_desync_is_detected_when_venue_best_differs():
    c = core_with_book()
    c.feed_raw(json.dumps(pchange([("BUY", "0.40", "5", "0.50", "0.52")])), 101)   # venue says best bid 0.50
    assert c.desyncs == 1


def test_other_tokens_and_garbage_are_ignored():
    c = core_with_book()
    c.feed_raw(json.dumps(book([("0.10", "1")], [("0.90", "1")], tok=OTHER)), 101)
    c.feed_raw(json.dumps(trade("BUY", "0.52", "9", tok=OTHER)), 101)
    c.feed_raw("PONG", 102)
    c.feed_raw("not json {", 103)
    c.feed_raw(json.dumps([trade("BUY", "0.52", "3")]), 104)           # array framing
    t = c.snapshot(105)
    assert t.bids[0][0] == 0.48 and t.trades == ((0.52, 3.0, "buy"),)


def test_trade_side_mapping_and_tape_cleared_each_block():
    c = core_with_book()
    c.feed_raw(json.dumps(trade("SELL", "0.48", "4")), 101)
    t1 = c.snapshot(101.5)
    assert t1.trades == ((0.48, 4.0, "sell"),)
    assert c.snapshot(102.5).trades == ()


def test_crossed_or_one_sided_book_gives_no_tick():
    c = StreamCore(TOK)
    assert c.snapshot(1.0) is None                                      # nothing yet
    c.feed_raw(json.dumps(book([("0.60", "5")], [("0.55", "5")])), 1)
    assert c.snapshot(2.0) is None                                      # crossed
    c.feed_raw(json.dumps(book([("0.50", "5")], [])), 3)
    assert c.snapshot(4.0) is None                                      # one-sided


def test_data_age_counts_from_last_message_including_pong():
    c = core_with_book()
    c.feed_raw("PONG", 103.0)
    assert c.snapshot(105.0).data_age_ms == pytest.approx(2000.0)


# ---- replay determinism + end to end -------------------------------------
def make_session(path, seconds=900, seed=4):
    rnd, rows, t = random.Random(seed), [], 1000.0
    rows.append({"kind": "meta", "token": TOK, "t": t})
    bid, ask = 0.48, 0.52
    bids = {0.48: 40.0, 0.47: 60.0, 0.46: 80.0}
    asks = {0.52: 40.0, 0.53: 60.0, 0.54: 80.0}
    rows.append({"kind": "msg", "t": t, "token": TOK, "raw": json.dumps(
        book([(f"{p:.2f}", str(s)) for p, s in bids.items()], [(f"{p:.2f}", str(s)) for p, s in asks.items()]))})
    for i in range(seconds * 2):
        t += 0.5
        raw = None
        r = rnd.random()
        if r < 0.5:
            side, px = rnd.choice([("BUY", 0.52), ("SELL", 0.48)])
            raw = trade(side, f"{px:.2f}", f"{rnd.uniform(1, 15):.1f}")
        elif r < 0.8:
            p = round(rnd.choice([0.48, 0.47, 0.49]), 2)
            size = round(rnd.uniform(5, 90))
            bids[p] = float(size)
            best_bid = max(q for q, s in bids.items() if s > 0)
            raw = pchange([("BUY", f"{p:.2f}", str(size), f"{best_bid:.2f}", "0.52")])
        if raw:
            rows.append({"kind": "msg", "t": t, "token": TOK, "raw": json.dumps(raw)})
    path.write_text("\n".join(json.dumps(r) for r in rows))


async def collect(feed):
    return [t async for t in feed]


def test_replay_is_deterministic_and_blocks_are_regular(tmp_path):
    p = tmp_path / "s.jsonl"
    make_session(p)
    a = asyncio.run(collect(ReplayFeed(str(p))))
    b = asyncio.run(collect(ReplayFeed(str(p))))
    assert a == b and len(a) > 800
    assert all(abs((y.ts - x.ts) - 1.0) < 1e-6 for x, y in zip(a, a[1:]))
    assert [t.block for t in a] == list(range(len(a)))


def test_bot_runs_end_to_end_on_replayed_polymarket_data(tmp_path):
    p = tmp_path / "s.jsonl"
    make_session(p)
    cfg = load_config("config.polymarket.toml")
    cfg.run.kill_file = str(tmp_path / "KILL")
    cfg.run.warmup_blocks = 20
    bot = Bot(cfg, ReplayFeed(str(p)), PaperVenue(cfg.paper), None, mode="rules")
    s = asyncio.run(bot.run())
    assert bot.feed.core.desyncs == 0                                  # local book matches the venue's own best bid/ask
    assert not s["killed"] and cfg.pricing.tick_size == 0.01
    assert s["orders_sent"] > 0 and abs(bot.venue.inv) <= cfg.risk.max_position * 1.5


def test_polymarket_config_loads_and_pins_model():
    cfg = load_config("config.polymarket.toml")
    assert cfg.jev.block_ms == 1000 and cfg.jev.deadline_ms < cfg.jev.block_ms
    assert cfg.jev.model not in ("", "jev-latest")
