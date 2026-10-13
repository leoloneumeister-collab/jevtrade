"""Cross-stock ranking test: does Jev rank stocks against each other better than chance, and better than a free rule?

Each day, every stock in the universe gets Jev's P(up). Stocks are ranked by it. We then look at what the top fifth and
the bottom fifth actually did over the next h days. Because every day compares stocks with each other, the market's
overall drift and Jev's overall bullish or bearish mood cancel out. That is a fairer test than one stock at a time.
"""

from __future__ import annotations

import asyncio
import math
import zlib

from .errors import friendly_error
from .market_data import Bar, check_symbol
from .stock_app import _ranks, get_bars, norm_src
from .stock_features import features

MIN_STOCKS = 5  # per day; fewer and "top fifth vs bottom fifth" means nothing
MIN_INDEPENDENT_DATES = 20
MAX_SYMBOLS = 40
MAX_CALLS = 6000  # Jev calls per test: protects your credit and your patience


def _t(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return None if var <= 0 else m / math.sqrt(var / n)


def _spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def cross_stats(by_date: dict[str, list[dict]], h: int, cost: float, key: str = "p_up") -> dict:
    """by_date maps a date to rows {symbol, p_up, mom, fwd}. `key` picks the score being tested.
    The rank link (IC) is averaged over all days; t statistics and the spread use only every h-th day so that
    overlapping forward windows are not counted as separate evidence."""
    dates = sorted(d for d, rows in by_date.items() if len(rows) >= MIN_STOCKS)
    per = []
    for d in dates:
        rows = by_date[d]
        n = len(rows)
        score, fwd = [r[key] for r in rows], [r["fwd"] for r in rows]
        order = sorted(range(n), key=lambda i: (score[i], zlib.crc32(f"{d}{rows[i]['symbol']}".encode())))
        buckets: list[list[float]] = [[] for _ in range(5)]
        for pos, i in enumerate(order):
            buckets[min(4, pos * 5 // n)].append(fwd[i])
        qm = [sum(b) / len(b) for b in buckets]
        per.append({"n": n, "ic": _spearman(score, fwd), "q": qm, "spread": qm[4] - qm[0], "all": sum(fwd) / n})
    ind = per[::h]
    if not per:
        return {
            "dates": 0,
            "independent_dates": 0,
            "avg_stocks": 0,
            "ic_mean": 0.0,
            "ic_t": None,
            "spread_mean_pct": 0.0,
            "spread_t": None,
            "spread_net_pct": 0.0,
            "quintiles": [],
            "universe_avg_pct": 0.0,
        }
    spread = sum(p["spread"] for p in ind) / len(ind)
    r3 = lambda x: None if x is None else round(x, 3)  # noqa: E731
    return {
        "dates": len(per),
        "independent_dates": len(ind),
        "avg_stocks": round(sum(p["n"] for p in per) / len(per), 1),
        "ic_mean": r3(sum(p["ic"] for p in per) / len(per)),
        "ic_t": r3(_t([p["ic"] for p in ind])),
        "spread_mean_pct": r3(spread),
        "spread_t": r3(_t([p["spread"] for p in ind])),
        "spread_net_pct": r3(spread - 2 * cost),  # long the top fifth, short the bottom fifth: two legs
        "quintiles": [r3(sum(p["q"][k] for p in ind) / len(ind)) for k in range(5)],
        "universe_avg_pct": r3(sum(p["all"] for p in ind) / len(ind)),
    }


def cross_verdict(jev: dict, mom: dict) -> str:
    n = jev["independent_dates"]
    if n < MIN_INDEPENDENT_DATES:
        return (
            f"Too few independent days ({n}, need {MIN_INDEPENDENT_DATES}) to say anything. "
            "Use more days or a longer look-ahead window."
        )
    t = jev["ic_t"]
    if t is None or t < 2 or jev["spread_net_pct"] <= 0:
        return "No demonstrated ranking skill: Jev's ranking is not reliably better than chance after costs."
    if (mom["ic_mean"] or 0) >= (jev["ic_mean"] or 0):
        return (
            "Jev ranks stocks better than chance, but no better than a free momentum ranking, "
            "so it adds nothing over the rule here."
        )
    return (
        "Promising, not proof. Jev ranked stocks better than chance and better than momentum, after costs. "
        "But Jev may have seen this period in training, and trying many tests makes one lucky result likely. "
        "Only the forward scoreboard counts."
    )


async def cross_test(asker, symbols: list[str], h: int, n: int, demo, cost: float, skip: int, conc: int = 8) -> dict:
    src = norm_src(demo)
    skipped: list[str] = []
    syms: list[str] = []
    for raw in symbols:
        if not raw.strip():
            continue
        try:
            s = check_symbol(raw)
        except ValueError as e:
            skipped.append(f"{raw.strip()[:12]}: {friendly_error(e)}")
            continue
        if s not in syms:
            syms.append(s)
    if len(syms) > MAX_SYMBOLS:
        raise ValueError(f"Use at most {MAX_SYMBOLS} stocks (got {len(syms)}).")

    loaded = await asyncio.gather(*(asyncio.to_thread(get_bars, s, src) for s in syms), return_exceptions=True)
    bars: dict[str, list[Bar]] = {}
    for s, res in zip(syms, loaded, strict=True):
        if isinstance(res, BaseException):
            skipped.append(f"{s}: {friendly_error(res)}")
        else:
            bars[s] = res[0]
    if len(bars) < MIN_STOCKS:
        raise ValueError(
            f"Need at least {MIN_STOCKS} stocks with data (got {len(bars)}). Skipped: {'; '.join(skipped) or 'none'}"
        )

    index = {s: {b.d: i for i, b in enumerate(bl)} for s, bl in bars.items()}
    members: dict[str, list[str]] = {}
    for s, idx in index.items():
        for d, i in idx.items():
            if i >= 60 and i + h < len(bars[s]):
                members.setdefault(d, []).append(s)
    eligible = sorted(d for d, ss in members.items() if len(ss) >= MIN_STOCKS)
    skip = max(0, skip)
    usable = eligible[: len(eligible) - skip] if skip else eligible
    n = max(10, min(n, 250))
    chosen = usable[-n:]
    if len(chosen) < 10:
        raise ValueError(
            f"Not enough overlapping history: only {len(usable)} usable days where {MIN_STOCKS}+ stocks have data."
        )
    calls = sum(len(members[d]) for d in chosen)
    if calls > MAX_CALLS:
        raise ValueError(f"That would need {calls} Jev calls (limit {MAX_CALLS}). Use fewer stocks or days.")

    sem = asyncio.Semaphore(conc)

    async def one(d: str, s: str):
        i = index[s][d]
        async with sem:
            try:
                snap = features(bars[s][: i + 1])  # data up to this day only
                a = await asker(snap, h)
            except Exception:
                return None
            return d, {
                "symbol": s,
                "p_up": a["p_up"],
                "mom": snap["ret_20d_pct"],
                "fwd": (bars[s][i + h].c / bars[s][i].c - 1) * 100,
            }

    res = await asyncio.gather(*(one(d, s) for d in chosen for s in members[d]))
    by_date: dict[str, list[dict]] = {}
    for r in res:
        if r:
            by_date.setdefault(r[0], []).append(r[1])
    jev, mom = cross_stats(by_date, h, cost, "p_up"), cross_stats(by_date, h, cost, "mom")
    return {
        "src": src,
        "demo": src == "demo",
        "h": h,
        "cost_pct": cost,
        "skip": skip,
        "n_dates": len(chosen),
        "n_stocks": len(bars),
        "symbols": sorted(bars),
        "skipped": skipped,
        "calls": calls,
        "errors": sum(1 for r in res if r is None),
        "first_date": chosen[0],
        "last_date": chosen[-1],
        "jev": jev,
        "momentum": mom,
        "verdict": cross_verdict(jev, mom),
        "caveat": "Jev may have seen this price history during training, so a good result here can be flattering. "
        "A bad result is more trustworthy than a good one.",
    }
