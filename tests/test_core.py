import asyncio
import json
from types import SimpleNamespace

import pytest
from conftest import mk_tick

from jevbot.config import Config, PolicyCfg, _fill, load_config
from jevbot.feed import SyntheticFeed
from jevbot.jev import Decision, HeuristicDecider, build_questions, decision_from_response
from jevbot.loop import Bot
from jevbot.policy import Action, compose_action, rules_only
from jevbot.pricing import make_quotes
from jevbot.report import calibration
from jevbot.risk import RiskEngine
from jevbot.state import StateEngine
from jevbot.venue import PaperVenue


def dec(**kw):
    base = {
        "regime": "mean_reverting",
        "regime_conf": 0.8,
        "direction": "neutral",
        "direction_conf": 0.5,
        "toxic": 0.1,
        "stressed": 0.1,
        "quote_score": 2.5,
        "quote_conf": 0.9,
        "pressure_score": 0,
        "pressure_conf": 0.9,
        "model": "m1",
        "latency_ms": 50,
    }
    base.update(kw)
    return Decision(**base)


# ---------------- state ----------------
async def _ticks(n, seed=1):
    return [t async for t in SyntheticFeed(seed, n)]


def test_snapshot_has_no_future_data(cfg):
    ticks = asyncio.run(_ticks(400))

    def snaps(ts):
        e, v, out = StateEngine(warmup_blocks=5), PaperVenue(cfg.paper), []
        for t in ts:
            v.process_tick(t)
            out.append(e.update(t, v))
        return out

    short, long = snaps(ticks[:250]), snaps(ticks)
    assert short == long[:250]  # appending the future never changes the past


def test_snapshot_under_token_budget(cfg):
    ticks = asyncio.run(_ticks(500))
    e, v = StateEngine(warmup_blocks=5), PaperVenue(cfg.paper)
    for t in ticks:
        v.process_tick(t)
        s = e.update(t, v)
    assert StateEngine.approx_tokens(s) < 400


# ---------------- policy ----------------
def test_policy_thresholds():
    p = PolicyCfg()
    assert compose_action(dec(), p).kind == "QUOTE_BOTH"
    assert compose_action(dec(toxic=0.7), p).kind == "PULL_QUOTES"
    assert compose_action(dec(stressed=0.8), p).kind == "WIDEN"
    assert compose_action(dec(regime="crisis", regime_conf=0.9), p).kind == "STAND_DOWN"
    assert compose_action(dec(quote_conf=0.6), p).kind == "QUOTE_WIDE"  # confidence gate
    assert compose_action(dec(quote_conf=0.3), p).size_mult == 0.5
    assert compose_action(dec(quote_score=0.4), p).kind == "STAND_DOWN"


def test_quote_bar_higher_than_pull_bar():
    p = PolicyCfg()
    assert p.quote_min_conf > p.toxic_pull  # wrong quoting costs more than wrongly pulling


def test_rules_only_stands_down_when_wild():
    snap = {"vol_vs_long": 3.0, "spread_bps": 3, "aggressive_buy_ratio": 0.5, "inventory": 0}
    assert rules_only(snap, PolicyCfg()).kind == "STAND_DOWN"


# ---------------- pricing ----------------
SNAP = {"mid": 3.4002, "vol_5m_bps": 3.0, "inventory": 0.0}


def test_quotes_never_cross(cfg):
    for inv in (-50, 0, 50):
        for p in (0, 3):
            qs = make_quotes({**SNAP, "inventory": inv}, Action("QUOTE_BOTH", pressure=p), 3.4000, 3.4004, cfg.pricing)
            for q in qs:
                assert (q.side == "buy" and q.px < 3.4004) or (q.side == "sell" and q.px > 3.4000)


def test_long_inventory_shifts_quotes_down_and_cuts_adding_side(cfg):
    flat = {q.side: q for q in make_quotes(SNAP, Action("QUOTE_BOTH"), 3.3990, 3.4014, cfg.pricing)}
    long = {
        q.side: q
        for q in make_quotes({**SNAP, "inventory": 30.0}, Action("QUOTE_BOTH", pressure=2), 3.3990, 3.4014, cfg.pricing)
    }
    assert long["buy"].px < flat["buy"].px and long["sell"].px <= flat["sell"].px
    assert long["buy"].size < flat["buy"].size
    reduce_now = {
        q.side
        for q in make_quotes({**SNAP, "inventory": 30.0}, Action("QUOTE_BOTH", pressure=3), 3.3990, 3.4014, cfg.pricing)
    }
    assert reduce_now == {"sell"}


# ---------------- venue ----------------
def test_no_same_block_fill_and_post_only(cfg):
    v = PaperVenue(cfg.paper)
    t0 = mk_tick(0)
    v.process_tick(t0)
    assert v.place("buy", 3.4004, 5, 0, t0) is None and v.rejects == 1  # would cross: rejected
    v.place("buy", 3.4000, 5, 0, t0)
    sell = ((3.3999, 20.0, "sell"),)
    assert v.process_tick(mk_tick(0, trades=sell)) == []  # same block: no fill
    fills = v.process_tick(mk_tick(1, trades=sell))
    assert fills and fills[0].size == pytest.approx(5.0) and v.inv == pytest.approx(5.0)


def test_flatten_zeroes_inventory(cfg):
    v = PaperVenue(cfg.paper)
    t0 = mk_tick(0)
    v.process_tick(t0)
    v.place("buy", 3.4000, 5, 0, t0)
    v.process_tick(mk_tick(1, trades=((3.3999, 20.0, "sell"),)))
    assert v.inv > 0
    v.flatten()
    assert v.inv == 0 and not v.orders


# ---------------- risk ----------------
def test_risk_limits(cfg):
    v, r = PaperVenue(cfg.paper), RiskEngine(cfg.risk)
    t = mk_tick(0)
    v.process_tick(t)
    v.day_start_equity = v.hwm = v.equity
    from jevbot.pricing import Quote

    assert r.veto_order(Quote("buy", 3.4, 99), v)  # too big
    assert r.veto_order(Quote("buy", 3.4, 5), v) is None
    assert r.check_global(v, 5000, 0).veto_all  # stale data
    assert r.check_global(v, 0, 0, decision_ms=900).veto_all  # slow decision
    assert r.check_global(v, 0, 99).kill  # api errors
    v.cash -= cfg.risk.max_drawdown + 1
    assert r.check_global(v, 0, 0).kill  # drawdown


# ---------------- jev mapping ----------------
def _resp(model="m1"):
    def ch(c, k):
        return SimpleNamespace(choice=c, confidence=k)

    return SimpleNamespace(
        model=model,
        choices={"regime": ch("trending", 0.8), "direction": ch("up", 0.7)},
        nouls={"toxic_flow": SimpleNamespace(noul=0.2), "liquidity_stressed": SimpleNamespace(noul=0.1)},
        scores={
            "quote_environment": SimpleNamespace(score=2.4, confidence=0.9),
            "inventory_pressure": SimpleNamespace(score=1.0, confidence=0.7),
        },
    )


def test_decision_mapping_and_sdk_questions():
    d = decision_from_response(_resp(), 42)
    assert d.toxic == 0.2 and d.quote_conf == 0.9 and d.model == "m1"
    q = build_questions()
    assert set(q) == {
        "regime",
        "direction",
        "toxic_flow",
        "liquidity_stressed",
        "quote_environment",
        "inventory_pressure",
    }
    json.dumps({k: v.model_dump() for k, v in q.items()})  # serialisable


# ---------------- loop / fallback ladder ----------------
class Late:
    async def decide(self, snap):
        await asyncio.sleep(1)


class Broken:
    async def decide(self, snap):
        raise RuntimeError("503")


def run(cfg, decider, n=60, mode="jev", feed=None):
    cfg.jev.deadline_ms = 30
    bot = Bot(cfg, feed or SyntheticFeed(3, n), PaperVenue(cfg.paper), decider, mode=mode)
    return bot, asyncio.run(bot.run())


def test_late_decision_means_no_orders(cfg):
    cfg.jev.breaker_after = 10**6
    bot, s = run(cfg, Late())
    assert s["orders_sent"] == 0 and s["actions"].get("HOLD", 0) > 0


def test_jev_down_falls_back_to_rules_then_recovers(cfg):
    bot, s = run(cfg, Broken(), n=200)
    assert bot.jev_fails >= cfg.jev.breaker_after and not s["killed"]
    assert "STAND_DOWN" in s["actions"] or "QUOTE_WIDE" in s["actions"]
    assert s["actions"].get("HOLD", 0) <= cfg.jev.breaker_after + 200 // cfg.jev.retry_every + 1


def test_kill_flattens_and_blocks_restart(cfg):
    cfg.risk.max_drawdown = 0.01
    cfg.risk.max_daily_loss = 0.01
    feed = SyntheticFeed(5, 3000)
    bot, s = run(cfg, HeuristicDecider(), n=3000, feed=feed)
    assert s["killed"] and bot.venue.inv == 0 and not bot.venue.orders
    with pytest.raises(SystemExit):
        asyncio.run(Bot(cfg, SyntheticFeed(5, 10), PaperVenue(cfg.paper), None, mode="rules").run())


def test_venue_errors_trip_kill(cfg):
    cfg.risk.max_venue_errors = 3

    class Flaky(PaperVenue):
        def place(self, *a, **k):
            from jevbot.venue import VenueError

            raise VenueError("down")

    class AlwaysQuote:
        async def decide(self, snap):
            return dec()

    bot = Bot(cfg, SyntheticFeed(3, 300), Flaky(cfg.paper), AlwaysQuote())
    s = asyncio.run(bot.run())
    assert s["killed"] and "venue errors" in s["kill_reasons"][0]


def test_risk_never_exceeded_over_long_run(cfg):
    bot, s = run(cfg, HeuristicDecider(), n=6000)
    assert max(abs(f_inv) for f_inv in [bot.venue.inv]) <= cfg.risk.max_position * 1.5


# ---------------- config / report ----------------
def test_unknown_config_key_rejected():
    with pytest.raises(ValueError):
        _fill(Config(), {"policy": {"toxik_pull": 1}})


def test_shipped_config_loads_and_matches_defaults():
    shipped = load_config("config.toml")
    assert shipped.jev.model not in ("", "jev-latest")  # must be pinned
    shipped.jev.model = ""
    assert shipped == Config()


def test_calibration_labels_use_only_forward_prices():
    rows = []
    for i in range(60):
        mid = 3.4 + (0.002 * i)  # steady rise
        rows.append(
            {
                "snap": {"mid": mid, "aggressive_buy_ratio": 0.8},
                "decision": dict(dec(toxic=0.9, direction="up", direction_conf=0.9).to_dict(), source="jev"),
            }
        )
    c = calibration(rows)
    assert c["toxic_reliability"][-1]["observed"] == 1.0 and c["n_judged"] == 50
