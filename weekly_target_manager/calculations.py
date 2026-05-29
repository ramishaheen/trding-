"""Pure weekly-target calculations — unit-tested exactly as they run.

No exchange / governor imports; just math on numbers so the rules are testable.
"""

from __future__ import annotations

from typing import Optional, Sequence


def weekly_metrics(start_balance: float, current_equity: float, multiplier: float) -> dict:
    """Core weekly numbers (spec section 3)."""
    start_balance = max(0.0, start_balance)
    target_balance = start_balance * multiplier
    required_profit = target_balance - start_balance
    weekly_profit = current_equity - start_balance
    weekly_profit_percent = (weekly_profit / start_balance * 100.0) if start_balance > 0 else 0.0
    completion = (weekly_profit / required_profit * 100.0) if required_profit > 0 else 0.0
    remaining_profit = max(0.0, target_balance - current_equity)
    return {
        "weekly_start_balance": start_balance,
        "current_equity": current_equity,
        "weekly_target_balance": target_balance,
        "required_weekly_profit": required_profit,
        "current_weekly_profit": weekly_profit,
        "weekly_profit_percent": weekly_profit_percent,
        "target_completion_percent": completion,
        "remaining_profit_needed": remaining_profit,
    }


def remaining_trading_days(weekday_mon0: int, trading_days_per_week: int = 7) -> int:
    """Days left in the week (inclusive of today). Monday=0 ... Sunday=6."""
    return max(1, trading_days_per_week - weekday_mon0)


def required_daily_return_percent(current_equity: float, target_balance: float,
                                  remaining_days: int) -> float:
    """Compounded daily growth needed to reach target by week end. 0 if already
    at/above target or inputs invalid."""
    if current_equity <= 0 or remaining_days <= 0 or target_balance <= current_equity:
        return 0.0
    growth = (target_balance / current_equity) ** (1.0 / remaining_days) - 1.0
    return growth * 100.0


def expectancy(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """expectancy = win_rate*avg_win - loss_rate*avg_loss. avg_loss is a positive
    magnitude. win_rate in [0,1]."""
    loss_rate = max(0.0, 1.0 - win_rate)
    return win_rate * avg_win - loss_rate * abs(avg_loss)


def expectancy_from_trades(pnls: Sequence[float]) -> dict:
    """Compute win rate, avg win/loss, profit factor and expectancy from a list
    of realized PnLs."""
    n = len(pnls)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "expectancy": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    return {
        "trades": n,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy(win_rate, avg_win, avg_loss),
    }


def profit_lock(weekly_profit_percent: float, levels: list[dict]) -> tuple[float, bool, Optional[dict]]:
    """Return (risk_multiplier, stop_and_lock, level_hit) for the highest profit
    lock level crossed (spec section 6)."""
    action_to_mult = {
        "reduce_risk_by_25_percent": 0.75,
        "reduce_risk_by_50_percent": 0.50,
        "reduce_risk_by_75_percent": 0.25,
        "stop_trading_and_lock_profit": 0.0,
    }
    hit: Optional[dict] = None
    for lvl in sorted(levels, key=lambda x: x.get("weekly_profit_percent", 0)):
        if weekly_profit_percent >= lvl.get("weekly_profit_percent", 0):
            hit = lvl
    if hit is None:
        return 1.0, False, None
    action = hit.get("action", "")
    mult = action_to_mult.get(action, 1.0)
    return mult, action == "stop_trading_and_lock_profit", hit
