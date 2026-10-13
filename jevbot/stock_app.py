"""Local web app: analyze a stock with Jev, backtest honestly, keep a forward scoreboard.

Runs on 127.0.0.1 only. The API key is read from the environment by the server and never reaches the browser.
Paper only. This app cannot place orders.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .market_data import Bar, check_symbol, demo_bars, fetch_bars
from .stock_features import features
from .stock_jev import StockPolicy, ask, lean

STATIC = Path(__file__).parent / "static"
ROUND_TRIP_COST_PCT = 0.10          # assumed cost per paper trade, in percent


def _client(model: str):
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
    return AsyncTypeSafeClient(model=model, retry=RetryPolicy(max_retries=1), timeout=10)


def get_bars(sym: str, demo: bool) -> tuple[list[Bar], str]:
    return (demo_bars(sym), "demo") if demo else fetch_bars(sym)


def _fwd(bars: list[Bar], i: int, h: int) -> float | None:
    return (bars[i + h].c / bars[i].c - 1) * 100 if i + h < len(bars) else None


# ---------------------------------------------------------------------------
async def analyze(asker, sym: str, h: int, demo: bool, log_path: str | None) -> dict:
    bars, source = get_bars(sym, demo)
    snap = features(bars)
    a = await asker(snap, h)
    label, why = lean(a)
    out = {"symbol": sym, "h": h, "asof": bars[-1].d, "close": round(bars[-1].c, 2), "source": source,
           "demo": demo, "features": snap, "jev": a, "lean": label, "why": why,
           "series": [[b.d, round(b.c, 2)] for b in bars[-130:]]}
    if log_path and not demo:       # demo prices are fake, so they never enter the scoreboard
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps({"id": uuid.uuid4().hex[:8], "symbol": sym, "asof": out["asof"], "close": out["close"],
                                "h": h, "lean": label, "p_up": a["p_up"], "p_down": a["p_down"], "model": a["model"],
                                "made_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return out


def _bins(pairs: list[tuple[float, int]], edges=(0.0, 0.4, 0.5, 0.6, 0.7, 1.0001)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [y for p, y in pairs if lo <= p < hi]
        if sel:
            out.append({"range": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(sel), "said_up": round((lo + min(hi, 1.0)) / 2, 2),
                        "was_up": round(sum(sel) / len(sel), 3)})
    return out


def summarize(rows: list[dict]) -> dict:
    """rows: per-day dicts with lean, p_up, fwd (percent). Honest stats, with the caveats attached."""
    n = len(rows)
    ups = [r for r in rows if r["fwd"] > 0]
    base = len(ups) / n if n else 0.0
    longs = [r for r in rows if r["lean"] == "LEAN UP"]
    downs = [r for r in rows if r["lean"] == "LEAN DOWN"]
    hit = sum(r["fwd"] > 0 for r in longs) / len(longs) if longs else None
    z = None
    if longs and 0 < base < 1:
        z = (hit - base) / math.sqrt(base * (1 - base) / len(longs))
    avg_all = sum(r["fwd"] for r in rows) / n if n else 0.0
    avg_long = sum(r["fwd"] for r in longs) / len(longs) if longs else None
    return {
        "days": n, "baseline_up_rate": round(base, 3), "avg_return_all_days_pct": round(avg_all, 3),
        "long_signals": len(longs), "long_hit_rate": None if hit is None else round(hit, 3),
        "avg_return_after_long_pct": None if avg_long is None else round(avg_long, 3),
        "avg_return_after_long_net_pct": None if avg_long is None else round(avg_long - ROUND_TRIP_COST_PCT, 3),
        "down_signals": len(downs),
        "avg_return_after_down_pct": round(sum(r["fwd"] for r in downs) / len(downs), 3) if downs else None,
        "sat_out": n - len(longs) - len(downs), "z_vs_baseline": None if z is None else round(z, 2),
        "calibration": _bins([(r["p_up"], int(r["fwd"] > 0)) for r in rows]),
        "verdict": _verdict(n, len(longs), hit, base, z, avg_long),
    }


def _verdict(n, n_long, hit, base, z, avg_long) -> str:
    if n_long < 15:
        return f"Too few signals ({n_long}) to say anything. Jev mostly sat out, which is not a failure."
    if z is not None and z >= 2 and avg_long is not None and avg_long - ROUND_TRIP_COST_PCT > 0:
        return ("Promising, not proof. Signals beat the baseline and survive costs. But days overlap, the sample is small, "
                "and Jev may have seen this period in training. Only the forward scoreboard counts.")
    return ("No demonstrated edge here. Signals do not clearly beat simply holding, or do not survive costs.")


async def backtest(asker, sym: str, h: int, n: int, demo: bool, conc: int = 6) -> dict:
    bars, source = get_bars(sym, demo)
    n = max(10, min(n, 150))
    idx = list(range(max(60, len(bars) - h - n), len(bars) - h))
    sem = asyncio.Semaphore(conc)

    async def one(i):
        async with sem:
            try:
                a = await asker(features(bars[: i + 1]), h)      # data up to day i only
            except Exception as e:
                return {"error": repr(e)}
            lab, _ = lean(a)
            return {"asof": bars[i].d, "lean": lab, "p_up": a["p_up"], "fwd": _fwd(bars, i, h), "model": a["model"]}

    res = await asyncio.gather(*(one(i) for i in idx))
    ok = [r for r in res if "error" not in r]
    out = {"symbol": sym, "h": h, "source": source, "demo": demo, "errors": len(res) - len(ok),
           "stats": summarize(ok), "rows": ok[-40:],
           "caveat": "Jev may have seen this price history during training, so this test can look better than reality."}
    return out


def scoreboard(log_path: str, demo: bool = False) -> dict:
    p = Path(log_path)
    preds = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    cache: dict[str, list[Bar]] = {}
    resolved, pending = [], 0
    for q in preds:
        try:
            bars = cache.setdefault(q["symbol"], get_bars(q["symbol"], demo)[0])
            i = next(k for k, b in enumerate(bars) if b.d == q["asof"])
            fwd = _fwd(bars, i, q["h"])
        except Exception:
            fwd = None
        if fwd is None:
            pending += 1
        else:
            resolved.append({**q, "fwd": fwd})
    return {"made": len(preds), "pending": pending, "stats": summarize(resolved) if resolved else None,
            "recent": resolved[-15:]}


# ---------------------------------------------------------------------------
def make_handler(asker, model: str, log_path: str, has_key: bool):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if u.path == "/api/status":
                    return self._json({"has_key": has_key, "model": model})
                demo = q.get("demo") == "1"
                if u.path in ("/api/analyze", "/api/backtest") and not has_key:
                    return self._json({"error": "TYPESAFE_API_KEY is not set on the machine running this app."}, 400)
                h = max(1, min(int(q.get("h", 5)), 20))
                if u.path == "/api/analyze":
                    return self._json(asyncio.run(analyze(asker, check_symbol(q.get("symbol", "")), h, demo, log_path)))
                if u.path == "/api/backtest":
                    return self._json(asyncio.run(backtest(asker, check_symbol(q.get("symbol", "")), h,
                                                           int(q.get("n", 60)), demo)))
                if u.path == "/api/scoreboard":
                    return self._json(scoreboard(log_path, demo))
                self._send(404, b"not found", "text/plain")
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if "Authentication" in type(e).__name__ or "401" in str(e):
                    msg = ("TypeSafe rejected the API key (401). Stop the app with Ctrl+C, run ./start.sh again and "
                           "paste the full key, with no spaces. Keys start with apikey_.")
                self._json({"error": msg}, 502)

    return H


DEFAULT_MODEL = "jev-1.13.0"


def serve(model: str, port: int = 8000, log_path: str = "runs/predictions.jsonl", open_browser: bool = True) -> None:
    import webbrowser
    model = model if model not in ("", "jev-latest") else DEFAULT_MODEL
    key = bool(os.environ.get("TYPESAFE_API_KEY"))

    async def asker(snap, h):
        # one client per event loop: asyncio.run() makes a fresh loop per request
        return await ask(_client(model), model, snap, h)

    handler, srv = make_handler(asker, model, log_path, key), None
    for p in range(port, port + 11):                      # port busy? try the next ones
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
        webbrowser.open(url)                              # server is already listening, so no race
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
