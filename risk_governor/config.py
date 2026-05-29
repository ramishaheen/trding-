"""Risk Governor configuration loader.

Reads risk_config.json and applies environment overrides (RG_<UPPER_KEY>).
All risk values are config-driven; nothing is hardcoded in execution logic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = os.environ.get(
    "RISK_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "risk_config.json"),
)


@dataclass
class RiskConfig:
    trading_mode: str = "REAL_TRADING_STRICT"

    max_risk_per_trade_percent: float = 0.5
    max_daily_loss_percent: float = 2.0
    max_weekly_loss_percent: float = 5.0
    max_total_drawdown_percent: float = 8.0
    max_open_positions: int = 1
    max_capital_exposure_percent: float = 15.0
    max_leverage: float = 2
    min_risk_reward_ratio: float = 1.5
    preferred_risk_reward_ratio: float = 2.0
    max_consecutive_losses: int = 3
    cooldown_after_loss_minutes: float = 30
    cooldown_after_consecutive_losses_hours: float = 24
    cooldown_after_max_loss_hours: float = 24
    max_spread_percent: float = 0.05
    max_slippage_percent: float = 0.10
    atr_spike_multiplier: float = 2.0
    trade_quality_min_score: float = 75
    account_reconciliation_interval_seconds: float = 30
    force_isolated_margin: bool = True
    allow_cross_margin: bool = False
    allow_martingale: bool = False
    allow_averaging_down: bool = False
    allow_trade_without_stop_loss: bool = False
    allow_trade_without_take_profit: bool = False
    fail_closed: bool = True
    news_pause: bool = False
    manual_restart_required_after_kill_switch: bool = True

    # Operational thresholds (complete the fail-closed implementation).
    min_order_value_usdt: float = 2.0
    min_stop_distance_percent: float = 0.10
    max_stop_distance_percent: float = 15.0
    max_price_staleness_seconds: float = 10
    min_orderbook_depth_quote: float = 50.0
    api_error_threshold: int = 5
    api_error_window_seconds: float = 600
    order_rejection_threshold: int = 3
    order_rejection_window_seconds: float = 600
    duplicate_window_seconds: float = 60
    default_max_holding_time_minutes: float = 1440


def _coerce(field_type: Any, raw: str) -> Any:
    # With `from __future__ import annotations`, field types arrive as strings.
    name = field_type if isinstance(field_type, str) else getattr(field_type, "__name__", "")
    if name == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if name == "int":
        return int(float(raw))
    if name == "float":
        return float(raw)
    return raw


def load_config(path: str | None = None) -> RiskConfig:
    """Load config from JSON, then apply RG_<UPPER_KEY> env overrides."""
    path = path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    try:
        with open(path) as fh:
            data = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except FileNotFoundError:
        # Fail-closed config philosophy: missing file -> use safe dataclass
        # defaults (which are the conservative spec values).
        data = {}

    cfg = RiskConfig()
    valid = {f.name: f.type for f in fields(RiskConfig)}
    for key, value in data.items():
        if key in valid:
            setattr(cfg, key, value)

    # Env overrides win.
    for f in fields(RiskConfig):
        env_key = f"RG_{f.name.upper()}"
        if env_key in os.environ:
            setattr(cfg, f.name, _coerce(f.type, os.environ[env_key]))

    return cfg
