"""RiskGovernor — the single trade-approval authority.

    Strategy Engine -> RiskGovernor.approve_trade() -> Execution Engine -> BingX

No order may be placed unless approve_trade() returns an approved ApprovalResult.
The governor owns all risk state (kill switch, cooldowns, drawdown locks,
consecutive losses, equity peak, daily/weekly realized PnL) and fails CLOSED.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import checks
from .alerts import AlertLevel, send_alert
from .audit import audit
from .config import RiskConfig, load_config
from .models import (
    AccountSnapshot,
    ApprovalResult,
    CheckResult,
    Decision,
    MarketSnapshot,
    RiskMode,
    RiskStatus,
    TradeSignal,
    TradingMode,
)


def _utc_date(ts: float):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _utc_week(ts: float):
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar()
    return (iso[0], iso[1])


class RiskGovernor:
    def __init__(self, config: Optional[RiskConfig] = None,
                 cancel_all_orders: Optional[Callable[[], None]] = None,
                 close_all_positions: Optional[Callable[[], None]] = None):
        self.cfg = config or load_config()
        self._cancel_all_orders = cancel_all_orders
        self._close_all_positions = close_all_positions

        now = time.time()
        # Trading enablement / kill state
        self.trading_enabled = True
        self.kill_switch_active = False
        self.manual_restart_required = False
        self.reconciliation_error = False
        self.news_pause = bool(self.cfg.news_pause)

        # Loss / cooldown state
        self.consecutive_losses = 0
        self.size_multiplier = 1.0
        self.cooldown_until = 0.0
        self.daily_locked = False
        self.weekly_locked = False
        self.last_trade_was_loss = False
        self.prev_position_value: Optional[float] = None

        # Equity / PnL tracking
        self.equity_peak = 0.0
        self.current_equity = 0.0
        self.day = _utc_date(now)
        self.week = _utc_week(now)
        self.day_start_equity = 0.0
        self.week_start_equity = 0.0
        self.daily_realized_pnl = 0.0
        self.weekly_realized_pnl = 0.0

        # Error counters (rolling windows)
        self._api_errors: list[float] = []
        self._order_rejections: list[float] = []

        # Recent signals (duplicate detection) and last account snapshot
        self._recent_signals: list[dict] = []
        self._last_account: Optional[AccountSnapshot] = None

        # Status strings
        self.last_rejection_reason = ""
        self.last_approved_trade = ""
        self.last_kill_switch_reason = ""

    # ------------------------------------------------------------------ modes
    @property
    def trading_mode(self) -> str:
        return self.cfg.trading_mode

    def set_trading_mode(self, mode: str) -> None:
        mode = TradingMode(mode).value  # validate
        self.cfg.trading_mode = mode
        audit("trading_mode_changed", trading_mode=mode)

    def set_news_pause(self, paused: bool) -> None:
        self.news_pause = bool(paused)
        audit("news_pause_set", news_pause=self.news_pause)
        if paused:
            send_alert(AlertLevel.WARNING, "News pause activated",
                       "New entries are paused; existing positions still managed.", {})

    def current_mode(self) -> RiskMode:
        if self.kill_switch_active or self.manual_restart_required:
            return RiskMode.KILLED
        if self.reconciliation_error:
            return RiskMode.RECONCILIATION_ERROR
        if self.news_pause:
            return RiskMode.NEWS_PAUSE
        if time.time() < self.cooldown_until:
            return RiskMode.COOLDOWN
        if self.daily_locked or self.weekly_locked or not self.trading_enabled:
            return RiskMode.LOCKED
        if self.consecutive_losses > 0 or self.size_multiplier < 1.0:
            return RiskMode.CAUTION
        return RiskMode.NORMAL

    # ------------------------------------------------------------- kill switch
    def emergency_kill_switch(self, reason: str) -> None:
        if self.kill_switch_active:
            return
        self.kill_switch_active = True
        self.trading_enabled = False
        self.manual_restart_required = bool(self.cfg.manual_restart_required_after_kill_switch)
        self.last_kill_switch_reason = reason
        audit("kill_switch", reason=reason)
        send_alert(AlertLevel.KILL_SWITCH, "KILL SWITCH ACTIVATED", reason, {})
        # Cancel orders and (best-effort) flatten unsafe positions.
        for fn, label in ((self._cancel_all_orders, "cancel_all_orders"),
                          (self._close_all_positions, "close_all_positions")):
            if fn:
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    audit("kill_switch_action_failed", action=label, error=str(exc))

    def manual_restart(self, confirm: bool = False) -> bool:
        """Operator-only reset after a kill switch. Requires explicit confirm."""
        if not confirm:
            return False
        self.kill_switch_active = False
        self.manual_restart_required = False
        self.reconciliation_error = False
        self.trading_enabled = True
        self.cooldown_until = 0.0
        self.daily_locked = False
        self.weekly_locked = False
        self.consecutive_losses = 0
        self.size_multiplier = 1.0
        audit("manual_restart", confirmed=True)
        send_alert(AlertLevel.WARNING, "Manual restart", "Risk Governor reset by operator.", {})
        return True

    # ---------------------------------------------------------- error tracking
    def note_api_error(self, now_ts: Optional[float] = None) -> None:
        now_ts = now_ts or time.time()
        self._api_errors = [t for t in self._api_errors if now_ts - t < self.cfg.api_error_window_seconds]
        self._api_errors.append(now_ts)
        if len(self._api_errors) >= self.cfg.api_error_threshold:
            self.emergency_kill_switch("api_error_threshold_exceeded")

    def note_order_rejection(self, now_ts: Optional[float] = None) -> None:
        now_ts = now_ts or time.time()
        self._order_rejections = [t for t in self._order_rejections
                                  if now_ts - t < self.cfg.order_rejection_window_seconds]
        self._order_rejections.append(now_ts)
        if len(self._order_rejections) >= self.cfg.order_rejection_threshold:
            self.emergency_kill_switch("order_rejection_threshold_exceeded")

    # --------------------------------------------------------- equity tracking
    def update_equity(self, equity: float, now_ts: Optional[float] = None) -> None:
        now_ts = now_ts or time.time()
        # Rollovers reset realized PnL buckets and clear time-based locks.
        d, w = _utc_date(now_ts), _utc_week(now_ts)
        if d != self.day:
            self.day = d
            self.day_start_equity = equity
            self.daily_realized_pnl = 0.0
            self.daily_locked = False
        if w != self.week:
            self.week = w
            self.week_start_equity = equity
            self.weekly_realized_pnl = 0.0
            self.weekly_locked = False

        if self.day_start_equity <= 0:
            self.day_start_equity = equity
        if self.week_start_equity <= 0:
            self.week_start_equity = equity

        self.current_equity = equity
        self.equity_peak = max(self.equity_peak, equity)

        dd = self.current_drawdown_percent()
        if dd >= self.cfg.max_total_drawdown_percent:
            self.emergency_kill_switch(f"max_total_drawdown {dd:.2f}%>={self.cfg.max_total_drawdown_percent}%")

    def current_drawdown_percent(self) -> float:
        if self.equity_peak <= 0:
            return 0.0
        return max(0.0, (self.equity_peak - self.current_equity) / self.equity_peak * 100.0)

    # ----------------------------------------------------------- trade results
    def record_trade_result(self, realized_pnl: float, position_value: Optional[float] = None,
                            now_ts: Optional[float] = None) -> None:
        now_ts = now_ts or time.time()
        self.daily_realized_pnl += realized_pnl
        self.weekly_realized_pnl += realized_pnl
        if position_value is not None:
            self.prev_position_value = position_value

        if realized_pnl < 0:
            self.consecutive_losses += 1
            self.last_trade_was_loss = True
            self.size_multiplier = 0.5  # halve next size after a loss
            self.cooldown_until = max(self.cooldown_until,
                                      now_ts + self.cfg.cooldown_after_loss_minutes * 60)
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.cooldown_until = now_ts + self.cfg.cooldown_after_consecutive_losses_hours * 3600
                send_alert(AlertLevel.CRITICAL, "Consecutive-loss cooldown",
                           f"{self.consecutive_losses} losses -> 24h cooldown.", {})
        else:
            self.consecutive_losses = 0
            self.last_trade_was_loss = False
            self.size_multiplier = 1.0

        # Daily / weekly realized-loss locks.
        base_day = self.day_start_equity or self.current_equity or self.equity_peak
        base_week = self.week_start_equity or self.current_equity or self.equity_peak
        if base_day > 0 and -self.daily_realized_pnl >= base_day * self.cfg.max_daily_loss_percent / 100.0:
            if not self.daily_locked:
                self.daily_locked = True
                self.cooldown_until = max(self.cooldown_until,
                                          now_ts + self.cfg.cooldown_after_max_loss_hours * 3600)
                send_alert(AlertLevel.CRITICAL, "Daily loss limit reached",
                           "New trades blocked until next day.", {})
        if base_week > 0 and -self.weekly_realized_pnl >= base_week * self.cfg.max_weekly_loss_percent / 100.0:
            if not self.weekly_locked:
                self.weekly_locked = True
                send_alert(AlertLevel.CRITICAL, "Weekly loss limit reached",
                           "New trades blocked until next week.", {})

        audit("trade_result", realized_pnl=realized_pnl, consecutive_losses=self.consecutive_losses,
              daily_realized_pnl=self.daily_realized_pnl, weekly_realized_pnl=self.weekly_realized_pnl,
              size_multiplier=self.size_multiplier)

    # ------------------------------------------------------------ reconciliation
    def reconcile(self, local_state: dict, exchange_state: dict) -> bool:
        """Compare local vs exchange state. On mismatch: stop new trading and,
        if unrecoverable, kill. Returns True if reconciled OK."""
        keys = ["open_orders", "open_positions", "balance", "leverage", "margin_mode"]
        mismatches = []
        for k in keys:
            if k in local_state and k in exchange_state and local_state[k] != exchange_state[k]:
                mismatches.append(k)
        if mismatches:
            self.reconciliation_error = True
            self.trading_enabled = False
            audit("reconciliation_mismatch", mismatches=mismatches,
                  local=local_state, exchange=exchange_state)
            send_alert(AlertLevel.CRITICAL, "Reconciliation mismatch",
                       f"Fields differ: {mismatches}", {"local": local_state, "exchange": exchange_state})
            return False
        self.reconciliation_error = False
        return True

    # --------------------------------------------------------------- approval
    def _reject(self, reason: str, mode: RiskMode, checks_list: list[CheckResult],
                signal: TradeSignal, now_ts: float) -> ApprovalResult:
        self.last_rejection_reason = reason
        audit("trade_rejected", reason=reason, risk_mode=mode.value,
              symbol=signal.symbol, side=signal.side, signal_id=signal.signal_id,
              balance=(self._last_account.balance if self._last_account else None),
              config={"max_risk_per_trade_percent": self.cfg.max_risk_per_trade_percent})
        return ApprovalResult(Decision.REJECTED, reason=reason, risk_mode=mode, checks=checks_list)

    def approve_trade(self, signal: TradeSignal, account: AccountSnapshot,
                      market: MarketSnapshot, now_ts: Optional[float] = None,
                      risk_multiplier: float = 1.0,
                      min_quality_override: Optional[float] = None) -> ApprovalResult:
        """Approve/reject a trade.

        `risk_multiplier` (<=1) and `min_quality_override` let an upstream layer
        (e.g. the Weekly Target Manager) TIGHTEN risk only — they can shrink size
        and raise the quality bar, never loosen them. The governor remains the
        final authority.
        """
        now_ts = now_ts or time.time()
        # External multiplier may only reduce exposure (clamped to [0, 1]).
        risk_multiplier = max(0.0, min(1.0, risk_multiplier))
        self._last_account = account
        if account and account.known:
            self.update_equity(account.equity or account.balance, now_ts)

        results: list[CheckResult] = []

        def run(cr: CheckResult) -> Optional[ApprovalResult]:
            results.append(cr)
            if not cr.passed:
                return self._reject(cr.reason, self.current_mode(), results, signal, now_ts)
            return None

        # --- state gates (fail closed) ---
        if self.kill_switch_active or self.manual_restart_required:
            return self._reject("kill_switch_active", RiskMode.KILLED, results, signal, now_ts)
        if self.reconciliation_error:
            return self._reject("reconciliation_error", RiskMode.RECONCILIATION_ERROR, results, signal, now_ts)
        if not self.trading_enabled:
            return self._reject("trading_disabled", RiskMode.LOCKED, results, signal, now_ts)
        if self.news_pause or self.cfg.news_pause:
            return self._reject("news_pause", RiskMode.NEWS_PAUSE, results, signal, now_ts)
        if now_ts < self.cooldown_until:
            return self._reject("cooldown_active", RiskMode.COOLDOWN, results, signal, now_ts)
        if self.daily_locked:
            return self._reject("daily_loss_lock", RiskMode.LOCKED, results, signal, now_ts)
        if self.weekly_locked:
            return self._reject("weekly_loss_lock", RiskMode.LOCKED, results, signal, now_ts)

        # --- data availability ---
        for cr in (checks.check_signal_complete(signal, self.cfg),
                   checks.check_account_known(account),
                   checks.check_market_valid(market, self.cfg, now_ts)):
            r = run(cr)
            if r is not None:
                return r

        # --- account-level limits ---
        if account.open_positions >= self.cfg.max_open_positions and \
                signal.symbol not in (account.open_symbols or ()):
            return self._reject("max_open_positions", RiskMode.LOCKED, results, signal, now_ts)
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return self._reject("max_consecutive_losses", RiskMode.COOLDOWN, results, signal, now_ts)

        # --- duplicate / anti-martingale / leverage / protective orders ---
        for cr in (checks.check_duplicate(signal, account, self._recent_signals, self.cfg, now_ts),
                   checks.check_leverage_margin(signal, self.cfg),
                   checks.check_no_averaging_down(signal, account, self.cfg),
                   checks.check_stop_take_profit(signal, self.cfg),
                   checks.check_stop_distance(signal, self.cfg),
                   checks.check_risk_reward(signal, self.cfg),
                   checks.check_spread(market, self.cfg),
                   checks.check_slippage(market, self.cfg),
                   checks.check_volatility(market, self.cfg)):
            r = run(cr)
            if r is not None:
                return r

        # --- position sizing (governor authority; external multiplier tightens) ---
        effective_size_mult = self.size_multiplier * risk_multiplier
        sizing = checks.compute_position_size(signal, account, self.cfg, effective_size_mult)
        results.append(CheckResult("position_sizing", sizing.ok, sizing.reason))
        if not sizing.ok:
            return self._reject(sizing.reason, self.current_mode(), results, signal, now_ts)

        # Trade-quality threshold: governor's config, raised (never lowered) by override.
        effective_min_quality = self.cfg.trade_quality_min_score
        if min_quality_override is not None:
            effective_min_quality = max(effective_min_quality, min_quality_override)
        score = checks.trade_quality_score(signal, market, self.cfg)
        quality_cr = CheckResult(
            "trade_quality", score >= effective_min_quality,
            "" if score >= effective_min_quality else f"score {score:.0f}<{effective_min_quality:.0f}")

        for cr in (checks.check_no_martingale(sizing.position_value, self.prev_position_value,
                                              self.last_trade_was_loss, self.cfg),
                   checks.check_exposure(sizing.position_value, account, self.cfg),
                   quality_cr):
            r = run(cr)
            if r is not None:
                return r

        # --- APPROVED ---
        rr = checks.compute_risk_reward(signal) or 0.0
        score = checks.trade_quality_score(signal, market, self.cfg)
        self._recent_signals.append({"signal_id": signal.signal_id, "symbol": signal.symbol,
                                     "side": signal.side, "ts": now_ts})
        self._recent_signals = self._recent_signals[-200:]
        self.last_approved_trade = f"{signal.symbol} {signal.side} qty={sizing.quantity:.8f}"
        audit("trade_approved", symbol=signal.symbol, side=signal.side, signal_id=signal.signal_id,
              quantity=sizing.quantity, position_value=sizing.position_value,
              risk_amount=sizing.risk_amount, risk_reward=rr, trade_quality_score=score,
              balance=account.balance, size_multiplier=self.size_multiplier,
              config={"max_risk_per_trade_percent": self.cfg.max_risk_per_trade_percent,
                      "max_leverage": self.cfg.max_leverage})
        return ApprovalResult(
            Decision.APPROVED, reason="approved", quantity=sizing.quantity,
            position_value=sizing.position_value, risk_amount=sizing.risk_amount,
            risk_reward_ratio=rr, trade_quality_score=score,
            risk_mode=self.current_mode(), checks=results,
        )

    # ------------------------------------------------------------- status
    def risk_status(self) -> RiskStatus:
        acct = self._last_account
        return RiskStatus(
            trading_enabled=self.trading_enabled,
            risk_mode=self.current_mode().value,
            trading_mode=self.cfg.trading_mode,
            account_balance=acct.balance if acct else 0.0,
            available_margin=acct.available_margin if acct else 0.0,
            daily_pnl=self.daily_realized_pnl,
            weekly_pnl=self.weekly_realized_pnl,
            current_drawdown_percent=self.current_drawdown_percent(),
            equity_peak=self.equity_peak,
            consecutive_losses=self.consecutive_losses,
            open_positions=acct.open_positions if acct else 0,
            open_orders=acct.open_orders if acct else 0,
            risk_per_trade_percent=self.cfg.max_risk_per_trade_percent,
            max_daily_loss_percent=self.cfg.max_daily_loss_percent,
            max_weekly_loss_percent=self.cfg.max_weekly_loss_percent,
            max_total_drawdown_percent=self.cfg.max_total_drawdown_percent,
            max_leverage=self.cfg.max_leverage,
            max_capital_exposure_percent=self.cfg.max_capital_exposure_percent,
            news_pause=self.news_pause,
            last_rejection_reason=self.last_rejection_reason,
            last_approved_trade=self.last_approved_trade,
            last_kill_switch_reason=self.last_kill_switch_reason,
            kill_switch_active=self.kill_switch_active,
            manual_restart_required=self.manual_restart_required,
        )
