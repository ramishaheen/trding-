"""WeeklyTargetManager — risk-first pursuit of an aspirational weekly target.

Never forces the target, never overrides the Risk Governor. It can only BLOCK a
trade (target reached, risk-locked, overtrading, negative expectancy) or TIGHTEN
risk (reduce size via a multiplier, raise the quality bar). The Risk Governor
keeps final authority downstream.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from .calculations import (
    expectancy_from_trades,
    profit_lock,
    remaining_trading_days,
    required_daily_return_percent,
    weekly_metrics,
)
from .config import WeeklyTargetConfig, load_weekly_config
from .models import (
    SafeMode,
    TargetStatus,
    WeeklyDecision,
    WeeklyMetrics,
    dashboard_dict,
)

logger = logging.getLogger("weekly_target_manager")

try:  # zoneinfo may need the `tzdata` package on some systems
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None  # type: ignore


class WeeklyTargetManager:
    def __init__(self, config: Optional[WeeklyTargetConfig] = None,
                 cancel_orders: Optional[Callable[[], None]] = None,
                 close_positions: Optional[Callable[[], None]] = None,
                 alert: Optional[Callable[[str, str], None]] = None):
        self.cfg = config or load_weekly_config()
        self._cancel_orders = cancel_orders
        self._close_positions = close_positions
        self._alert = alert or (lambda title, msg: logger.warning("%s: %s", title, msg))

        self._tz = timezone.utc
        if ZoneInfo is not None:
            try:
                self._tz = ZoneInfo(self.cfg.timezone)
            except Exception:  # noqa: BLE001
                logger.warning("timezone %s unavailable; using UTC", self.cfg.timezone)

        # Equity / balances
        self.weekly_start_balance = 0.0
        self.daily_start_equity = 0.0
        self.current_equity = 0.0
        self.current_balance = 0.0
        self.unrealized_pnl = 0.0
        self.equity_peak = 0.0
        self.max_equity_week = 0.0

        # Realized PnL buckets
        self.realized_pnl_week = 0.0
        self.realized_pnl_day = 0.0

        # State flags
        self.profit_locked = False
        self.week_completed = False
        self.kill_switch_active = False
        self.external_drawdown_percent: Optional[float] = None

        # Trade activity
        self.consecutive_losses = 0
        self.last_trade_time = 0.0
        self.last_trade_result: Optional[str] = None
        self.cooldown_until = 0.0
        self.trade_times_day: list[float] = []
        self.trade_times_week: list[float] = []
        self.trade_pnls: list[float] = []          # rolling history for expectancy

        # Audit of rejections
        self.rejected_count = 0
        self.rejection_reasons: dict[str, int] = {}
        self.biggest_win = 0.0
        self.biggest_loss = 0.0

        self._week_key: Optional[tuple] = None
        self._day_key: Optional[tuple] = None

    # ---------------------------------------------------------------- time
    def _dt(self, now_ts: float) -> datetime:
        return datetime.fromtimestamp(now_ts, tz=self._tz)

    def _wk(self, now_ts: float) -> tuple:
        iso = self._dt(now_ts).isocalendar()
        return (iso[0], iso[1])

    def _dk(self, now_ts: float) -> tuple:
        d = self._dt(now_ts)
        return (d.year, d.month, d.day)

    # -------------------------------------------------------------- update
    def update(self, equity: float, balance: float, now_ts: float,
               unrealized_pnl: float = 0.0, drawdown_percent: Optional[float] = None,
               kill_switch_active: bool = False) -> None:
        wk, dk = self._wk(now_ts), self._dk(now_ts)

        if self._week_key is None:
            self._week_key = wk
            self.weekly_start_balance = equity
            self.max_equity_week = equity
        elif wk != self._week_key:  # weekly reset (Asia/Amman, Monday 00:00)
            self._week_key = wk
            self.weekly_start_balance = equity
            self.realized_pnl_week = 0.0
            self.max_equity_week = equity
            self.profit_locked = False
            self.week_completed = False
            self.trade_times_week = []
            self._alert("Weekly reset", f"New week. start_balance={equity:.2f}")

        if self._day_key is None or dk != self._day_key:
            self._day_key = dk
            self.daily_start_equity = equity
            self.realized_pnl_day = 0.0
            self.trade_times_day = []

        self.current_equity = equity
        self.current_balance = balance
        self.unrealized_pnl = unrealized_pnl
        self.equity_peak = max(self.equity_peak, equity)
        self.max_equity_week = max(self.max_equity_week, equity)
        self.external_drawdown_percent = drawdown_percent
        self.kill_switch_active = kill_switch_active

    # -------------------------------------------------------- trade results
    def record_trade_result(self, pnl: float, now_ts: float) -> None:
        self.realized_pnl_week += pnl
        self.realized_pnl_day += pnl
        self.trade_pnls.append(pnl)
        self.trade_pnls = self.trade_pnls[-500:]
        self.trade_times_day.append(now_ts)
        self.trade_times_week.append(now_ts)
        self.last_trade_time = now_ts
        self.biggest_win = max(self.biggest_win, pnl)
        self.biggest_loss = min(self.biggest_loss, pnl)
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_trade_result = "loss"
            self.cooldown_until = now_ts + self.cfg.cooldown_after_loss_minutes * 60
        else:
            self.consecutive_losses = 0
            self.last_trade_result = "win"
            self.cooldown_until = now_ts + self.cfg.cooldown_after_win_minutes * 60

    def note_rejection(self, reason: str) -> None:
        self.rejected_count += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    # ------------------------------------------------------------- metrics
    def drawdown_percent(self) -> float:
        if self.external_drawdown_percent is not None:
            return self.external_drawdown_percent
        if self.equity_peak <= 0:
            return 0.0
        return max(0.0, (self.equity_peak - self.current_equity) / self.equity_peak * 100.0)

    def weekly_loss_percent(self) -> float:
        if self.weekly_start_balance <= 0:
            return 0.0
        return max(0.0, (self.weekly_start_balance - self.current_equity) / self.weekly_start_balance * 100.0)

    def daily_loss_percent(self) -> float:
        if self.daily_start_equity <= 0:
            return 0.0
        return max(0.0, (self.daily_start_equity - self.current_equity) / self.daily_start_equity * 100.0)

    def metrics(self, now_ts: float) -> WeeklyMetrics:
        m = weekly_metrics(self.weekly_start_balance, self.current_equity,
                           self.cfg.weekly_target_multiplier)
        rem_days = remaining_trading_days(self._dt(now_ts).weekday(), self.cfg.trading_days_per_week)
        rdr = required_daily_return_percent(self.current_equity, m["weekly_target_balance"], rem_days)
        return WeeklyMetrics(
            weekly_start_balance=m["weekly_start_balance"],
            current_equity=m["current_equity"],
            weekly_target_multiplier=self.cfg.weekly_target_multiplier,
            weekly_target_balance=m["weekly_target_balance"],
            required_weekly_profit=m["required_weekly_profit"],
            current_weekly_profit=m["current_weekly_profit"],
            weekly_profit_percent=m["weekly_profit_percent"],
            target_completion_percent=m["target_completion_percent"],
            remaining_profit_needed=m["remaining_profit_needed"],
            remaining_trading_days=rem_days,
            required_daily_return_percent=rdr,
        )

    def expectancy_stats(self) -> dict:
        return expectancy_from_trades(self.trade_pnls)

    def evaluate_target_realism(self, now_ts: float) -> tuple[TargetStatus, str]:
        m = self.metrics(now_ts)
        stats = self.expectancy_stats()

        if self.week_completed or m.target_completion_percent >= 100:
            return TargetStatus.COMPLETED, "completed"

        # Negative recent expectancy => not realistic (and should not trade live).
        if stats["trades"] >= self.cfg.min_trades_for_expectancy and stats["expectancy"] < 0:
            return TargetStatus.UNREALISTIC, "negative_expectancy"

        if m.required_daily_return_percent > self.cfg.required_daily_return_unrealistic_percent:
            return TargetStatus.UNREALISTIC, "unrealistic_under_current_risk_limits"

        if m.target_completion_percent >= 50:
            return TargetStatus.ON_TRACK, "realistic"
        if m.weekly_profit_percent < 0:
            return TargetStatus.BEHIND_TARGET, "behind"
        return TargetStatus.ASPIRATIONAL, "aspirational"

    # ---------------------------------------------------------- safe mode
    def safe_mode(self, volatility_abnormal: bool = False) -> tuple[SafeMode, float, float]:
        """Return (mode, risk_multiplier, min_quality_score)."""
        wl = self.weekly_loss_percent()
        dl = self.daily_loss_percent()

        # LOCKED conditions
        if (self.kill_switch_active or self.week_completed or self.profit_locked
                or wl >= self.cfg.max_weekly_loss_percent
                or dl >= self.cfg.max_daily_loss_percent
                or self.drawdown_percent() >= self.cfg.max_total_drawdown_percent):
            return SafeMode.LOCKED, 0.0, 0.0

        # DEFENSIVE
        if wl > 3.5 or dl > 1.5 or volatility_abnormal:
            return SafeMode.DEFENSIVE, 0.25, self.cfg.defensive_min_quality_score
        # CAUTION
        if wl > 2.0 or dl > 1.0 or self.consecutive_losses >= 2:
            return SafeMode.CAUTION, 0.5, self.cfg.caution_min_quality_score
        return SafeMode.NORMAL, 1.0, 0.0

    # ------------------------------------------------------- target complete
    def _complete_week(self, now_ts: float) -> None:
        if self.week_completed:
            return
        self.week_completed = True
        self.profit_locked = True
        self._alert("Weekly target reached", "Locking profit; stopping new trades for the week.")
        for fn in (self._cancel_orders, self._close_positions):
            if fn:
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    logger.error("target-complete action failed: %s", exc)

    # --------------------------------------------------------- check_trade
    def check_trade(self, now_ts: float, volatility_abnormal: bool = False) -> WeeklyDecision:
        m = self.metrics(now_ts)
        status, _realism = self.evaluate_target_realism(now_ts)

        if self.kill_switch_active:
            return WeeklyDecision(False, "kill_switch_active", 0.0, 0.0, SafeMode.LOCKED, TargetStatus.KILLED)

        # Target reached -> reject even if the governor would approve.
        plock_mult, stop_lock, _level = profit_lock(m.weekly_profit_percent, self.cfg.profit_lock_levels)
        if (stop_lock and self.cfg.stop_trading_after_target_reached) or self.week_completed:
            self._complete_week(now_ts)
            return WeeklyDecision(False, "weekly_target_reached", 0.0, 0.0, SafeMode.LOCKED, TargetStatus.COMPLETED)

        # Risk-first locks (never overridden by the target).
        if self.drawdown_percent() >= self.cfg.max_total_drawdown_percent:
            return WeeklyDecision(False, "max_drawdown", 0.0, 0.0, SafeMode.LOCKED, TargetStatus.LOCKED_DUE_TO_DRAWDOWN)
        if self.weekly_loss_percent() >= self.cfg.max_weekly_loss_percent:
            return WeeklyDecision(False, "weekly_loss_limit", 0.0, 0.0, SafeMode.LOCKED, TargetStatus.LOCKED_DUE_TO_LOSS)
        if self.daily_loss_percent() >= self.cfg.max_daily_loss_percent:
            return WeeklyDecision(False, "daily_loss_limit", 0.0, 0.0, SafeMode.LOCKED, TargetStatus.LOCKED_DUE_TO_LOSS)
        if self.consecutive_losses >= 3:
            return WeeklyDecision(False, "consecutive_losses", 0.0, 0.0, SafeMode.LOCKED, status)

        # Never trade live on negative recent expectancy.
        stats = self.expectancy_stats()
        if stats["trades"] >= self.cfg.min_trades_for_expectancy and stats["expectancy"] < 0:
            return WeeklyDecision(False, "negative_expectancy_switch_to_observation",
                                  0.0, 0.0, SafeMode.LOCKED, TargetStatus.UNREALISTIC)

        # Trade-frequency control (do not chase the target by overtrading).
        if len(self.trade_times_day) >= self.cfg.max_trades_per_day:
            return WeeklyDecision(False, "max_trades_per_day", 0.0, 0.0, SafeMode.LOCKED, status)
        if len(self.trade_times_week) >= self.cfg.max_trades_per_week:
            return WeeklyDecision(False, "max_trades_per_week", 0.0, 0.0, SafeMode.LOCKED, status)
        if self.last_trade_time and (now_ts - self.last_trade_time) < self.cfg.minimum_minutes_between_trades * 60:
            return WeeklyDecision(False, "min_spacing_between_trades", 0.0, 0.0, SafeMode.CAUTION, status)
        if now_ts < self.cooldown_until:
            return WeeklyDecision(False, "cooldown_active", 0.0, 0.0, SafeMode.CAUTION, status)

        # Safe-mode risk tightening (combine with profit-lock multiplier).
        mode, sm_mult, sm_min_score = self.safe_mode(volatility_abnormal)
        if mode == SafeMode.LOCKED:
            return WeeklyDecision(False, "safe_mode_locked", 0.0, 0.0, SafeMode.LOCKED, status)

        risk_multiplier = min(plock_mult, sm_mult)
        if risk_multiplier <= 0:
            return WeeklyDecision(False, "risk_multiplier_zero", 0.0, sm_min_score, mode, status)

        return WeeklyDecision(True, "ok", risk_multiplier, sm_min_score, mode, status)

    # ----------------------------------------------------------- dashboard
    def dashboard(self, now_ts: float, risk_mode: Optional[str] = None) -> dict:
        m = self.metrics(now_ts)
        status, realism = self.evaluate_target_realism(now_ts)
        decision = self.check_trade(now_ts)
        return dashboard_dict(
            m,
            target_status=status.value,
            target_realism=realism,
            risk_mode=risk_mode or decision.safe_mode.value,
            trading_allowed=decision.allow,
            profit_locked=self.profit_locked,
            weekly_loss_limit_reached=self.weekly_loss_percent() >= self.cfg.max_weekly_loss_percent,
            daily_loss_limit_reached=self.daily_loss_percent() >= self.cfg.max_daily_loss_percent,
            kill_switch_active=self.kill_switch_active,
        )

    # ------------------------------------------------------------- reports
    def daily_report(self, now_ts: float) -> dict:
        m = self.metrics(now_ts)
        stats = self.expectancy_stats()
        decision = self.check_trade(now_ts)
        return {
            "type": "daily",
            "weekly_start_balance": self.weekly_start_balance,
            "current_equity": self.current_equity,
            "weekly_target_balance": m.weekly_target_balance,
            "current_weekly_profit": m.current_weekly_profit,
            "daily_pnl": self.realized_pnl_day,
            "target_completion_percent": m.target_completion_percent,
            "trades_today": len(self.trade_times_day),
            "trades_this_week": len(self.trade_times_week),
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy"],
            "max_drawdown_percent": self.drawdown_percent(),
            "risk_mode": decision.safe_mode.value,
            "trading_allowed": decision.allow,
            "lock_reason": "" if decision.allow else decision.reason,
            "target_realism": self.evaluate_target_realism(now_ts)[1],
        }

    def weekly_report(self, now_ts: float) -> dict:
        m = self.metrics(now_ts)
        stats = self.expectancy_stats()
        top_reasons = sorted(self.rejection_reasons.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "type": "weekly",
            "reached_target": m.target_completion_percent >= 100 or self.week_completed,
            "highest_equity": self.max_equity_week,
            "final_equity": self.current_equity,
            "weekly_return_percent": m.weekly_profit_percent,
            "max_drawdown_percent": self.drawdown_percent(),
            "trades": len(self.trade_times_week),
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy"],
            "biggest_loss": self.biggest_loss,
            "biggest_win": self.biggest_win,
            "rejected_trades": self.rejected_count,
            "main_rejection_reasons": top_reasons,
            "target_realistic": self.evaluate_target_realism(now_ts)[0] != TargetStatus.UNREALISTIC,
        }
