from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import sys
from pathlib import Path

from .config import Config, load_config
from .feed import SyntheticFeed
from .jev import HeuristicDecider, JevDecider
from .journal import Journal
from .loop import Bot
from .report import fee_check, print_report
from .venue import PaperVenue


def _feed(cfg: Config, a, n: int, realtime: bool = False):
    kind = getattr(a, "feed", "synthetic")
    if kind == "polymarket":
        from .polymarket import LiveFeed
        if not a.token:
            sys.exit("Pass --token <yes-token-id>. List candidates with `jevbot markets`.")
        return LiveFeed(a.token, cfg.jev.block_ms, record=a.record, max_blocks=n)
    if kind == "replay":
        from .polymarket import ReplayFeed
        if not a.replay:
            sys.exit("Pass --replay <recorded.jsonl>.")
        return ReplayFeed(a.replay, a.token, cfg.jev.block_ms, max_blocks=a.blocks)
    return SyntheticFeed(cfg.run.seed, n, cfg.jev.block_ms, cfg.pricing.tick_size, realtime=realtime)


def _bot(cfg: Config, mode: str, journal: str | None, blocks: int | None, realtime: bool = False, a=None) -> Bot:
    n = blocks or cfg.run.blocks
    feed = _feed(cfg, a, n, realtime)
    venue = PaperVenue(cfg.paper)
    if mode == "rules":
        decider = None
    elif mode == "double":
        decider = HeuristicDecider()
    elif mode == "jev":
        if not os.environ.get("TYPESAFE_API_KEY"):
            sys.exit("Set TYPESAFE_API_KEY first (console.typesafe.ai/keys).")
        if cfg.jev.model in ("", "jev-latest"):
            sys.exit("Pin an exact model in config.toml [jev].model. Run `jevbot models`. "
                     "A moving alias silently invalidates your tuned thresholds.")
        decider = JevDecider(cfg.jev.model, cfg.jev.deadline_ms)
    else:
        sys.exit(f"unknown mode {mode}")
    return Bot(cfg, feed, venue, decider, Journal(journal), mode)


def cmd_run(a, cfg: Config) -> None:
    bot = _bot(cfg, a.mode, a.journal or cfg.run.journal, a.blocks, realtime=a.realtime, a=a)
    s = asyncio.run(bot.run())
    print(json.dumps(s, indent=2))
    print(fee_check(s))
    if a.feed == "synthetic" and a.mode != "jev":
        print("NOTE: synthetic data. This checks plumbing and fee math only, not an edge.")


def cmd_compare(a, cfg: Config) -> None:
    """Same data, same costs, same limits: code-only vs decision layer vs decision layer without the gate."""
    nogate = copy.deepcopy(cfg)
    nogate.policy.quote_min_conf = 0.0
    nogate.policy.low_conf = 0.0
    cases = [("rules-only", cfg, "rules"), ("double+gate", cfg, "double"), ("double-no-gate", nogate, "double")]
    for name, c, mode in cases:
        kf = Path(c.run.kill_file)
        s = asyncio.run(_bot(c, mode, None, a.blocks).run())
        print(f"{name:15} net={s['net_pnl']:>9} fees={s['fees']:>8} fills={s['fills']:>5} "
              f"maxdd={s['max_drawdown']:>7} killed={s['killed']}")
        if kf.exists():
            kf.unlink()
    print("\n'double' is an offline stand-in, NOT Jev. Run `jevbot run --mode jev` with a real key for the real comparison.")


def cmd_markets(a, cfg: Config) -> None:
    from .polymarket import list_markets
    rows = list_markets(min_days=a.min_days)
    if not rows:
        sys.exit("No candidates (or the Gamma API is unreachable from here).")
    for r in rows[: a.top]:
        print(f"{r['mid']:.3f} spr={r['spread']:.3f} tick={r['tick']} vol24h={r['vol24h']:>10,.0f} "
              f"{r['days_left']:>6}d  {r['token_yes']}\n    {r['question']}")


def cmd_web(a, cfg: Config) -> None:
    from .stock_app import serve
    serve(cfg.jev.model, a.port, open_browser=not a.no_browser)


def cmd_scan(a, cfg: Config) -> None:
    """One pass over a watchlist: print the table and save every read to the scoreboard. Made for cron/launchd."""
    from . import stock_app
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("Set TYPESAFE_API_KEY first.")
    model = cfg.jev.model if cfg.jev.model not in ("", "jev-latest") else stock_app.DEFAULT_MODEL
    res = asyncio.run(stock_app.scan(stock_app.make_asker(model), (a.symbols or stock_app.DEFAULT_WATCHLIST).split(","),
                                     a.h, a.demo, a.log))
    for r in res["rows"]:
        if r.get("error"):
            print(f"{r['symbol']:<8} ERROR {r['error']}")
        else:
            print(f"{r['symbol']:<8} {r['lean']:<10} up {r['p_up']:.0%}  down {r['p_down']:.0%}  close {r['close']}  ({r['asof']})")
    if all(r.get("error") for r in res["rows"]):
        sys.exit(1)


def cmd_models(a, cfg: Config) -> None:
    from typesafe_sdk import TypeSafeClient
    for m in TypeSafeClient().models.list().models:
        print(f"{m.name}\t{m.release_date}\t{m.description}")


def cmd_check(a, cfg: Config) -> None:
    """First Jev call on a sample snapshot. If this prints, you are talking to Jev."""
    from .jev import JevDecider
    model = cfg.jev.model or "jev-latest"
    d = JevDecider(model, 10_000)
    d.model = model
    snap = {"mid": 3.4127, "spread_bps": 3.5, "imbalance": 0.71, "vol_5m_bps": 3.4, "inventory": -120,
            "aggressive_buy_ratio": 0.63}
    async def go():
        from .jev import decision_from_response
        import time
        t0 = time.perf_counter()
        resp = await d.client.system_one(state=snap, questions=d.questions, model=model)
        return decision_from_response(resp, (time.perf_counter() - t0) * 1000)
    dec = asyncio.run(go())
    print(json.dumps(dec.to_dict(), indent=2))
    print(f"\nserver model: {dec.model}  <- put this exact string in config.toml [jev].model")


def main() -> None:
    p = argparse.ArgumentParser(prog="jevbot")
    p.add_argument("--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--mode", choices=["rules", "double", "jev"], default="double")
    r.add_argument("--blocks", type=int); r.add_argument("--journal"); r.add_argument("--realtime", action="store_true")
    r.add_argument("--feed", choices=["synthetic", "polymarket", "replay"], default="synthetic")
    r.add_argument("--token", help="Polymarket outcome token id (the YES token from `jevbot markets`)")
    r.add_argument("--record", help="save raw websocket messages here (for replay and schema checks)")
    r.add_argument("--replay", help="recorded session to replay")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("compare"); c.add_argument("--blocks", type=int, default=8000); c.set_defaults(fn=cmd_compare)
    mk = sub.add_parser("markets"); mk.add_argument("--top", type=int, default=15); mk.add_argument("--min-days", type=float, default=7.0)
    mk.set_defaults(fn=cmd_markets)
    w = sub.add_parser("web"); w.add_argument("--port", type=int, default=8000); w.add_argument("--no-browser", action="store_true"); w.set_defaults(fn=cmd_web)
    sc = sub.add_parser("scan"); sc.add_argument("--symbols"); sc.add_argument("--h", type=int, default=5)
    sc.add_argument("--log", default="runs/predictions.jsonl"); sc.add_argument("--demo", action="store_true")
    sc.set_defaults(fn=cmd_scan)
    sub.add_parser("models").set_defaults(fn=cmd_models)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    rp = sub.add_parser("report"); rp.add_argument("journal", nargs="?"); rp.set_defaults(fn=lambda a, cfg: print_report(a.journal or cfg.run.journal))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    a.fn(a, load_config(a.config))


if __name__ == "__main__":
    main()
