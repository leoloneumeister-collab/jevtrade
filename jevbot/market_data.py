"""Free daily stock bars. Tries Yahoo's chart endpoint, then Stooq's CSV. Demo bars work offline.

Both sources are free and unofficial/keyless: they can change or rate-limit without notice.
"""
from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")


@dataclass(frozen=True)
class Bar:
    d: str          # YYYY-MM-DD
    o: float
    h: float
    l: float
    c: float
    v: float


def check_symbol(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValueError("Ticker looks invalid. Use letters, like AAPL or BRK-B.")
    return sym


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 jevbot-prototype"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def _yahoo(sym: str) -> list[Bar]:
    raw = json.loads(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2y"))
    res = raw["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        vals = [q[k][i] for k in ("open", "high", "low", "close", "volume")]
        if any(v is None for v in vals):
            continue
        out.append(Bar(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"), *map(float, vals)))
    return out


def _stooq(sym: str) -> list[Bar]:
    txt = _get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d").decode()
    rows = list(csv.DictReader(io.StringIO(txt)))
    return [Bar(r["Date"], float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]),
                float(r.get("Volume") or 0)) for r in rows if r.get("Close")][-520:]


def fetch_bars(sym: str) -> tuple[list[Bar], str]:
    errs = []
    for name, fn in (("yahoo", _yahoo), ("stooq", _stooq)):
        try:
            bars = fn(sym)
            if len(bars) >= 80:
                return bars, name
            errs.append(f"{name}: only {len(bars)} bars")
        except Exception as e:
            errs.append(f"{name}: {e!r}")
    raise RuntimeError("Could not get price data (" + "; ".join(errs) + ")")


def demo_bars(sym: str, n: int = 420) -> list[Bar]:
    """Synthetic but deterministic per ticker. For trying the UI only. Prices are NOT real."""
    rnd = random.Random(zlib.crc32(sym.encode()))
    price, drift, out = 100 * (0.5 + rnd.random()), 0.0, []
    day = date.today() - timedelta(days=int(n * 1.45))
    while len(out) < n:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        if rnd.random() < 0.02:
            drift = rnd.gauss(0, 0.0012)
        r = drift + rnd.gauss(0, 0.014)
        o = price
        price *= math.exp(r)
        hi, lo = max(o, price) * (1 + abs(rnd.gauss(0, .004))), min(o, price) * (1 - abs(rnd.gauss(0, .004)))
        out.append(Bar(day.isoformat(), o, hi, lo, price, rnd.uniform(0.7, 1.4) * 5e6))
    return out
