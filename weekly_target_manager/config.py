"""Weekly Target Manager configuration loader (JSON + WT_<KEY> env overrides)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = os.environ.get(
    "WEEKLY_TARGET_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "weekly_target_config.json"),
)


@dataclass
class WeeklyTargetConfig:
    weekly_target_multiplier: float = 4.0
    weekly_target_mode: str = "aspirational"
    force_target: bool = False
    allow_risk_increase_to_reach_target: bool = False
    allow_martingale: bool = False
    allow_averaging_down: bool = False
    allow_leverage_increase: bool = False
    lock_profit_after_target_reached: bool = True
    stop_trading_after_target_reached: bool = True

    max_weekly_loss_percent: float = 5.0
    max_daily_loss_percent: float = 2.0
    max_total_drawdown_percent: float = 8.0
    max_risk_per_trade_percent: float = 0.5
    max_leverage: float = 2
    max_open_positions: int = 1
    max_capital_exposure_percent: float = 15.0

    profit_lock_levels: list = field(default_factory=lambda: [
        {"weekly_profit_percent": 25, "action": "reduce_risk_by_25_percent"},
        {"weekly_profit_percent": 50, "action": "reduce_risk_by_50_percent"},
        {"weekly_profit_percent": 100, "action": "reduce_risk_by_75_percent"},
        {"weekly_profit_percent": 300, "action": "stop_trading_and_lock_profit"},
    ])

    max_trades_per_day: int = 3
    max_trades_per_week: int = 10
    minimum_minutes_between_trades: float = 60
    cooldown_after_loss_minutes: float = 30
    cooldown_after_win_minutes: float = 15

    timezone: str = "Asia/Amman"
    trading_days_per_week: int = 7

    required_daily_return_unrealistic_percent: float = 12.0
    min_trades_for_expectancy: int = 10
    caution_min_quality_score: float = 85
    defensive_min_quality_score: float = 90


def _coerce(field_type: Any, raw: str) -> Any:
    name = field_type if isinstance(field_type, str) else getattr(field_type, "__name__", "")
    if name == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if name == "int":
        return int(float(raw))
    if name == "float":
        return float(raw)
    return raw


def load_weekly_config(path: str | None = None) -> WeeklyTargetConfig:
    path = path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    try:
        with open(path) as fh:
            data = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except FileNotFoundError:
        data = {}

    cfg = WeeklyTargetConfig()
    valid = {f.name for f in fields(WeeklyTargetConfig)}
    for key, value in data.items():
        if key in valid:
            setattr(cfg, key, value)

    for f in fields(WeeklyTargetConfig):
        if f.name == "profit_lock_levels":
            continue
        env_key = f"WT_{f.name.upper()}"
        if env_key in os.environ:
            setattr(cfg, f.name, _coerce(f.type, os.environ[env_key]))
    return cfg
