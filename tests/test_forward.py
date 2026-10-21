"""Step 1: the forward (live) test is valid from day one. Written before the implementation."""

import asyncio
import json
import random
from pathlib import Path

import pytest

from jevbot import cross, stock_app
from jevbot.market_data import demo_bars


def A(p=0.6):
    return {
        "direction": "up",
        "direction_conf": 0.7,
        "p_up": p,
        "p_down": 1 - p,
        "p_flat": 0.0,
        "uptrend": 0.6,
        "overextended": 0.2,
        "vol_regime": "normal",
        "vol_conf": 0.7,
        "entry_score": 2.5,
        "entry_conf": 0.7,
        "model": "m",
        "latency_ms": 1,
    }


@pytest.fixture
def prices(monkeypatch):
    cache = {}
    monkeypatch.setattr(stock_app, "get_bars", lambda s, src: (cache.setdefault(s, demo_bars(s)), "test"))
    return cache


def test_live_reads_log_the_momentum_score(tmp_path, prices):
    log = str(tmp_path / "p.jsonl")

    async def asker(snap, h):
        return A()

    d = asyncio.run(stock_app.analyze(asker, "AAA", 5, "live", log))
    rec = json.loads(Path(log).read_text().splitlines()[0])
    assert rec["mom"] == d["features"]["ret_20d_pct"]


def write_log(path, bars_by_sym, n_days=40, h=5, signal=0.0, seed=1, with_mom=True):
    """Fake forward reads on already-matured days, with an optional planted relationship between p_up and the outcome."""
    rnd = random.Random(seed)
    rows = []
    for sym, bars in bars_by_sym.items():
        for k in range(n_days):
            i = len(bars) - 1 - h - 40 + k
            fwd = (bars[i + h].c / bars[i].c - 1) * 100
            p = min(0.99, max(0.01, 0.5 + signal * fwd / 20 + rnd.gauss(0, 0.15)))
            r = {
                "id": f"{sym}{k}",
                "symbol": sym,
                "asof": bars[i].d,
                "close": bars[i].c,
                "h": h,
                "lean": "SIT OUT",
                "p_up": p,
                "p_down": 1 - p,
                "model": "m",
                "made_at": "x",
            }
            if with_mom:
                r["mom"] = rnd.gauss(0, 3)
            rows.append(r)
    path.write_text("\n".join(json.dumps(r) for r in rows))


SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


def test_forward_cross_report_structure(tmp_path, prices):
    for s in SYMS:
        prices[s] = demo_bars(s)
    log = tmp_path / "p.jsonl"
    write_log(log, {s: prices[s] for s in SYMS})
    r = cross.forward_cross(str(log))
    g = r["horizons"]["5"]
    assert r["made"] == 8 * 40 and r["pending"] == 0 and g["n_stocks"] == 8
    assert {"jev", "momentum", "verdict", "first_date", "last_date", "stop_rule"} <= set(g)
    assert g["jev"]["dates"] == 40 and len(g["jev"]["quintiles"]) == 5


def test_forward_cross_detects_a_planted_signal_and_not_noise(tmp_path, prices):
    for s in SYMS:
        prices[s] = demo_bars(s)
    good, bad = tmp_path / "g.jsonl", tmp_path / "b.jsonl"
    write_log(good, {s: prices[s] for s in SYMS}, signal=40.0)
    write_log(bad, {s: prices[s] for s in SYMS}, signal=0.0, seed=9)
    assert cross.forward_cross(str(good))["horizons"]["5"]["jev"]["ic_mean"] > 0.3
    assert abs(cross.forward_cross(str(bad))["horizons"]["5"]["jev"]["ic_mean"]) < 0.2


def test_pending_reads_are_counted_not_scored(tmp_path, prices):
    for s in SYMS:
        prices[s] = demo_bars(s)
    log = tmp_path / "p.jsonl"
    write_log(log, {s: prices[s] for s in SYMS}, n_days=10)
    extra = {
        "id": "z",
        "symbol": "AAA",
        "asof": prices["AAA"][-1].d,
        "close": 1,
        "h": 5,
        "lean": "SIT OUT",
        "p_up": 0.5,
        "p_down": 0.5,
        "model": "m",
        "made_at": "x",
        "mom": 0.0,
    }
    with open(log, "a") as f:
        f.write("\n" + json.dumps(extra))
    assert cross.forward_cross(str(log))["pending"] == 1


def test_horizons_are_never_mixed(tmp_path, prices):
    for s in SYMS:
        prices[s] = demo_bars(s)
    log = tmp_path / "p.jsonl"
    write_log(log, {s: prices[s] for s in SYMS}, h=5)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    more = [{**r, "id": r["id"] + "h1", "h": 1} for r in rows]
    log.write_text("\n".join(json.dumps(r) for r in rows + more))
    assert set(cross.forward_cross(str(log))["horizons"]) == {"1", "5"}


def test_old_reads_without_momentum_are_reported_and_excluded_from_the_benchmark(tmp_path, prices):
    for s in SYMS:
        prices[s] = demo_bars(s)
    log = tmp_path / "p.jsonl"
    write_log(log, {s: prices[s] for s in SYMS}, with_mom=False)
    r = cross.forward_cross(str(log))
    assert r["missing_mom"] == 8 * 40 and r["horizons"]["5"]["momentum"]["dates"] == 0


def test_stop_rule_text_is_explicit(tmp_path, prices):
    for s in SYMS:
        prices[s] = demo_bars(s)
    log = tmp_path / "p.jsonl"
    write_log(log, {s: prices[s] for s in SYMS})
    rule = cross.forward_cross(str(log))["horizons"]["5"]["stop_rule"]
    assert "60" in rule and "t" in rule.lower()


def test_empty_log(tmp_path):
    r = cross.forward_cross(str(tmp_path / "none.jsonl"))
    assert r == {"made": 0, "pending": 0, "missing_mom": 0, "horizons": {}}


def test_forward_command_prints_report(tmp_path, prices, monkeypatch, capsys):
    from jevbot.cli import main

    for s in SYMS:
        prices[s] = demo_bars(s)
    log = tmp_path / "p.jsonl"
    write_log(log, {s: prices[s] for s in SYMS})
    monkeypatch.setattr("sys.argv", ["jevbot", "forward", "--log", str(log)])
    main()
    out = capsys.readouterr().out
    assert "look-ahead 5" in out and "Jev" in out and "Verdict" in out and "Stop rule" in out
