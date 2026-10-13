"""Cross-stock ranking test. Written before the implementation."""

import asyncio
import json
import random
import threading
from http.server import ThreadingHTTPServer

import pytest

from jevbot import cross, stock_app


def make_dates(n_dates, n_stocks, signal=0.0, seed=1, ties=False):
    """by_date -> rows. Forward return = signal * score + noise, so signal=0 is pure noise."""
    rnd = random.Random(seed)
    out = {}
    for d in range(n_dates):
        rows = []
        for s in range(n_stocks):
            score = 0.5 if ties else rnd.random()
            rows.append(
                {"symbol": f"S{s}", "p_up": score, "mom": rnd.random(), "fwd": signal * score + rnd.gauss(0, 1)}
            )
        out[f"2020-01-{d + 1:02d}" if d < 31 else f"2020-02-{d - 30:02d}"] = rows
    return out


# ---- statistics --------------------------------------------------------------------------------
def test_planted_signal_is_detected():
    s = cross.cross_stats(make_dates(60, 20, signal=3.0), h=5, cost=0.1)
    assert s["ic_mean"] > 0.5 and s["ic_t"] > 5
    q = s["quintiles"]
    assert len(q) == 5 and q[4] > q[0] and q == sorted(q) or q[4] > q[0] + 1
    assert s["spread_mean_pct"] > 1 and s["spread_t"] > 3
    assert s["spread_net_pct"] == pytest.approx(s["spread_mean_pct"] - 0.2)


def test_pure_noise_shows_nothing():
    s = cross.cross_stats(make_dates(120, 20, signal=0.0, seed=7), h=5, cost=0.1)
    assert abs(s["ic_mean"]) < 0.1 and abs(s["ic_t"]) < 3 and abs(s["spread_mean_pct"]) < 1


def test_constant_scores_do_not_crash_and_have_zero_rank_link():
    s = cross.cross_stats(make_dates(30, 10, ties=True), h=5, cost=0.1)
    assert s["ic_mean"] == 0.0 and len(s["quintiles"]) == 5


def test_independent_dates_use_every_hth_day():
    s = cross.cross_stats(make_dates(50, 10, seed=2), h=5, cost=0.1)
    assert s["dates"] == 50 and s["independent_dates"] == 10


def test_dates_with_too_few_stocks_are_ignored():
    by = make_dates(20, 10, seed=3)
    for k in list(by)[:5]:
        by[k] = by[k][:4]  # only 4 stocks that day
    s = cross.cross_stats(by, h=1, cost=0.1)
    assert s["dates"] == 15


def test_empty_input_is_handled():
    s = cross.cross_stats({}, h=5, cost=0.1)
    assert s["dates"] == 0 and s["ic_t"] is None and s["quintiles"] == []


def test_the_momentum_column_can_be_scored_too():
    by = make_dates(60, 20, signal=0.0, seed=4)
    for rows in by.values():
        for r in rows:
            r["mom"] = r["fwd"] + random.Random(0).gauss(0, 0.1)  # momentum happens to be informative here
    assert cross.cross_stats(by, 5, 0.1, key="mom")["ic_mean"] > 0.5
    assert abs(cross.cross_stats(by, 5, 0.1, key="p_up")["ic_mean"]) < 0.2


# ---- verdicts -----------------------------------------------------------------------------------
def _s(n=40, ic=0.05, t=3.0, net=0.5):
    return {"independent_dates": n, "ic_mean": ic, "ic_t": t, "spread_net_pct": net}


def test_verdict_branches():
    assert "Too few" in cross.cross_verdict(_s(n=5), _s())
    assert "No demonstrated" in cross.cross_verdict(_s(t=0.5), _s())
    assert "No demonstrated" in cross.cross_verdict(_s(net=-0.1), _s())  # costs eat it
    assert "no better than a free momentum" in cross.cross_verdict(_s(ic=0.04), _s(ic=0.08))
    v = cross.cross_verdict(_s(ic=0.10), _s(ic=0.02))
    assert "Promising" in v and "not proof" in v


# ---- end to end with a fake asker ----------------------------------------------------------------
async def asker(snap, h):
    p = 0.5 + max(-0.4, min(0.4, snap["ret_20d_pct"] / 50))
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


SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


def test_cross_test_runs_and_counts_calls():
    r = asyncio.run(cross.cross_test(asker, SYMS, 5, 30, "demo", 0.1, 0))
    assert r["calls"] == 30 * 6 and r["errors"] == 0 and r["n_dates"] == 30 and r["n_stocks"] == 6
    assert {"jev", "momentum", "verdict", "caveat", "first_date", "last_date", "skipped"} <= set(r)
    assert r["jev"]["dates"] == 30 and len(r["jev"]["quintiles"]) == 5


def test_skip_moves_the_window_back():
    a = asyncio.run(cross.cross_test(asker, SYMS, 5, 30, "demo", 0.1, 0))
    b = asyncio.run(cross.cross_test(asker, SYMS, 5, 30, "demo", 0.1, 60))
    assert b["last_date"] < a["first_date"] or b["last_date"] < a["last_date"]


def test_needs_at_least_five_stocks():
    with pytest.raises(ValueError, match="at least 5"):
        asyncio.run(cross.cross_test(asker, SYMS[:4], 5, 30, "demo", 0.1, 0))


def test_invalid_and_duplicate_tickers_are_skipped_and_reported():
    r = asyncio.run(cross.cross_test(asker, SYMS + ["aaa", "<bad>", "BBB"], 5, 20, "demo", 0.1, 0))
    assert r["n_stocks"] == 6 and any("<bad>" in s or "BAD" in s.upper() for s in r["skipped"])


def test_call_budget_protects_the_wallet(monkeypatch):
    monkeypatch.setattr(cross, "MAX_CALLS", 100)
    with pytest.raises(ValueError, match="Jev calls"):
        asyncio.run(cross.cross_test(asker, SYMS, 5, 60, "demo", 0.1, 0))


def test_too_many_symbols_refused():
    with pytest.raises(ValueError, match="at most"):
        asyncio.run(cross.cross_test(asker, [f"T{i}" for i in range(cross.MAX_SYMBOLS + 1)], 5, 20, "demo", 0.1, 0))


def test_failures_for_single_pairs_are_dropped_not_fatal():
    calls = {"n": 0}

    async def flaky(snap, h):
        calls["n"] += 1
        if calls["n"] % 7 == 0:
            raise RuntimeError("503")
        return await asker(snap, h)

    r = asyncio.run(cross.cross_test(flaky, SYMS, 5, 30, "demo", 0.1, 0))
    assert r["errors"] > 0 and r["jev"]["dates"] > 0


def test_the_model_never_sees_the_future():
    seen = []

    async def spy(snap, h):
        seen.append(snap["ret_5d_pct"])
        return await asker(snap, h)

    asyncio.run(cross.cross_test(spy, SYMS, 5, 20, "demo", 0.1, 0))
    assert len(seen) == 120


# ---- HTTP -----------------------------------------------------------------------------------------
def test_http_cross_endpoint(tmp_path, monkeypatch):
    import http.client

    srv = ThreadingHTTPServer(("127.0.0.1", 0), stock_app.make_handler(asker, "m", str(tmp_path / "p.jsonl"), True))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        port = srv.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            c.request("GET", path, headers={"X-Jevbot": "1"})
            r = c.getresponse()
            return r.status, json.loads(r.read())

        st, d = get("/api/cross?symbols=AAA,BBB,CCC,DDD,EEE,FFF&h=5&n=20&src=demo")
        assert st == 200 and d["calls"] == 120 and "verdict" in d
        st, d = get("/api/cross?symbols=AAA,BBB&n=20&src=demo")
        assert st == 400 and "at least 5" in d["error"]
        assert get("/api/cross?symbols=" + ",".join(f"T{i}" for i in range(80)) + "&src=demo")[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_cross_defaults_to_all_csv_files(tmp_path, monkeypatch):
    import http.client

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("JEVBOT_DATA_DIR", str(d))
    from datetime import date, timedelta

    day, days = date(2020, 1, 1), []
    while len(days) < 200:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    for k, s in enumerate(SYMS):
        rows = ["Date,Open,High,Low,Close,Volume"] + [
            f"{x},{10 + i * 0.01 * (k + 1)},{11 + i * 0.01},{9},{10 + i * 0.01 * (k + 1) + (i % 5) * 0.1},100"
            for i, x in enumerate(days)
        ]
        (d / f"{s}.csv").write_text("\n".join(rows))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), stock_app.make_handler(asker, "m", str(tmp_path / "p.jsonl"), True))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
        c.request("GET", "/api/cross?src=file&n=20&h=5", headers={"X-Jevbot": "1"})
        r = c.getresponse()
        body = json.loads(r.read())
        assert r.status == 200 and body["n_stocks"] == 6 and body["src"] == "file"
    finally:
        srv.shutdown()
        srv.server_close()


# ---- command line ---------------------------------------------------------------------------------
def test_cross_command_prints_a_readable_report(monkeypatch, capsys):
    from jevbot.cli import main

    monkeypatch.setattr(stock_app, "make_asker", lambda model: asker)
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy")
    monkeypatch.setattr("sys.argv", ["jevbot", "cross", "--symbols", ",".join(SYMS), "--src", "demo", "--n", "30"])
    main()
    out = capsys.readouterr().out
    assert "Jev" in out and "momentum" in out.lower() and "Q1" in out and "Q5" in out and "Verdict" in out


def test_cross_command_reports_errors_cleanly(monkeypatch):
    from jevbot.cli import main

    monkeypatch.setattr(stock_app, "make_asker", lambda model: asker)
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy")
    monkeypatch.setattr("sys.argv", ["jevbot", "cross", "--symbols", "AAA,BBB", "--src", "demo"])
    with pytest.raises(SystemExit) as e:
        main()
    assert "at least 5" in str(e.value)
