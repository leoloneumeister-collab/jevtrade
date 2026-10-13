"""Phase 2: a scoreboard that fills itself. Written before the implementation."""
import asyncio
import csv
import io
import json

import pytest

from jevbot import stock_app
from jevbot.market_data import demo_bars


def A(**kw):
    base = dict(direction="up", direction_conf=.7, p_up=.7, p_down=.1, p_flat=.2, uptrend=.6, overextended=.2,
                vol_regime="normal", vol_conf=.7, entry_score=2.5, entry_conf=.7, model="m", latency_ms=1)
    base.update(kw)
    return base


async def asker(snap, h):
    return A()


@pytest.fixture
def fake_prices(monkeypatch):
    """Pretend the price feed is real (not demo) so reads are logged, but keep it offline and deterministic."""
    bars = demo_bars("AAPL")
    monkeypatch.setattr(stock_app, "get_bars", lambda sym, demo: (bars, "test"))
    return bars


def lines(p):
    return [json.loads(l) for l in open(p).read().splitlines() if l.strip()]


def test_same_read_is_logged_once_per_symbol_day_horizon(tmp_path, fake_prices):
    log = str(tmp_path / "p.jsonl")
    first = asyncio.run(stock_app.analyze(asker, "AAPL", 5, False, log))
    again = asyncio.run(stock_app.analyze(asker, "AAPL", 5, False, log))
    assert len(lines(log)) == 1 and first["logged"] is True and again["logged"] is False
    asyncio.run(stock_app.analyze(asker, "AAPL", 10, False, log))              # different horizon is a new read
    asyncio.run(stock_app.analyze(asker, "MSFT", 5, False, log))                # different symbol too
    assert len(lines(log)) == 3


def test_demo_reads_never_logged(tmp_path, fake_prices):
    log = str(tmp_path / "p.jsonl")
    d = asyncio.run(stock_app.analyze(asker, "AAPL", 5, True, log))
    assert d["logged"] is False and not (tmp_path / "p.jsonl").exists()


def test_scoreboard_resolves_old_reads_and_leaves_recent_pending(tmp_path, fake_prices):
    bars = fake_prices
    log = tmp_path / "p.jsonl"
    old, recent = bars[-30], bars[-2]
    rows = [{"id": "a", "symbol": "AAPL", "asof": old.d, "close": old.c, "h": 5, "lean": "LEAN UP", "p_up": .8,
             "p_down": .1, "model": "m", "made_at": "x"},
            {"id": "b", "symbol": "AAPL", "asof": recent.d, "close": recent.c, "h": 5, "lean": "LEAN UP", "p_up": .8,
             "p_down": .1, "model": "m", "made_at": "x"}]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    sb = stock_app.scoreboard(str(log))
    assert sb["made"] == 2 and sb["pending"] == 1 and len(sb["recent"]) == 1
    expect = (bars[-30 + 5].c / bars[-30].c - 1) * 100
    assert sb["recent"][0]["fwd"] == pytest.approx(expect)


def test_csv_export_has_status_and_outcome(tmp_path, fake_prices):
    bars = fake_prices
    log = tmp_path / "p.jsonl"
    rows = [{"id": "a", "symbol": "AAPL", "asof": bars[-30].d, "close": bars[-30].c, "h": 5, "lean": "LEAN UP",
             "p_up": .8, "p_down": .1, "model": "m", "made_at": "t1"},
            {"id": "b", "symbol": "AAPL", "asof": bars[-2].d, "close": bars[-2].c, "h": 5, "lean": "SIT OUT",
             "p_up": .5, "p_down": .2, "model": "m", "made_at": "t2"}]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    out = list(csv.DictReader(io.StringIO(stock_app.scoreboard_csv(str(log)))))
    assert [r["status"] for r in out] == ["resolved", "pending"]
    assert out[0]["fwd_pct"] != "" and out[0]["correct"] in ("yes", "no") and out[1]["fwd_pct"] == ""
    assert out[1]["correct"] == ""                                              # sit-outs and pending have no verdict
    assert set(out[0]) >= {"made_at", "symbol", "asof", "h", "lean", "p_up", "p_down", "close", "model"}


def test_csv_of_empty_log_is_just_a_header(tmp_path):
    txt = stock_app.scoreboard_csv(str(tmp_path / "none.jsonl"))
    assert txt.strip().split(",")[0] == "made_at" and len(txt.strip().splitlines()) == 1


def test_scan_command_prints_table_and_logs(tmp_path, fake_prices, monkeypatch, capsys):
    from jevbot.cli import main
    monkeypatch.setattr(stock_app, "make_asker", lambda model: asker)
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy")
    monkeypatch.setattr("sys.argv", ["jevbot", "scan", "--symbols", "AAPL,MSFT", "--h", "5",
                                     "--log", str(tmp_path / "p.jsonl")])
    main()
    out = capsys.readouterr().out
    assert "AAPL" in out and "MSFT" in out and "LEAN UP" in out
    assert len(lines(tmp_path / "p.jsonl")) == 2


def test_scan_command_needs_a_key(monkeypatch, capsys):
    from jevbot.cli import main
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["jevbot", "scan", "--symbols", "AAPL"])
    with pytest.raises(SystemExit) as e:
        main()
    assert "TYPESAFE_API_KEY" in str(e.value)
