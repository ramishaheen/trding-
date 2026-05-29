"""Weekly Target Manager.

Tracks an *aspirational* weekly profit target (default 4x), locks profit as gains
accrue, reduces risk in adaptive safe modes, controls trade frequency, and stops
trading when the target is reached or risk limits are hit.

It NEVER forces the target and NEVER overrides the Risk Governor:

    Strategy -> Weekly Target Manager -> Risk Governor -> Execution -> BingX

If the Target Manager says "trade" but the Risk Governor rejects, the trade does
not execute. If the Risk Governor would approve but the Target Manager says the
weekly target is reached (or risk-locked), the trade is rejected.
"""

from .config import WeeklyTargetConfig, load_weekly_config
from .models import (
    SafeMode,
    TargetStatus,
    WeeklyDecision,
    WeeklyMetrics,
)
from .manager import WeeklyTargetManager

__all__ = [
    "WeeklyTargetConfig",
    "load_weekly_config",
    "WeeklyTargetManager",
    "WeeklyMetrics",
    "WeeklyDecision",
    "TargetStatus",
    "SafeMode",
]
