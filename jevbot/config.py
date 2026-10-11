from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class JevCfg:
    model: str = ""
    block_ms: float = 300
    deadline_ms: float = 250
    breaker_after: int = 3
    retry_every: int = 20


@dataclass
class PolicyCfg:
    toxic_pull: float = 0.60
    stress_widen: float = 0.70
    quote_min_score: float = 2.0
    quote_min_conf: float = 0.80
    wide_min_score: float = 1.0
    crisis_conf: float = 0.60
    low_conf: float = 0.50
    direction_min_conf: float = 0.75
    direction_tilt_bps: float = 0.0
    widen_mult: float = 3.0
    wide_mult: float = 2.0
    rules_max_spread_bps: float = 8.0      # code-only fallback: quote only if the spread is sane for THIS venue
    rules_max_vol_ratio: float = 1.5
    rules_flow_band: float = 0.2           # |buy ratio - 0.5| must stay below this
    rules_pressure_step: float = 12.0      # inventory per pressure level


@dataclass
class PricingCfg:
    gamma: float = 1.0
    horizon_blocks: int = 100
    base_half_spread_bps: float = 2.0
    vol_spread_k: float = 0.5
    min_half_spread_bps: float = 1.0
    skew_bps: float = 1.0
    base_size: float = 5.0
    tick_size: float = 0.0001


@dataclass
class RiskCfg:
    max_position: float = 40.0
    max_order_size: float = 10.0
    max_daily_loss: float = 25.0
    max_drawdown: float = 40.0
    max_hold_seconds: float = 900.0
    max_data_age_ms: float = 1500.0
    max_leverage: float = 3.0
    max_venue_errors: int = 5
    max_decision_ms: float = 250.0


@dataclass
class PaperCfg:
    capital: float = 1000.0
    maker_fee_bps: float = 1.0
    order_cost: float = 0.001
    queue_factor: float = 0.5
    exit_slippage_bps: float = 10.0


@dataclass
class RunCfg:
    blocks: int = 20000
    seed: int = 7
    warmup_blocks: int = 120
    order_ttl_blocks: int = 30
    replace_bps: float = 0.75
    journal: str = "runs/journal.jsonl"
    kill_file: str = "KILL"
    vol_long_halflife_blocks: int = 20000


@dataclass
class Config:
    jev: JevCfg = field(default_factory=JevCfg)
    policy: PolicyCfg = field(default_factory=PolicyCfg)
    pricing: PricingCfg = field(default_factory=PricingCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    paper: PaperCfg = field(default_factory=PaperCfg)
    run: RunCfg = field(default_factory=RunCfg)


def _fill(obj: Any, data: dict[str, Any], path: str = "") -> None:
    names = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            _fill(current, value, f"{path}{key}.")
        else:
            setattr(obj, key, type(current)(value) if current is not None else value)


def load_config(path: str | Path | None = "config.toml") -> Config:
    cfg = Config()
    if path and Path(path).exists():
        _fill(cfg, tomllib.loads(Path(path).read_text()))
    return cfg
