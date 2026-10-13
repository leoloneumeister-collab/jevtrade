"""Phase 1: honest evidence. Written before the implementation."""
import asyncio

import pytest

from jevbot.market_data import demo_bars
from jevbot.stock_app import backtest, non_overlapping, scan, summarize
from jevbot.stock_features import features, momentum_rule


def A(**kw):
    base = dict(direction="up", direction_conf=.7, p_up=.7, p_down=.1, p_flat=.2, uptrend=.6, overextended=.2,
                vol_regime="normal", vol_conf=.7, entry_score=2.5, entry_conf=.7, model="m", latency_ms=1)
    base.update(kw)
    return base


# ---- the dumb rule Jev has to beat --------------------------------------
def test_momentum_rule():
    up = dict(ret_20d_pct=5, vs_sma50_pct=3, rsi14=60)
    assert momentum_rule(up) == "LEAN UP"
    assert momentum_rule({**up, "rsi14": 80}) == "SIT OUT"                 # too hot
    assert momentum_rule({**up, "vs_sma50_pct": -1}) == "SIT OUT"          # signals disagree
    assert momentum_rule(dict(ret_20d_pct=-5, vs_sma50_pct=-3, rsi14=40)) == "LEAN DOWN"


# ---- overlapping windows must not count as independent evidence ----------
def test_non_overlapping_picks_spaced_signals_per_symbol():
    rows = [{"i": i, "symbol": "A", "lean": "LEAN UP"} for i in range(20)]
    assert [r["i"] for r in non_overlapping(rows, 5)] == [0, 5, 10, 15]
    mixed = rows + [{"i": i, "symbol": "B", "lean": "LEAN UP"} for i in range(10)]
    assert len(non_overlapping(mixed, 5)) == 4 + 2                         # each symbol has its own clock
    assert non_overlapping([{"i": 0, "symbol": "A", "lean": "SIT OUT"}], 5) == []


def test_summarize_uses_independent_signals_for_the_verdict():
    rows = [{"i": i, "lean": "LEAN UP", "p_up": .8, "fwd": 1.0} for i in range(40)]
    s = summarize(rows, h=5)
    assert s["long_signals"] == 40 and s["independent_long"] == 8
    assert "Too few" in s["verdict"]                                       # 40 raw signals, only 8 independent
    big = [{"i": i, "lean": "LEAN UP" if i % 5 == 0 else "SIT OUT", "p_up": .8, "fwd": 1.0 if i % 10 else -0.2}
           for i in range(150)]
    assert summarize(big, h=5)["independent_long"] == 30


def test_promising_needs_edge_after_costs_and_enough_independent_signals():
    rows = []
    for i in range(200):
        up = i % 5 == 0
        rows.append({"i": i, "lean": "LEAN UP" if up else "SIT OUT", "p_up": .7, "fwd": 2.0 if up else -1.0})
    s = summarize(rows, h=5)
    assert s["independent_long"] == 40 and s["long_hit_rate"] == 1.0 and "Promising" in s["verdict"]
    assert summarize(rows, h=5, cost=5.0)["avg_return_after_long_net_pct"] == pytest.approx(-3.0)
    assert "No demonstrated edge" in summarize(rows, h=5, cost=5.0)["verdict"]     # costs wipe the edge


def test_summarize_can_score_the_rule_column():
    rows = [{"i": i, "lean": "SIT OUT", "rule_lean": "LEAN UP", "p_up": .5, "fwd": 1.0} for i in range(50)]
    assert summarize(rows, h=5)["long_signals"] == 0
    assert summarize(rows, h=5, key="rule_lean")["long_signals"] == 50


# ---- backtest compares Jev with the rule on identical days ---------------
def test_backtest_reports_jev_rule_and_comparison():
    r = asyncio.run(backtest(lambda s, h: _always(A()), "AAPL", 5, 120, True, cost=0.2))
    assert {"stats", "rule_stats", "comparison"} <= set(r)
    assert r["stats"]["days"] == r["rule_stats"]["days"] == 120            # same days for both
    assert r["cost_pct"] == 0.2 and isinstance(r["comparison"], str)


async def _always(a):
    return a


def test_backtest_allows_long_windows_but_caps_them():
    r = asyncio.run(backtest(lambda s, h: _always(A()), "AAPL", 5, 999, True))
    assert 100 <= r["stats"]["days"] <= 250


def _stats(n_ind=20, hit=.6, net=1.0, all_days=0.2):
    return {"independent_long": n_ind, "long_hit_rate": hit, "avg_return_after_long_net_pct": net,
            "avg_return_all_days_pct": all_days}


def test_compare_verdicts():
    from jevbot.stock_app import compare
    assert "Not enough" in compare(_stats(n_ind=3), _stats())
    assert "too few" in compare(_stats(), _stats(n_ind=2))
    assert "beat the simple momentum rule" in compare(_stats(hit=.7, net=1.5), _stats(hit=.55, net=.5))
    assert "did not beat the simple momentum rule" in compare(_stats(hit=.5, net=.4), _stats(hit=.6, net=.9))
    assert "simply holding" in compare(_stats(net=0.1, all_days=0.5), _stats(hit=.4, net=-1))   # holding wins


def test_always_up_is_not_credited_as_skill():
    r = asyncio.run(backtest(lambda s, h: _always(A()), "AAPL", 5, 200, True))
    assert "Jev beat" not in r["comparison"]


# ---- watchlist scan -------------------------------------------------------
def test_scan_isolates_failures_and_sorts(tmp_path):
    async def asker(snap, h):
        return A(p_up=.9 if snap["ret_20d_pct"] > 0 else .1, p_down=.05 if snap["ret_20d_pct"] > 0 else .8)
    out = asyncio.run(scan(asker, ["AAPL", "MSFT", "bad ticker!", "NVDA"], 5, True, None))
    assert len(out["rows"]) == 4
    assert [r["symbol"] for r in out["rows"] if r.get("error")] == ["BAD TICKER!"] or any(r.get("error") for r in out["rows"])
    ok = [r for r in out["rows"] if not r.get("error")]
    assert len(ok) == 3 and all(r["lean"] in ("LEAN UP", "LEAN DOWN", "SIT OUT") for r in ok)
    ps = [r["p_up"] for r in ok]
    assert ps == sorted(ps, reverse=True)                                   # strongest first


def test_scan_caps_the_watchlist_to_protect_your_wallet():
    out = asyncio.run(scan(lambda s, h: _always(A()), [f"T{i}" for i in range(60)], 5, True, None))
    assert len(out["rows"]) == 25 and out["truncated"] is True
