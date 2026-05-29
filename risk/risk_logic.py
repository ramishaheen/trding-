"""Pure, dependency-free risk-limit logic.

Kept separate from the watchdog's I/O (Freqtrade REST, Telegram, logging) so the
limit rules can be unit-tested deterministically. A strategy bug cannot bypass
these checks because the watchdog is an independent process.

Conventions
-----------
* All P&L and equity values are in the stake currency (USDT).
* `daily_max_loss` and `max_drawdown` are POSITIVE fractions of total capital
  (e.g. 0.05 == 5%).
* "Loss" is measured as a positive number of currency lost.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    total_capital: float
    daily_max_loss_pct: float   # e.g. 0.05
    max_drawdown_pct: float     # e.g. 0.10

    @property
    def daily_max_loss_abs(self) -> float:
        return self.total_capital * self.daily_max_loss_pct

    @property
    def max_drawdown_abs(self) -> float:
        return self.total_capital * self.max_drawdown_pct


@dataclass
class RiskState:
    # realized + unrealized P&L for the current day (negative == loss)
    day_pnl: float
    # peak equity seen and current equity, for drawdown
    peak_equity: float
    current_equity: float


@dataclass
class HaltDecision:
    halt: bool
    reasons: list[str]

    def __bool__(self) -> bool:  # convenient truthiness
        return self.halt


def daily_loss_breached(day_pnl: float, limits: RiskLimits) -> bool:
    """True when today's loss meets/exceeds the daily cap."""
    loss = -min(day_pnl, 0.0)  # positive loss amount
    return loss >= limits.daily_max_loss_abs


def current_drawdown_abs(state: RiskState) -> float:
    """Absolute drawdown from peak equity (>= 0)."""
    return max(0.0, state.peak_equity - state.current_equity)


def drawdown_breached(state: RiskState, limits: RiskLimits) -> bool:
    return current_drawdown_abs(state) >= limits.max_drawdown_abs


def evaluate(state: RiskState, limits: RiskLimits) -> HaltDecision:
    """Combine all limits into a single halt decision with human-readable
    reasons. Independent of any strategy logic."""
    reasons: list[str] = []
    if daily_loss_breached(state.day_pnl, limits):
        reasons.append(
            f"daily loss {(-state.day_pnl):.2f} >= cap "
            f"{limits.daily_max_loss_abs:.2f} ({limits.daily_max_loss_pct:.1%})"
        )
    if drawdown_breached(state, limits):
        reasons.append(
            f"drawdown {current_drawdown_abs(state):.2f} >= cap "
            f"{limits.max_drawdown_abs:.2f} ({limits.max_drawdown_pct:.1%})"
        )
    return HaltDecision(halt=bool(reasons), reasons=reasons)
