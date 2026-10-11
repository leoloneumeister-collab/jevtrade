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


def _bot(cfg: Config, mode: str, journal: str | None, blocks: int | None, realtime: bool = False) -> Bot:
    n = blocks or cfg.run.blocks
    feed = SyntheticFeed(cfg.run.seed, n, cfg.jev.block_ms, cfg.pricing.tick_size, realtime=realtime)
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
    bot = _bot(cfg, a.mode, a.journal or cfg.run.journal, a.blocks, realtime=a.realtime)
    s = asyncio.run(bot.run())
    print(json.dumps(s, indent=2))
    print(fee_check(s))
    if a.mode != "jev":
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
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("compare"); c.add_argument("--blocks", type=int, default=8000); c.set_defaults(fn=cmd_compare)
    sub.add_parser("models").set_defaults(fn=cmd_models)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    rp = sub.add_parser("report"); rp.add_argument("journal", nargs="?"); rp.set_defaults(fn=lambda a, cfg: print_report(a.journal or cfg.run.journal))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    a.fn(a, load_config(a.config))


if __name__ == "__main__":
    main()
