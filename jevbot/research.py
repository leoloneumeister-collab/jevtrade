"""Research desk: trained model, specialist "departments", a risk officer and a paper portfolio. Walk-forward only.

Departments each score every stock their own way. The risk officer can veto buys. The portfolio manager (committee)
combines the desks with weights learned on past years only. Every desk is also paper traded alone, so you can see
which one, if any, adds anything over simply holding all stocks.
Long only: each rebalance holds the top fifth by score; a stock is sold when it drops out of the top fifth or is vetoed.
"""

from __future__ import annotations

import math
import statistics as st
from collections.abc import Callable

import numpy as np

from .stock_app import get_bars
from .stock_features import features

FEATS = [
    "ret_1d_pct",
    "ret_5d_pct",
    "ret_20d_pct",
    "ret_60d_pct",
    "vol_20d_pct",
    "vol_60d_pct",
    "atr14_pct",
    "rsi14",
    "vs_sma20_pct",
    "vs_sma50_pct",
    "pos_in_52w_range",
    "below_52w_high_pct",
    "volume_vs_20d",
]

# ---- departments: each maps one stock's features to a score (higher = more attractive) -----------
SPECIALISTS: dict[str, Callable[[dict], float]] = {
    "trend": lambda x: float(x["ret_20d_pct"] + 0.5 * x["vs_sma50_pct"]),
    "reversion": lambda x: float(-x["ret_5d_pct"] - 0.1 * (x["rsi14"] - 50)),
    "low_vol": lambda x: float(-x["vol_20d_pct"]),
}


def risk_veto(day: dict[str, dict], mult: float = 2.5) -> set[str]:
    """Risk officer: no new buys in a stock swinging more than `mult` times the day's median volatility."""
    med = st.median(x["vol_20d_pct"] for x in day.values())
    return {s for s, x in day.items() if med > 0 and x["vol_20d_pct"] > mult * med}


# ---- data ----------------------------------------------------------------------------------------
def build_panel(symbols: list[str], src, h: int = 5) -> list[dict]:
    rows = []
    for s in symbols:
        bars = get_bars(s, src)[0]
        for i in range(60, len(bars) - h):
            rows.append(
                {
                    "date": bars[i].d,
                    "symbol": s,
                    "x": features(bars[: i + 1]),
                    "fwd": (bars[i + h].c / bars[i].c - 1) * 100,
                }
            )
    dates = sorted({str(r["date"]) for r in rows})
    di = {d: k for k, d in enumerate(dates)}
    for r in rows:
        r["di"] = di[r["date"]]
    return rows


def _zx(rows: list[dict], names: list[str]) -> np.ndarray:
    """Cross-sectional z-score per date, so the model learns rankings, not market moves."""
    X = np.array([[r["x"].get(n, 0.0) for n in names] for r in rows], dtype=float)
    out = np.zeros_like(X)
    by: dict[str, list[int]] = {}
    for k, r in enumerate(rows):
        by.setdefault(r["date"], []).append(k)
    for idx in by.values():
        sub = X[idx]
        sd = sub.std(axis=0)
        out[idx] = (sub - sub.mean(axis=0)) / np.where(sd > 0, sd, 1)
    return np.nan_to_num(out)


def _target(rows: list[dict]) -> np.ndarray:
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r["fwd"])
    mean = {d: sum(v) / len(v) for d, v in by.items()}
    return np.array([r["fwd"] - mean[r["date"]] for r in rows])


def _ic(scores: dict[str, dict[str, float]], fwd: dict) -> list[float]:
    out = []
    for d, sc in scores.items():
        syms = list(sc)
        if len(syms) < 5:
            continue
        a = np.argsort(np.argsort([sc[s] for s in syms]))
        b = np.argsort(np.argsort([fwd[(d, s)] for s in syms]))
        c = np.corrcoef(a, b)[0, 1]
        out.append(0.0 if not np.isfinite(c) else float(c))
    return out


def walk_forward(rows: list[dict], h: int = 5, model: str = "ridge", names: list[str] | None = None, _spy=None) -> dict:
    """Train on earlier years (minus a purge gap of h days), score the next year. Never sees test labels."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge

    names = names or sorted({k for r in rows for k in r["x"]})
    years = sorted({r["date"][:4] for r in rows})
    scores: dict[str, dict[str, float]] = {}
    for y in years[1:]:
        test = [r for r in rows if r["date"][:4] == y]
        start = min(r["di"] for r in test)
        train = [r for r in rows if r["di"] + h <= start - 0 and r["date"][:4] < y]
        if len(train) < 200 or not test:
            continue
        if _spy:
            _spy(train, test)
        m = (
            Ridge(alpha=10.0)
            if model == "ridge"
            else HistGradientBoostingRegressor(max_depth=3, max_iter=150, learning_rate=0.05)
        )
        m.fit(_zx(train, names), _target(train))
        pred = m.predict(_zx(test, names))
        for r, p in zip(test, pred, strict=True):
            scores.setdefault(r["date"], {})[r["symbol"]] = float(p)
    fwd = {(r["date"], r["symbol"]): r["fwd"] for r in rows}
    ics = _ic(scores, fwd)
    return {
        "scores": scores,
        "oos_ic_mean": round(float(np.mean(ics)), 4) if ics else 0.0,
        "test_years": sorted({d[:4] for d in scores}),
    }


# ---- paper portfolio -----------------------------------------------------------------------------
def paper_portfolio(
    scores: dict[str, dict[str, float]],
    fwd: dict,
    dates: list[str],
    cost: float = 0.10,
    vetoes: dict[str, set[str]] | None = None,
    periods_per_year: float = 50.0,
    log: bool = False,
) -> dict:
    """Rebalance on each date in `dates` (space them h days apart): hold the top fifth, equal weight.
    Cost is charged in percent on the traded fraction of the portfolio. Benchmark: all stocks, equal weight."""
    held: set[str] = set()
    eq, bench, peak, mdd = 1.0, 1.0, 1.0, 0.0
    rets, turns, trades = [], [], []
    for d in dates:
        sc = scores.get(d)
        if not sc or len(sc) < 5:
            continue
        veto = (vetoes or {}).get(d, set())
        ranked = sorted((s for s in sc if s not in veto), key=lambda s: sc[s], reverse=True)
        k = max(1, len(sc) // 5)
        new = set(ranked[:k])
        turn = len(new.symmetric_difference(held)) / (2 * k)
        if log:
            trades.append({"date": d, "buy": sorted(new - held), "sell": sorted(held - new)})
        held = new
        r = sum(fwd[(d, s)] for s in new) / len(new) - turn * cost * 2
        b = sum(fwd[(d, s)] for s in sc) / len(sc)
        eq *= 1 + r / 100
        bench *= 1 + b / 100
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        rets.append(r - b)
        turns.append(turn)
    n = len(rets)
    sd = st.pstdev(rets) if n > 1 else 0.0
    out = {
        "periods": n,
        "strategy_total_pct": round((eq - 1) * 100, 3),
        "benchmark_total_pct": round((bench - 1) * 100, 3),
        "excess_per_period_pct": round(sum(rets) / n, 3) if n else 0.0,
        "excess_t": round(sum(rets) / n / (sd / math.sqrt(n)), 2) if n > 1 and sd > 0 else None,
        "sharpe": round((sum(rets) / n) / sd * math.sqrt(periods_per_year), 2) if n > 1 and sd > 0 else 0.0,
        "max_drawdown_pct": round(mdd * 100, 2),
        "turnover_avg": round(sum(turns) / n, 3) if n else 0.0,
        "beat_benchmark_share": round(sum(x > 0 for x in rets) / n, 3) if n else 0.0,
    }
    if log:
        out["log"] = trades
    return out


# ---- committee -----------------------------------------------------------------------------------
def desk_scores(rows: list[dict]) -> dict[str, dict[str, dict[str, float]]]:
    out: dict[str, dict[str, dict[str, float]]] = {n: {} for n in SPECIALISTS}
    for r in rows:
        for n, fn in SPECIALISTS.items():
            out[n].setdefault(r["date"], {})[r["symbol"]] = fn(r["x"])
    return out


def committee(rows: list[dict], h: int, desks: dict, ml: dict) -> dict:
    """Portfolio manager: learns how much to trust each desk from earlier years only (ridge stacking)."""
    stacked = []
    for r in rows:
        x = {n: desks[n][r["date"]][r["symbol"]] for n in desks}
        if r["date"] in ml and r["symbol"] in ml[r["date"]]:
            x["ml"] = ml[r["date"]][r["symbol"]]
        stacked.append({**r, "x": x})
    return walk_forward(stacked, h, "ridge", names=[*desks, "ml"])


def run_research(symbols: list[str], src, h: int = 5, cost: float = 0.10) -> dict:
    rows = build_panel(symbols, src, h)
    fwd = {(r["date"], r["symbol"]): r["fwd"] for r in rows}
    ridge, boost = walk_forward(rows, h, "ridge", FEATS), walk_forward(rows, h, "boost", FEATS)
    desks = desk_scores(rows)
    pm = committee(rows, h, desks, ridge["scores"])
    test_dates = sorted(ridge["scores"])[::h]  # non-overlapping rebalances, test years only
    by_day: dict[str, dict] = {}
    for r in rows:
        by_day.setdefault(r["date"], {})[r["symbol"]] = r["x"]
    vetoes = {d: risk_veto(by_day[d]) for d in test_dates}
    ppy = 252 / h

    def evaluate(scores, ic=None, veto=True):
        return {
            "oos_ic_mean": ic
            if ic is not None
            else round(float(np.mean(_ic({d: scores[d] for d in test_dates if d in scores}, fwd) or [0])), 4),
            "paper": paper_portfolio(scores, fwd, test_dates, cost, vetoes if veto else None, ppy),
        }

    out = {
        "symbols": len(symbols),
        "test_years": ridge["test_years"],
        "rebalances": len(test_dates),
        "h": h,
        "cost_pct": cost,
        "ridge": evaluate(ridge["scores"], ridge["oos_ic_mean"]),
        "boost": evaluate(boost["scores"], boost["oos_ic_mean"]),
        "committee": evaluate(pm["scores"], pm["oos_ic_mean"]),
        **{n: evaluate(desks[n]) for n in desks},
        "momentum": evaluate(desks["trend"]),
    }
    out["benchmark"] = {"total_pct": out["ridge"]["paper"]["benchmark_total_pct"]}
    best = max(("ridge", "boost", "committee", *desks), key=lambda n: out[n]["paper"]["excess_t"] or -9)
    t = out[best]["paper"]["excess_t"] or 0
    out["verdict"] = (
        f"Best desk '{best}' beat holding everything with t={t}. With {len(desks) + 3} strategies tried, "
        + (
            "that clears a corrected bar (about 2.6) but is still not proof."
            if t >= 2.6
            else "that is not enough: no desk reliably beats simply holding all stocks after costs."
        )
    )
    return out
