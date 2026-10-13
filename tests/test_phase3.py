"""Phase 3: reliability. Written before the implementation."""
import asyncio
import threading
import urllib.error
from datetime import date

import pytest

from jevbot import market_data as md
from jevbot import stock_app
from jevbot.errors import friendly_error
from jevbot.market_data import Bar, demo_bars


def http(code):
    return urllib.error.HTTPError("http://x", code, "msg", {}, None)


# ---- retry with backoff ---------------------------------------------------
def test_retry_backs_off_then_succeeds():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("temporary")
        return "ok"
    assert md.with_retry(flaky, tries=3, base=0.5, sleep=sleeps.append) == "ok"
    assert sleeps == [0.5, 1.0]


def test_retry_gives_up_and_raises_last_error():
    with pytest.raises(urllib.error.URLError):
        md.with_retry(lambda: (_ for _ in ()).throw(urllib.error.URLError("down")), tries=3, base=0, sleep=lambda s: None)


@pytest.mark.parametrize("err", [http(404), http(403), ValueError("bad"), KeyError("x")])
def test_retry_does_not_repeat_permanent_errors(err):
    n = []

    def f():
        n.append(1)
        raise err
    with pytest.raises(type(err)):
        md.with_retry(f, tries=3, base=0, sleep=lambda s: None)
    assert len(n) == 1


@pytest.mark.parametrize("code", [429, 500, 503])
def test_retry_repeats_rate_limit_and_server_errors(code):
    n = []

    def f():
        n.append(1)
        raise http(code)
    with pytest.raises(urllib.error.HTTPError):
        md.with_retry(f, tries=3, base=0, sleep=lambda s: None)
    assert len(n) == 3


# ---- source fallback + validation ---------------------------------------
def test_falls_back_to_second_source(monkeypatch):
    good = demo_bars("X")

    def boom(sym):
        raise urllib.error.URLError("yahoo down")
    monkeypatch.setattr(md, "_yahoo", boom)
    monkeypatch.setattr(md, "_stooq", lambda sym: good)
    monkeypatch.setattr(md, "_sleep", lambda s: None)
    bars, src = md.fetch_bars("X")
    assert src == "stooq" and bars == good


def test_both_sources_down_names_both(monkeypatch):
    monkeypatch.setattr(md, "_yahoo", lambda s: (_ for _ in ()).throw(http(429)))
    monkeypatch.setattr(md, "_stooq", lambda s: (_ for _ in ()).throw(urllib.error.URLError("nope")))
    monkeypatch.setattr(md, "_sleep", lambda s: None)
    with pytest.raises(RuntimeError) as e:
        md.fetch_bars("X")
    assert "yahoo" in str(e.value) and "stooq" in str(e.value)


def test_validate_bars_rejects_bad_data():
    good = demo_bars("X")
    md.validate_bars(good)
    with pytest.raises(ValueError, match="at least"):
        md.validate_bars(good[:20])
    bad = list(good)
    bad[50] = Bar(bad[50].d, 1, 1, 1, 0.0, 1)
    with pytest.raises(ValueError, match="price"):
        md.validate_bars(bad)
    with pytest.raises(ValueError, match="order"):
        md.validate_bars(list(reversed(good)))
    dup = list(good)
    dup[10] = dup[11]
    with pytest.raises(ValueError, match="duplicate|order"):
        md.validate_bars(dup)


def test_corrupt_source_is_skipped_not_trusted(monkeypatch):
    bad = [Bar(b.d, b.o, b.h, b.l, -1.0, b.v) for b in demo_bars("X")]
    good = demo_bars("Y")
    monkeypatch.setattr(md, "_yahoo", lambda s: bad)
    monkeypatch.setattr(md, "_stooq", lambda s: good)
    monkeypatch.setattr(md, "_sleep", lambda s: None)
    assert md.fetch_bars("X")[1] == "stooq"


# ---- cache ----------------------------------------------------------------
def test_cache_hits_expiry_and_no_caching_of_failures(monkeypatch):
    clock, calls = [1000.0], []
    monkeypatch.setattr(md, "_clock", lambda: clock[0])
    md.clear_cache()

    def fake(sym):
        calls.append(sym)
        if sym == "BAD":
            raise RuntimeError("fail")
        return demo_bars(sym), "test"
    monkeypatch.setattr(md, "fetch_bars", fake)
    md.fetch_bars_cached("AAA", ttl=900)
    md.fetch_bars_cached("AAA", ttl=900)
    assert calls == ["AAA"]
    md.fetch_bars_cached("BBB", ttl=900)
    assert calls == ["AAA", "BBB"]
    clock[0] += 901
    md.fetch_bars_cached("AAA", ttl=900)
    assert calls == ["AAA", "BBB", "AAA"]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            md.fetch_bars_cached("BAD", ttl=900)
    assert calls.count("BAD") == 2


def test_cache_is_safe_under_threads(monkeypatch):
    md.clear_cache()
    n = []
    monkeypatch.setattr(md, "fetch_bars", lambda s: (n.append(1), (demo_bars(s), "t"))[1])
    ts = [threading.Thread(target=md.fetch_bars_cached, args=("ZZZ", 900)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(n) == 1


# ---- stale data -----------------------------------------------------------
def test_stale_warning():
    assert stock_app.stale_warning("2026-10-09", today=date(2026, 10, 12)) is None            # Friday close, Monday morning
    assert stock_app.stale_warning("2026-10-02", today=date(2026, 10, 12)) is not None
    assert "10 days" in stock_app.stale_warning("2026-10-02", today=date(2026, 10, 12))


def test_analyze_surfaces_stale_warning(monkeypatch):
    bars = demo_bars("AAPL")
    monkeypatch.setattr(stock_app, "get_bars", lambda s, d: (bars, "test"))
    monkeypatch.setattr(stock_app, "_today", lambda: date.fromisoformat(bars[-1].d).replace(year=2099))

    async def asker(snap, h):
        return dict(direction="up", direction_conf=.7, p_up=.7, p_down=.1, p_flat=.2, uptrend=.6, overextended=.2,
                    vol_regime="normal", vol_conf=.7, entry_score=2.5, entry_conf=.7, model="m", latency_ms=1)
    d = asyncio.run(stock_app.analyze(asker, "AAPL", 5, False, None))
    assert d["warning"] and "old" in d["warning"]


# ---- friendly errors ------------------------------------------------------
def test_friendly_errors():
    assert "not found" in friendly_error(http(404)).lower()
    assert "rate" in friendly_error(http(429)).lower()
    assert "internet" in friendly_error(urllib.error.URLError("x")).lower()
    assert friendly_error(ValueError("Ticker looks invalid.")) == "Ticker looks invalid."
    assert "401" in friendly_error(type("TypeSafeAuthenticationError", (Exception,), {})("401 nope"))
    assert "timed out" in friendly_error(TimeoutError()).lower()
    assert friendly_error(RuntimeError("boom")) == "RuntimeError: boom"
