"""Quote math (Avellaneda-Stoikov style). Jev decides whether to quote, never where."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import PricingCfg
from .policy import Action


@dataclass(frozen=True)
class Quote:
    side: str
    px: float
    size: float


def make_quotes(snap: dict, action: Action, best_bid: float, best_ask: float, c: PricingCfg) -> list[Quote]:
    if not action.quoting:
        return []
    mid, inv = snap["mid"], snap["inventory"]
    sigma = snap["vol_5m_bps"] * 1e-4 * mid                   # price units per block
    reservation = mid - inv * c.gamma * sigma ** 2 * c.horizon_blocks
    sign = (inv > 0) - (inv < 0)
    reservation -= sign * action.pressure * c.skew_bps * 1e-4 * mid
    reservation += action.tilt_bps * 1e-4 * mid
    half_bps = max(c.min_half_spread_bps, c.base_half_spread_bps + c.vol_spread_k * snap["vol_5m_bps"])
    half_bps *= action.spread_mult
    if sign and action.pressure >= 3:
        half_bps_reduce = half_bps * 0.5
    else:
        half_bps_reduce = half_bps
    bid = reservation - (half_bps_reduce if sign < 0 else half_bps) * 1e-4 * mid
    ask = reservation + (half_bps_reduce if sign > 0 else half_bps) * 1e-4 * mid
    # post-only: stay strictly inside the book, snap to the tick grid in the safe direction
    t = c.tick_size
    bid = min(math.floor(bid / t + 1e-9) * t, best_ask - t)
    ask = max(math.ceil(ask / t - 1e-9) * t, best_bid + t)
    size = c.base_size * action.size_mult
    bid_size = ask_size = size
    if sign and action.pressure >= 2:
        adding = "buy" if sign > 0 else "sell"
        cut = 0.0 if action.pressure >= 3 else 0.5
        if adding == "buy":
            bid_size *= cut
        else:
            ask_size *= cut
    out = []
    if bid_size > 0:
        out.append(Quote("buy", round(bid, 8), round(bid_size, 4)))
    if ask_size > 0:
        out.append(Quote("sell", round(ask, 8), round(ask_size, 4)))
    return out
