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
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")


@dataclass(frozen=True)
class Bar:
    d: str  # YYYY-MM-DD
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
    if not url.startswith("https://"):  # only ever fetch over TLS, never file: or custom schemes
        raise ValueError("refusing to fetch a non-https URL")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 jevbot-prototype"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310  (https-only, checked above)
        return r.read()


def _yahoo(sym: str) -> list[Bar]:
    raw = json.loads(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2y"))
    res = raw["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out: list[Bar] = []
    for i, ts in enumerate(res["timestamp"]):
        vals = [q[k][i] for k in ("open", "high", "low", "close", "volume")]
        if any(v is None for v in vals):
            continue
        out.append(Bar(datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d"), *map(float, vals)))
    return out


def _stooq(sym: str) -> list[Bar]:
    txt = _get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d").decode()
    rows = list(csv.DictReader(io.StringIO(txt)))
    return [
        Bar(
            r["Date"],
            float(r["Open"]),
            float(r["High"]),
            float(r["Low"]),
            float(r["Close"]),
            float(r.get("Volume") or 0),
        )
        for r in rows
        if r.get("Close")
    ][-520:]


_sleep = time.sleep
_clock = time.monotonic


def _transient(e: BaseException) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or e.code >= 500
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def with_retry(fn, tries: int = 3, base: float = 0.5, sleep=None):
    """Retry transient failures (network, 429, 5xx) with doubling backoff. Permanent errors raise at once."""
    sleep = sleep or _sleep
    for k in range(tries):
        try:
            return fn()
        except Exception as e:
            if not _transient(e) or k == tries - 1:
                raise
            sleep(base * 2**k)


def validate_bars(bars: list[Bar]) -> None:
    """Refuse data we cannot trust, rather than feed it to the model."""
    if len(bars) < 80:
        raise ValueError(f"need at least 80 daily bars, got {len(bars)}")
    dates = [b.d for b in bars]
    if dates != sorted(dates):
        raise ValueError("bars are not in date order")
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate dates in price data")
    if any(min(b.o, b.h, b.l, b.c) <= 0 for b in bars):
        raise ValueError("price data contains zero or negative prices")


def fetch_bars(sym: str) -> tuple[list[Bar], str]:
    errs = []
    for name, fn in (("yahoo", _yahoo), ("stooq", _stooq)):
        try:
            bars = with_retry(lambda fn=fn: fn(sym))
            validate_bars(bars)
            return bars, name
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise RuntimeError("Could not get price data (" + "; ".join(errs) + ")")


_cache: dict[str, tuple[float, tuple[list[Bar], str]]] = {}
_locks: dict[str, threading.Lock] = {}
_lock = threading.Lock()  # guards the two dicts only, never held during a fetch


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _locks.clear()


def fetch_bars_cached(sym: str, ttl: float = 900.0) -> tuple[list[Bar], str]:
    """15 minute cache so a scan or backtest never hammers a free source. Failures are not cached.
    One lock per symbol: a burst for one ticker makes one fetch, different tickers fetch in parallel."""
    with _lock:
        sym_lock = _locks.setdefault(sym, threading.Lock())
    with sym_lock:
        with _lock:
            hit = _cache.get(sym)
        if hit and _clock() - hit[0] < ttl:
            return hit[1]
        res = fetch_bars(sym)
        with _lock:
            _cache[sym] = (_clock(), res)
        return res


def demo_bars(sym: str, n: int = 420) -> list[Bar]:
    """Synthetic but deterministic per ticker. For trying the UI only. Prices are NOT real."""
    rnd = random.Random(zlib.crc32(sym.encode()))
    price, drift = 100 * (0.5 + rnd.random()), 0.0
    out: list[Bar] = []
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
        hi, lo = max(o, price) * (1 + abs(rnd.gauss(0, 0.004))), min(o, price) * (1 - abs(rnd.gauss(0, 0.004)))
        out.append(Bar(day.isoformat(), o, hi, lo, price, rnd.uniform(0.7, 1.4) * 5e6))
    return out
