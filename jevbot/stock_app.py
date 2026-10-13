"""Local web app: analyze a stock with Jev, backtest honestly, keep a forward scoreboard.

Runs on 127.0.0.1 only. The API key is read from the environment by the server and never reaches the browser.
Paper only. This app cannot place orders.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import friendly_error
from .market_data import (
    Bar,
    check_symbol,
    demo_bars,
    fetch_bars_cached,
    file_symbols,
    load_csv,
    resolve_file,
)
from .stock_features import features, momentum_rule
from .stock_jev import ask, lean

log = logging.getLogger("jevbot.stock")
STATIC = Path(__file__).parent / "static"
ROUND_TRIP_COST_PCT = 0.10  # assumed cost per paper trade, in percent


def _client(model: str):
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

    return AsyncTypeSafeClient(model=model, retry=RetryPolicy(max_retries=1), timeout=10)


def _today() -> date:
    return date.today()


def stale_warning(last_bar: str, today: date | None = None) -> str | None:
    """A weekend plus a holiday can leave the last bar 4 days old. Older than that and the read is about the past."""
    age = ((today or _today()) - date.fromisoformat(last_bar)).days
    return None if age <= 4 else f"The latest price is {age} days old, so this read describes the past, not today."


VALID_SRC = ("live", "file", "demo")


def norm_src(x) -> str:
    """Data source: live prices, a CSV in data/, or fake demo prices. Booleans are the old demo flag."""
    if x is True:
        return "demo"
    if x is False or x is None:
        return "live"
    if x in VALID_SRC:
        return x
    raise ValueError("'src' must be live, file or demo.")


def get_bars(sym: str, src) -> tuple[list[Bar], str]:
    src = norm_src(src)
    if src == "demo":
        return demo_bars(sym), "demo"
    if src == "file":
        return load_csv(resolve_file(sym)), "file"
    return fetch_bars_cached(sym)


def _fwd(bars: list[Bar], i: int, h: int) -> float | None:
    return (bars[i + h].c / bars[i].c - 1) * 100 if i + h < len(bars) else None


# ---------------------------------------------------------------------------
def _read_log(log_path: str) -> list[dict]:
    p = Path(log_path)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _log_read(log_path: str, out: dict, a: dict, label: str) -> bool:
    """Append one prediction. One per (symbol, as-of day, horizon): re-asking the same question adds nothing."""
    key = (out["symbol"], out["asof"], out["h"])
    if any((q["symbol"], q["asof"], q["h"]) == key for q in _read_log(log_path)):
        return False
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(
            json.dumps(
                {
                    "id": uuid.uuid4().hex[:8],
                    "symbol": out["symbol"],
                    "asof": out["asof"],
                    "close": out["close"],
                    "h": out["h"],
                    "lean": label,
                    "p_up": a["p_up"],
                    "p_down": a["p_down"],
                    "model": a["model"],
                    "made_at": datetime.now(UTC).isoformat(),
                }
            )
            + "\n"
        )
    return True


async def analyze(asker, sym: str, h: int, demo, log_path: str | None) -> dict:
    src = norm_src(demo)
    bars, source = await asyncio.to_thread(get_bars, sym, src)  # network I/O off the event loop
    snap = features(bars)
    a = await asker(snap, h)
    label, why = lean(a)
    out = {
        "symbol": sym,
        "h": h,
        "asof": bars[-1].d,
        "close": round(bars[-1].c, 2),
        "source": source,
        "demo": src == "demo",
        "src": src,
        "features": snap,
        "jev": a,
        "lean": label,
        "why": why,
        "warning": stale_warning(bars[-1].d) if not demo else None,
        "series": [[b.d, round(b.c, 2)] for b in bars[-130:]],
    }
    out["logged"] = False
    if log_path and src == "live":  # fake or historical-file prices never enter the forward scoreboard
        out["logged"] = _log_read(log_path, out, a, label)
    return out


def _bins(pairs: list[tuple[float, int]], edges=(0.0, 0.4, 0.5, 0.6, 0.7, 1.0001)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:], strict=False):
        sel = [y for p, y in pairs if lo <= p < hi]
        if sel:
            out.append(
                {
                    "range": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                    "n": len(sel),
                    "said_up": round((lo + min(hi, 1.0)) / 2, 2),
                    "was_up": round(sum(sel) / len(sel), 3),
                }
            )
    return out


MIN_INDEPENDENT = 10  # fewer independent signals than this and we refuse to call anything


def _ranks(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def rank_stats(rows: list[dict]) -> dict:
    """Threshold-free test: do days Jev rates more bullish see better forward moves? Spearman correlation of p_up
    with the realized move, and the average move in Jev's most vs least bullish 20% of days.
    Overlapping windows inflate how convincing this looks, so treat it as a lead, not a result."""
    if len(rows) < 50:
        return {"rank_corr": None, "top20_avg_pct": None, "bottom20_avg_pct": None}
    p, f = [r["p_up"] for r in rows], [r["fwd"] for r in rows]
    ra, rb = _ranks(p), _ranks(f)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    order = sorted(range(len(rows)), key=lambda i: p[i])
    q = len(rows) // 5
    top, bot = [f[i] for i in order[-q:]], [f[i] for i in order[:q]]
    return {
        "rank_corr": round(num / den, 3) if den else 0.0,
        "top20_avg_pct": round(sum(top) / q, 3),
        "bottom20_avg_pct": round(sum(bot) / q, 3),
    }


def non_overlapping(rows: list[dict], h: int, key: str = "lean", target: str = "LEAN UP") -> list[dict]:
    """Signals closer than h days share most of their outcome window, so they are not independent evidence.
    Greedy pick of signals at least h days apart, with a separate clock per symbol."""
    picked: list[dict] = []
    last: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: (r.get("symbol", ""), r["i"])):
        sym = r.get("symbol", "")
        if r[key] == target and (sym not in last or r["i"] - last[sym] >= h):
            picked.append(r)
            last[sym] = r["i"]
    return picked


def summarize(rows: list[dict], h: int = 5, cost: float = ROUND_TRIP_COST_PCT, key: str = "lean") -> dict:
    """rows carry the day index `i`, a call under `key`, p_up and the realized forward move `fwd` in percent.
    Hit rate, average move and the z score use only independent (non-overlapping) signals."""
    rows = [{**r, "i": r.get("i", k)} for k, r in enumerate(rows)]
    n = len(rows)
    base = sum(r["fwd"] > 0 for r in rows) / n if n else 0.0
    longs = [r for r in rows if r[key] == "LEAN UP"]
    ind = non_overlapping(rows, h, key)
    downs = [r for r in rows if r[key] == "LEAN DOWN"]
    hit: float | None = None
    z: float | None = None
    avg_ind: float | None = None
    if ind:
        hit = sum(r["fwd"] > 0 for r in ind) / len(ind)
        avg_ind = sum(r["fwd"] for r in ind) / len(ind)
        if 0 < base < 1:
            z = (hit - base) / math.sqrt(base * (1 - base) / len(ind))
    return {
        "days": n,
        "baseline_up_rate": round(base, 3),
        "avg_return_all_days_pct": round(sum(r["fwd"] for r in rows) / n, 3) if n else 0.0,
        "long_signals": len(longs),
        "independent_long": len(ind),
        "long_hit_rate": None if hit is None else round(hit, 3),
        "avg_return_after_long_pct": None if avg_ind is None else round(avg_ind, 3),
        "avg_return_after_long_net_pct": None if avg_ind is None else round(avg_ind - cost, 3),
        "down_signals": len(downs),
        "avg_return_after_down_pct": round(sum(r["fwd"] for r in downs) / len(downs), 3) if downs else None,
        "sat_out": n - len(longs) - len(downs),
        "z_vs_baseline": None if z is None else round(z, 2),
        **(rank_stats(rows) if key == "lean" else {"rank_corr": None, "top20_avg_pct": None, "bottom20_avg_pct": None}),
        "cost_pct": cost,
        "calibration": _bins([(r["p_up"], int(r["fwd"] > 0)) for r in rows]),
        "verdict": _verdict(len(ind), avg_ind, cost, z),
    }


def _verdict(n_ind: int, avg_ind: float | None, cost: float, z: float | None) -> str:
    if n_ind < MIN_INDEPENDENT:
        return (
            f"Too few independent signals ({n_ind}, need {MIN_INDEPENDENT}) to say anything. "
            "Sitting out most days is not a failure."
        )
    if z is not None and z >= 2 and avg_ind is not None and avg_ind - cost > 0:
        return (
            "Promising, not proof. Signals beat the baseline and survive costs. But the sample is small and Jev may "
            "have seen this period in training. Only the forward scoreboard counts."
        )
    return "No demonstrated edge here. Signals do not clearly beat simply holding, or do not survive costs."


def compare(jev: dict, rule: dict) -> str:
    if jev["independent_long"] < MIN_INDEPENDENT:
        return "Not enough Jev signals to compare against the simple rule."
    jn, rn = jev["avg_return_after_long_net_pct"], rule["avg_return_after_long_net_pct"]
    if rule["independent_long"] < MIN_INDEPENDENT or rn is None:
        return "The simple rule gave too few signals to compare."
    if jn <= jev["avg_return_all_days_pct"]:
        return "Jev's signals did not beat simply holding the stock on every day, even before comparing with the rule."
    if jn > rn and (jev["long_hit_rate"] or 0) > (rule["long_hit_rate"] or 0):
        return (
            "Jev beat the simple momentum rule on this sample. "
            "Check that it holds on other tickers before believing it."
        )
    return "Jev did not beat the simple momentum rule. If a 3-line rule does as well, Jev adds nothing here."


async def backtest(
    asker, sym: str, h: int, n: int, demo, conc: int = 6, cost: float = ROUND_TRIP_COST_PCT, skip: int = 0
) -> dict:
    src = norm_src(demo)
    bars, source = await asyncio.to_thread(get_bars, sym, src)
    n = max(10, min(n, 250))
    skip = max(0, skip)
    end = len(bars) - h - skip
    if end - 60 < 10:
        raise ValueError(f"Not enough history: {len(bars)} days cannot cover a test that skips the last {skip} days.")
    idx = list(range(max(60, end - n), end))
    sem = asyncio.Semaphore(conc)

    async def one(i):
        async with sem:
            try:
                snap = features(bars[: i + 1])  # data up to day i only
                a = await asker(snap, h)
            except Exception as e:
                return {"error": repr(e)}
            lab, _ = lean(a)
            return {
                "i": i,
                "asof": bars[i].d,
                "lean": lab,
                "rule_lean": momentum_rule(snap),
                "p_up": a["p_up"],
                "fwd": _fwd(bars, i, h),
                "model": a["model"],
            }

    res = await asyncio.gather(*(one(i) for i in idx))
    ok = [r for r in res if "error" not in r]
    jev, rule = summarize(ok, h, cost), summarize(ok, h, cost, key="rule_lean")
    return {
        "symbol": sym,
        "h": h,
        "source": source,
        "demo": src == "demo",
        "src": src,
        "skip": skip,
        "errors": len(res) - len(ok),
        "cost_pct": cost,
        "stats": jev,
        "rule_stats": rule,
        "comparison": compare(jev, rule),
        "rows": ok[-40:],
        "caveat": "Jev may have seen this price history during training, so this test can look better than reality.",
    }


MAX_WATCHLIST = 25


async def scan(asker, symbols: list[str], h: int, demo, log_path: str | None, conc: int = 4) -> dict:
    src = norm_src(demo)
    """Ask Jev about many tickers. One bad ticker never sinks the rest. Capped to protect your credit."""
    syms = [s.strip() for s in symbols if s.strip()]
    truncated = len(syms) > MAX_WATCHLIST
    syms = syms[:MAX_WATCHLIST]
    sem = asyncio.Semaphore(conc)

    async def one(raw):
        async with sem:
            try:
                d = await analyze(asker, check_symbol(raw), h, src, log_path)
                j = d["jev"]
                return {
                    "symbol": d["symbol"],
                    "lean": d["lean"],
                    "p_up": j["p_up"],
                    "p_down": j["p_down"],
                    "entry_score": j["entry_score"],
                    "vol_regime": j["vol_regime"],
                    "close": d["close"],
                    "asof": d["asof"],
                    "warning": d.get("warning"),
                }
            except Exception as e:
                return {"symbol": re.sub(r"[^A-Z0-9.\-]", "?", raw.upper())[:12], "error": friendly_error(e)}

    rows = await asyncio.gather(*(one(s) for s in syms))
    ok = sorted((r for r in rows if "error" not in r), key=lambda r: -r["p_up"])
    return {
        "rows": ok + [r for r in rows if "error" in r],
        "truncated": truncated,
        "h": h,
        "demo": src == "demo",
        "src": src,
    }


def _resolve(preds: list[dict], demo: bool = False) -> list[dict]:
    """Attach the realized move to each prediction whose look-ahead period has passed."""
    cache: dict[str, list[Bar]] = {}
    out = []
    for q in preds:
        fwd, idx = None, None
        try:
            bars = cache.setdefault(q["symbol"], get_bars(q["symbol"], demo)[0])
            idx = next(k for k, b in enumerate(bars) if b.d == q["asof"])
            fwd = _fwd(bars, idx, q["h"])
        except Exception as e:  # an unresolvable prediction stays pending; say why in the log, not on the page
            log.debug("could not resolve %s %s: %r", q.get("symbol"), q.get("asof"), e)
        out.append({**q, "fwd": fwd, "i": idx})
    return out


def scoreboard(log_path: str, demo: bool = False) -> dict:
    preds = _read_log(log_path)
    res = _resolve(preds, demo)
    done = [r for r in res if r["fwd"] is not None]
    horizon = max((r["h"] for r in done), default=5)
    return {
        "made": len(preds),
        "pending": len(res) - len(done),
        "stats": summarize(done, horizon) if done else None,
        "recent": done[-15:],
    }


CSV_COLUMNS = [
    "made_at",
    "symbol",
    "asof",
    "h",
    "lean",
    "p_up",
    "p_down",
    "close",
    "model",
    "status",
    "fwd_pct",
    "correct",
]


def scoreboard_csv(log_path: str, demo: bool = False) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in _resolve(_read_log(log_path), demo):
        done = r["fwd"] is not None
        verdict = ""
        if done and r["lean"] in ("LEAN UP", "LEAN DOWN"):
            verdict = "yes" if (r["fwd"] > 0) == (r["lean"] == "LEAN UP") else "no"
        w.writerow(
            {
                **r,
                "status": "resolved" if done else "pending",
                "fwd_pct": "" if not done else round(r["fwd"], 3),
                "correct": verdict,
            }
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding window. Keeps a runaway page or script from spending your Jev credit."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: list[float] = []
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = self.clock()
            self.hits = [t for t in self.hits if now - t < self.window]
            if len(self.hits) >= self.limit:
                return False
            self.hits.append(now)
            return True


def _num(q: dict, name: str, cast, default):
    """Parse a query number or raise a message a person can act on. Rejects nan and inf."""
    raw = q.get(name)
    if raw is None:
        return default
    try:
        val = cast(raw)
        if cast is float and not math.isfinite(val):
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(f"'{name}' must be a plain number, got '{raw[:20]}'.") from None
    return val


EXPENSIVE = ("/api/analyze", "/api/backtest", "/api/scan")  # these spend Jev credit
PUBLIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
MAX_QUERY_VALUE = 400


def make_handler(asker, model: str, log_path: str, has_key: bool, rate_limit: tuple[int, float] = (60, 60.0)):
    limiter = RateLimiter(*rate_limit)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _allowed_hosts(self) -> set[str]:
            port = int(self.server.server_address[1])  # type: ignore[index]
            return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

        def _guard(self, path: str) -> bool:
            """Reject DNS rebinding (bad Host), cross-site pages (bad Origin) and header-less API calls."""
            if self.headers.get("Host", "") not in self._allowed_hosts():
                self._json({"error": "Forbidden host."}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin.removeprefix("http://") not in self._allowed_hosts():
                self._json({"error": "Forbidden origin."}, 403)
                return False
            if path.startswith("/api/") and path != "/api/scoreboard.csv" and self.headers.get("X-Jevbot") != "1":
                self._json({"error": "Missing X-Jevbot header."}, 403)
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if not self._guard(u.path):
                return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if any(len(v) > MAX_QUERY_VALUE for v in q.values()):
                    raise ValueError("That request is too long.")
                if u.path in PUBLIC_FILES:
                    name, ctype = PUBLIC_FILES[u.path]
                    return self._send(200, (STATIC / name).read_bytes(), ctype)
                if u.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if u.path == "/api/files":
                    return self._json({"symbols": file_symbols()})
                if u.path == "/api/status":
                    return self._json({"has_key": has_key, "model": model})
                if u.path == "/api/scoreboard.csv":
                    return self._send(
                        200,
                        scoreboard_csv(log_path, q.get("demo") == "1").encode(),
                        "text/csv; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="jev_scoreboard.csv"'},
                    )
                if u.path == "/api/scoreboard":
                    return self._json(scoreboard(log_path, q.get("demo") == "1"))
                if u.path in EXPENSIVE:
                    if not has_key:
                        return self._json(
                            {"error": "TYPESAFE_API_KEY is not set on the machine running this app."}, 400
                        )
                    if not limiter.allow():
                        return self._json({"error": "Too many requests. Wait a minute."}, 429)
                    demo = norm_src(q.get("src") or ("demo" if q.get("demo") == "1" else "live"))
                    h = max(1, min(_num(q, "h", int, 5), 20))
                    if u.path == "/api/analyze":
                        return self._json(
                            asyncio.run(analyze(asker, check_symbol(q.get("symbol", "")), h, demo, log_path))
                        )
                    if u.path == "/api/backtest":
                        cost = max(0.0, min(_num(q, "cost", float, ROUND_TRIP_COST_PCT), 5.0))
                        skip = max(0, _num(q, "skip", int, 0))
                        return self._json(
                            asyncio.run(
                                backtest(
                                    asker,
                                    check_symbol(q.get("symbol", "")),
                                    h,
                                    _num(q, "n", int, 120),
                                    demo,
                                    cost=cost,
                                    skip=skip,
                                )
                            )
                        )
                    return self._json(asyncio.run(scan(asker, q.get("symbols", "").split(","), h, demo, log_path)))
                self._send(404, b"not found", "text/plain")
            except ValueError as e:
                self._json({"error": friendly_error(e)}, 400)
            except Exception as e:
                self._json({"error": friendly_error(e)}, 502)

    return H


def make_asker(model: str):
    async def asker(snap, h):
        # one client per event loop: asyncio.run() makes a fresh loop per request
        return await ask(_client(model), model, snap, h)

    return asker


DEFAULT_MODEL = "jev-1.13.0"  # keep equal to config.toml [jev].model (a test enforces it)


def resolve_model(model: str) -> str:
    """Never run on a floating alias: a silent model upgrade invalidates every threshold you tuned."""
    return model if model not in ("", "jev-latest") else DEFAULT_MODEL


DEFAULT_WATCHLIST = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,JPM,XOM,JNJ,WMT"


def serve(model: str, port: int = 8000, log_path: str = "runs/predictions.jsonl", open_browser: bool = True) -> None:
    import webbrowser

    model = resolve_model(model)
    key = bool(os.environ.get("TYPESAFE_API_KEY"))

    handler, srv = make_handler(make_asker(model), model, log_path, key), None
    for p in range(port, port + 11):  # port busy? try the next ones
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError:
            print(f"Port {p} is busy, trying the next one...")
    if srv is None:
        raise SystemExit(f"No free port between {port} and {port + 10}. Close other programs or pass --port.")
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"\nJev Stock Check is running at {url}")
    print(f"Model {model} | API key {'found' if key else 'NOT SET (set TYPESAFE_API_KEY, then restart)'}")
    print("Keep this window open while you use it. Press Ctrl+C to stop.\n")
    if open_browser:
        webbrowser.open(url)  # server is already listening, so no race
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
