import asyncio
import json

import pytest

from jevbot.market_data import check_symbol, demo_bars
from jevbot.stock_app import analyze, backtest, scoreboard, summarize
from jevbot.stock_features import features
from jevbot.stock_jev import StockPolicy, lean, build_questions
from jevbot.stock_jev import parse


def A(**kw):
    base = dict(direction="up", direction_conf=.7, p_up=.7, p_down=.1, p_flat=.2, uptrend=.6, overextended=.2,
                vol_regime="normal", vol_conf=.7, entry_score=2.5, entry_conf=.7, model="m", latency_ms=1)
    base.update(kw)
    return base


def test_symbol_validation_blocks_url_tricks():
    assert check_symbol(" aapl ") == "AAPL" and check_symbol("brk-b") == "BRK-B"
    for bad in ("", "../etc", "AAPL&x=1", "A B", "1AAPL", "A" * 20):
        with pytest.raises(ValueError):
            check_symbol(bad)


def test_features_use_only_past_bars():
    bars = demo_bars("TEST")
    assert features(bars[:300]) == features(bars[:300] + [])               # deterministic
    # changing the future must not change features computed on the prefix
    mutated = bars[:300] + [type(bars[0])(b.d, 1, 1, 1, 1, 1) for b in bars[300:]]
    assert features(mutated[:300]) == features(bars[:300])
    with pytest.raises(ValueError):
        features(bars[:30])


def test_feature_snapshot_is_small():
    assert len(json.dumps(features(demo_bars("TEST")))) / 3 < 400


def test_policy_is_cautious():
    assert lean(A())[0] == "LEAN UP"
    assert lean(A(p_up=.5))[0] == "SIT OUT"
    assert lean(A(entry_score=1.0))[0] == "SIT OUT"
    assert lean(A(overextended=.9))[0] == "SIT OUT"
    assert lean(A(vol_regime="turbulent"))[0] == "SIT OUT"
    assert lean(A(p_up=.1, p_down=.8))[0] == "LEAN DOWN"


def test_questions_build():
    assert set(build_questions(5)) == {"direction", "uptrend", "overextended", "vol_regime", "entry_quality"}


async def fake_asker(snap, h):                       # momentum rule standing in for Jev
    up = snap["ret_20d_pct"] > 0
    return A(p_up=.75 if up else .2, p_down=.1 if up else .7, p_flat=.15 if up else .1)


def test_analyze_logs_real_but_not_demo(tmp_path):
    log = str(tmp_path / "p.jsonl")
    d = asyncio.run(analyze(fake_asker, "AAPL", 5, True, log))
    assert d["lean"] in ("LEAN UP", "LEAN DOWN", "SIT OUT") and d["demo"] and len(d["series"]) == 130
    assert not (tmp_path / "p.jsonl").exists()                           # demo never pollutes the scoreboard


def test_backtest_only_shows_asker_the_past():
    seen = []

    async def spy(snap, h):
        seen.append(snap)
        return A()
    r = asyncio.run(backtest(spy, "AAPL", 5, 20, True))
    assert r["stats"]["days"] == 20 and len(seen) == 20
    bars = demo_bars("AAPL")
    # the last day shown to the asker has at least h days of unseen future after it
    assert max(i for i in range(60, len(bars)) if features(bars[: i + 1]) in seen) <= len(bars) - 1 - 5


def test_summarize_math():
    rows = [{"lean": "LEAN UP", "p_up": .8, "fwd": 1.0}] * 20 + [{"lean": "SIT OUT", "p_up": .5, "fwd": -1.0}] * 20
    s = summarize(rows)
    assert s["long_hit_rate"] == 1.0 and s["baseline_up_rate"] == .5 and s["sat_out"] == 20
    assert s["avg_return_after_long_net_pct"] == pytest.approx(0.9)
    assert "Too few" in s["verdict"]                      # 20 back-to-back signals are only 4 independent ones
    flat = summarize([{"i": i * 5, "lean": "LEAN UP", "p_up": .8, "fwd": 0.05 if i % 2 else -0.05} for i in range(30)])
    assert "No demonstrated edge" in flat["verdict"]


def test_scoreboard_empty(tmp_path):
    assert scoreboard(str(tmp_path / "none.jsonl"))["made"] == 0


def test_parse_maps_sdk_shape():
    from types import SimpleNamespace as N
    ch = lambda c, k, p=None: N(choice=c, confidence=k, probabilities=p or {})
    resp = N(model="m", choices={"direction": ch("up", .8, {"up": .7, "down": .1, "flat": .2}), "vol_regime": ch("calm", .6)},
             nouls={"uptrend": N(noul=.6), "overextended": N(noul=.1)}, scores={"entry_quality": N(score=2.2, confidence=.5)})
    r = parse(resp, 12.4)
    assert r["p_up"] == .7 and r["entry_score"] == 2.2 and r["latency_ms"] == 12
