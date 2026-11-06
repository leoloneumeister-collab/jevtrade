"""Paper portfolio + trained model, walk-forward. Written before the implementation."""

import math
import random

import pytest

from jevbot import research as R


def synth_panel(n_days=600, n_stocks=20, signal=0.0, seed=1, h=5):
    """Rows with one informative feature 'f1' (if signal>0) and one noise feature 'f2'."""
    rnd = random.Random(seed)
    rows = []
    for d in range(n_days):
        date = f"{2010 + d // 250}-{(d % 250) // 21 + 1:02d}-{(d % 21) + 1:02d}"
        for s in range(n_stocks):
            f1, f2 = rnd.gauss(0, 1), rnd.gauss(0, 1)
            fwd = signal * f1 + rnd.gauss(0, 2)
            rows.append(
                {"date": date, "di": d, "symbol": f"S{s}", "x": {"f1": f1, "f2": f2, "ret_20d_pct": f2}, "fwd": fwd}
            )
    return rows


def test_walk_forward_learns_a_planted_signal_out_of_sample():
    out = R.walk_forward(synth_panel(signal=1.0), h=5, model="ridge")
    assert out["oos_ic_mean"] > 0.2 and out["test_years"]


def test_walk_forward_finds_nothing_in_noise():
    out = R.walk_forward(synth_panel(signal=0.0, seed=4), h=5, model="ridge")
    assert abs(out["oos_ic_mean"]) < 0.05


def test_training_never_sees_test_labels():
    rows = synth_panel(n_days=520)
    seen = []
    R.walk_forward(
        rows, h=5, model="ridge", _spy=lambda tr, te: seen.append((max(r["di"] for r in tr), min(r["di"] for r in te)))
    )
    assert seen and all(tr_max + 5 <= te_min for tr_max, te_min in seen)  # purge gap of h days


def test_scores_only_exist_for_test_years():
    out = R.walk_forward(synth_panel(), h=5, model="ridge")
    first_year = min(r["date"][:4] for r in synth_panel())
    assert all(not d.startswith(first_year) for d in out["scores"])


def test_paper_portfolio_math():
    # two dates, 5 stocks; top fifth (1 stock) returns known
    scores = {"d1": {f"S{i}": i for i in range(5)}, "d2": {f"S{i}": i for i in range(5)}}
    fwd = {("d1", f"S{i}"): float(i) for i in range(5)} | {("d2", f"S{i}"): float(i) for i in range(5)}
    p = R.paper_portfolio(scores, fwd, ["d1", "d2"], cost=0.0)
    assert p["periods"] == 2 and p["strategy_total_pct"] == pytest.approx((1.04 * 1.04 - 1) * 100)
    assert p["benchmark_total_pct"] == pytest.approx((1.02 * 1.02 - 1) * 100)
    assert p["turnover_avg"] == pytest.approx(0.25)  # first rebalance trades half the book, second nothing


def test_costs_reduce_returns():
    scores = {f"d{k}": {f"S{i}": (i + k) % 5 for i in range(5)} for k in range(10)}
    fwd = {(f"d{k}", f"S{i}"): 0.5 for k in range(10) for i in range(5)}
    free = R.paper_portfolio(scores, fwd, list(scores), cost=0.0)
    paid = R.paper_portfolio(scores, fwd, list(scores), cost=0.5)
    assert paid["strategy_total_pct"] < free["strategy_total_pct"]


def test_drawdown_and_sharpe_are_sane():
    scores = {f"d{k}": {f"S{i}": i for i in range(5)} for k in range(50)}
    fwd = {(f"d{k}", f"S{i}"): (2.0 if k % 2 else -1.0) for k in range(50) for i in range(5)}
    p = R.paper_portfolio(scores, fwd, list(scores), cost=0.0, periods_per_year=50)
    assert p["max_drawdown_pct"] < 0 and math.isfinite(p["sharpe"])


def test_end_to_end_on_demo_prices():
    out = R.run_research(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], "demo", h=5)
    assert {"ridge", "boost", "momentum", "benchmark", "verdict"} <= set(out)
    assert out["ridge"]["paper"]["periods"] > 0


# ---- specialist desks ---------------------------------------------------------------------------
def test_each_specialist_scores_every_stock():
    x = {"ret_20d_pct": 5.0, "vs_sma50_pct": 3.0, "ret_5d_pct": -2.0, "rsi14": 40.0, "vol_20d_pct": 1.5}
    for name, fn in R.SPECIALISTS.items():
        assert isinstance(fn(x), float), name
    assert R.SPECIALISTS["trend"](x) > R.SPECIALISTS["trend"]({**x, "ret_20d_pct": -5, "vs_sma50_pct": -3})
    assert R.SPECIALISTS["reversion"](x) > R.SPECIALISTS["reversion"]({**x, "ret_5d_pct": 4, "rsi14": 70})


def test_risk_officer_vetoes_wild_stocks():
    day = {f"S{i}": {"vol_20d_pct": 1.0} for i in range(9)} | {"WILD": {"vol_20d_pct": 6.0}}
    assert R.risk_veto(day) == {"WILD"}


def test_vetoed_stock_is_never_bought():
    scores = {"d1": {f"S{i}": i for i in range(5)}}
    fwd = {("d1", f"S{i}"): 1.0 for i in range(5)}
    p = R.paper_portfolio(scores, fwd, ["d1"], cost=0.0, vetoes={"d1": {"S4"}}, log=True)
    assert "S4" not in p["log"][0]["buy"]


def test_trade_log_records_buys_and_sells():
    scores = {"d1": {f"S{i}": i for i in range(5)}, "d2": {f"S{i}": -i for i in range(5)}}
    fwd = {(d, f"S{i}"): 0.0 for d in ("d1", "d2") for i in range(5)}
    p = R.paper_portfolio(scores, fwd, ["d1", "d2"], cost=0.0, log=True)
    assert p["log"][0]["buy"] == ["S4"] and p["log"][1]["sell"] == ["S4"] and p["log"][1]["buy"] == ["S0"]
