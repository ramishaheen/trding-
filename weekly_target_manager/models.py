"""Models for the Weekly Target Manager."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class TargetStatus(str, Enum):
    ASPIRATIONAL = "aspirational"
    ON_TRACK = "on_track"
    BEHIND_TARGET = "behind_target"
    UNREALISTIC = "unrealistic_under_current_risk_limits"
    COMPLETED = "completed"
    LOCKED_DUE_TO_LOSS = "locked_due_to_loss"
    LOCKED_DUE_TO_DRAWDOWN = "locked_due_to_drawdown"
    KILLED = "killed"


class SafeMode(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    DEFENSIVE = "defensive"
    LOCKED = "locked"


@dataclass
class WeeklyMetrics:
    weekly_start_balance: float = 0.0
    current_equity: float = 0.0
    weekly_target_multiplier: float = 4.0
    weekly_target_balance: float = 0.0
    required_weekly_profit: float = 0.0
    current_weekly_profit: float = 0.0
    weekly_profit_percent: float = 0.0
    target_completion_percent: float = 0.0
    remaining_profit_needed: float = 0.0
    remaining_trading_days: int = 7
    required_daily_return_percent: float = 0.0


@dataclass
class WeeklyDecision:
    """The Target Manager's pre-governor verdict. It can BLOCK or TIGHTEN only."""
    allow: bool
    reason: str = ""
    risk_multiplier: float = 1.0          # multiplies governor size (<=1)
    min_quality_score: float = 0.0        # raises governor quality bar (never lowers)
    safe_mode: SafeMode = SafeMode.NORMAL
    target_status: TargetStatus = TargetStatus.ASPIRATIONAL

    def __bool__(self) -> bool:
        return self.allow


def dashboard_dict(metrics: WeeklyMetrics, *, target_status: str, target_realism: str,
                   risk_mode: str, trading_allowed: bool, profit_locked: bool,
                   weekly_loss_limit_reached: bool, daily_loss_limit_reached: bool,
                   kill_switch_active: bool) -> dict:
    """The section-10 weekly target dashboard object."""
    return {
        "weekly_start_balance": metrics.weekly_start_balance,
        "current_equity": metrics.current_equity,
        "weekly_target_multiplier": metrics.weekly_target_multiplier,
        "weekly_target_balance": metrics.weekly_target_balance,
        "required_weekly_profit": metrics.required_weekly_profit,
        "current_weekly_profit": metrics.current_weekly_profit,
        "weekly_profit_percent": metrics.weekly_profit_percent,
        "target_completion_percent": metrics.target_completion_percent,
        "remaining_profit_needed": metrics.remaining_profit_needed,
        "remaining_trading_days": metrics.remaining_trading_days,
        "required_daily_return_percent": metrics.required_daily_return_percent,
        "target_status": target_status,
        "target_realism": target_realism,
        "risk_mode": risk_mode,
        "trading_allowed": trading_allowed,
        "profit_locked": profit_locked,
        "weekly_loss_limit_reached": weekly_loss_limit_reached,
        "daily_loss_limit_reached": daily_loss_limit_reached,
        "kill_switch_active": kill_switch_active,
    }
