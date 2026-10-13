"""Plain arithmetic on price history. Uses only bars up to and including the last one given."""
from __future__ import annotations

import math
import statistics as st

from .market_data import Bar


def _sig(x: float, n: int = 4) -> float:
    return 0.0 if x == 0 or not math.isfinite(x) else float(f"{x:.{n}g}")


def _rsi(c: list[float], n: int = 14) -> float:
    d = [c[i] - c[i - 1] for i in range(len(c) - n, len(c))]
    gain, loss = sum(x for x in d if x > 0) / n, -sum(x for x in d if x < 0) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def features(bars: list[Bar]) -> dict:
    if len(bars) < 60:
        raise ValueError("need at least 60 daily bars")
    c = [b.c for b in bars]
    last = c[-1]
    lr = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    tr = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(bars, bars[1:])]
    win = c[-252:]
    f = {
        "ret_1d_pct": (last / c[-2] - 1) * 100,
        "ret_5d_pct": (last / c[-6] - 1) * 100,
        "ret_20d_pct": (last / c[-21] - 1) * 100,
        "ret_60d_pct": (last / c[-61] - 1) * 100,
        "vol_20d_pct": st.pstdev(lr[-20:]) * 100,
        "vol_60d_pct": st.pstdev(lr[-60:]) * 100,
        "atr14_pct": sum(tr[-14:]) / 14 / last * 100,
        "rsi14": _rsi(c),
        "vs_sma20_pct": (last / (sum(c[-20:]) / 20) - 1) * 100,
        "vs_sma50_pct": (last / (sum(c[-50:]) / 50) - 1) * 100,
        "pos_in_52w_range": (last - min(win)) / (max(win) - min(win)) if max(win) > min(win) else 0.5,
        "below_52w_high_pct": (last / max(win) - 1) * 100,
        "volume_vs_20d": bars[-1].v / (sum(b.v for b in bars[-20:]) / 20) if bars[-1].v else 1.0,
    }
    if len(c) >= 200:
        f["vs_sma200_pct"] = (last / (sum(c[-200:]) / 200) - 1) * 100
    return {k: _sig(v) for k, v in f.items()}
