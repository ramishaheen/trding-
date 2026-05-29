"""Tests for the Weekly Target Manager + the risk-first trade pipeline."""

import time

import pytest

from weekly_target_manager import WeeklyTargetManager, load_weekly_config
from weekly_target_manager.calculations import (
    expectancy,
    expectancy_from_trades,
    profit_lock,
    required_daily_return_percent,
    weekly_metrics,
)
from weekly_target_manager.models import SafeMode, TargetStatus

# A Monday 00:30 UTC timestamp so weekday math is stable.
MON = 1_700_006_400.0  # 2023-11-15 is Wed; we just need a fixed reference
NOW = time.time()


# ---------------------------------------------------------------------------
# Pure calculations
# ---------------------------------------------------------------------------
def test_weekly_metrics_example():
    m = weekly_metrics(100, 120, 4.0)
    assert m["weekly_target_balance"] == 400
    assert m["required_weekly_profit"] == 300
    assert m["current_weekly_profit"] == 20
    assert m["weekly_profit_percent"] == pytest.approx(20.0)
    assert m["target_completion_percent"] == pytest.approx(6.666, rel=1e-3)


def test_required_daily_return_zero_when_at_target():
    assert required_daily_return_percent(400, 400, 5) == 0.0


def test_required_daily_return_positive_and_high_for_4x():
    rdr = required_daily_return_percent(100, 400, 5)  # 4x in 5 days
    assert rdr > 30  # extraordinary -> will be flagged unrealistic


def test_profit_lock_levels():
    levels = load_weekly_config().profit_lock_levels
    assert profit_lock(10, levels) == (1.0, False, None)
    assert profit_lock(25, levels)[0] == 0.75
    assert profit_lock(50, levels)[0] == 0.50
    assert profit_lock(100, levels)[0] == 0.25
    mult, stop, _ = profit_lock(300, levels)
    assert mult == 0.0 and stop is True


def test_expectancy_sign():
    assert expectancy(0.5, 2.0, 1.0) == pytest.approx(0.5)   # positive
    assert expectancy(0.3, 1.0, 2.0) < 0                     # negative


def test_expectancy_from_trades():
    stats = expectancy_from_trades([10, -5, 20, -5])
    assert stats["trades"] == 4
    assert stats["win_rate"] == 0.5
    assert stats["profit_factor"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Manager behaviour
# ---------------------------------------------------------------------------
def mgr() -> WeeklyTargetManager:
    return WeeklyTargetManager(config=load_weekly_config())


def test_update_sets_weekly_start_and_metrics():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    metrics = m.metrics(NOW)
    assert metrics.weekly_start_balance == 100
    assert metrics.weekly_target_balance == 400
    assert metrics.required_weekly_profit == 300


def test_target_reached_blocks_trading_and_locks():
    closed = {"orders": False, "positions": False}
    m = WeeklyTargetManager(config=load_weekly_config(),
                            cancel_orders=lambda: closed.__setitem__("orders", True),
                            close_positions=lambda: closed.__setitem__("positions", True))
    m.update(equity=100, balance=100, now_ts=NOW)
    m.update(equity=400, balance=400, now_ts=NOW)   # +300% => 4x reached
    d = m.check_trade(NOW)
    assert not d.allow and d.reason == "weekly_target_reached"
    assert m.week_completed and m.profit_locked
    assert closed["orders"] and closed["positions"]


def test_profit_lock_reduces_risk_multiplier():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    m.update(equity=150, balance=150, now_ts=NOW)   # +50% weekly profit
    d = m.check_trade(NOW)
    assert d.allow
    assert d.risk_multiplier <= 0.5  # at least the 50% profit-lock reduction


def test_weekly_loss_limit_blocks():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    m.update(equity=94.9, balance=94.9, now_ts=NOW)  # -5.1% weekly
    d = m.check_trade(NOW)
    assert not d.allow and d.reason == "weekly_loss_limit"
    assert d.target_status == TargetStatus.LOCKED_DUE_TO_LOSS


def test_daily_loss_limit_blocks():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    m.update(equity=97.9, balance=97.9, now_ts=NOW)  # -2.1% daily
    d = m.check_trade(NOW)
    assert not d.allow and d.reason == "daily_loss_limit"


def test_drawdown_blocks():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    m.update(equity=91, balance=91, now_ts=NOW, drawdown_percent=9.0)
    d = m.check_trade(NOW)
    assert not d.allow and d.reason == "max_drawdown"


def test_overtrading_blocked_per_day():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    for i in range(m.cfg.max_trades_per_day):
        m.trade_times_day.append(NOW)
    d = m.check_trade(NOW)
    assert not d.allow and d.reason == "max_trades_per_day"


def test_min_spacing_between_trades():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    m.last_trade_time = NOW - 60  # 1 minute ago, < 60 min spacing
    d = m.check_trade(NOW)
    assert not d.allow and d.reason == "min_spacing_between_trades"


def test_negative_expectancy_switches_to_observation():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    # enough losing trades to make expectancy negative
    for _ in range(m.cfg.min_trades_for_expectancy):
        m.trade_pnls.append(-1.0)
    d = m.check_trade(NOW)
    assert not d.allow and "negative_expectancy" in d.reason


def test_caution_mode_reduces_risk_and_raises_quality():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    m.consecutive_losses = 2   # caution trigger without hitting a hard loss lock
    mode, mult, min_score = m.safe_mode()
    assert mode == SafeMode.CAUTION
    assert mult <= 0.5
    assert min_score >= 85


def test_defensive_mode_on_volatility_spike():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    mode, mult, min_score = m.safe_mode(volatility_abnormal=True)
    assert mode == SafeMode.DEFENSIVE
    assert mult <= 0.25 and min_score >= 90


def test_target_realism_flags_unrealistic_for_fresh_4x():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    status, realism = m.evaluate_target_realism(NOW)
    # 4x in a few days at 0.5% risk is not realistic
    assert status == TargetStatus.UNREALISTIC
    assert "unrealistic" in realism or realism == "negative_expectancy"


def test_dashboard_object_shape():
    m = mgr()
    m.update(equity=100, balance=100, now_ts=NOW)
    dash = m.dashboard(NOW)
    for key in ["weekly_start_balance", "weekly_target_balance", "required_weekly_profit",
                "target_completion_percent", "remaining_trading_days",
                "required_daily_return_percent", "target_status", "risk_mode",
                "trading_allowed", "profit_locked", "kill_switch_active"]:
        assert key in dash


def test_allows_normal_trade_with_full_multiplier():
    m = mgr()
    m.update(equity=105, balance=105, now_ts=NOW)  # small profit, no locks
    d = m.check_trade(NOW)
    assert d.allow
    assert d.risk_multiplier == 1.0
    assert d.safe_mode == SafeMode.NORMAL


# ---------------------------------------------------------------------------
# Pipeline: Weekly Target Manager -> Risk Governor (governor final authority)
# ---------------------------------------------------------------------------
def _signal_account_market():
    from risk_governor.models import AccountSnapshot, MarketSnapshot, TradeSignal
    signal = TradeSignal(
        symbol="BTC/USDT", side="long", entry_price=100.0, stop_loss_price=99.0,
        take_profit_price=102.0, quantity=0.1, leverage=1, margin_mode="isolated",
        max_holding_time_minutes=60, strategy_reason="test", timestamp=NOW, signal_id="s1",
        quality_components={k: 95 for k in [
            "trend_alignment", "htf_confirmation", "volume", "recent_performance",
            "news_risk", "orderbook_quality", "regime_quality"]},
    )
    account = AccountSnapshot(known=True, balance=100, equity=100, available_margin=100,
                             open_positions=0, open_orders=0, open_symbols=(),
                             margin_mode_confirmed=True, leverage_confirmed=True)
    market = MarketSnapshot(known=True, bid=100.0, ask=100.02, last_price=100.01,
                            price_timestamp=NOW, atr=1.0, atr_avg_20=1.0,
                            orderbook_depth_quote=1000.0, estimated_slippage_percent=0.01)
    return signal, account, market


def test_pipeline_both_approve_executes():
    from risk_governor import RiskGovernor, load_config
    from trade_pipeline import evaluate_trade
    wtm = mgr()
    gov = RiskGovernor(config=load_config())
    s, a, mk = _signal_account_market()
    res = evaluate_trade(wtm, gov, s, a, mk, now_ts=NOW)
    assert res.approved and res.quantity > 0


def test_pipeline_governor_has_final_authority():
    """Weekly says ok, but governor rejects (bad leverage) -> no trade."""
    from risk_governor import RiskGovernor, load_config
    from trade_pipeline import evaluate_trade
    wtm = mgr()
    gov = RiskGovernor(config=load_config())
    s, a, mk = _signal_account_market()
    s.leverage = 10  # governor will reject
    res = evaluate_trade(wtm, gov, s, a, mk, now_ts=NOW)
    assert not res.approved and res.reason.startswith("governor:")


def test_pipeline_weekly_target_reached_blocks_even_if_governor_would_approve():
    from risk_governor import RiskGovernor, load_config
    from trade_pipeline import evaluate_trade
    wtm = mgr()
    gov = RiskGovernor(config=load_config())
    s, a, mk = _signal_account_market()
    # Drive account to 4x so the weekly manager reports completed.
    a.balance = 400
    a.equity = 400
    wtm.update(equity=100, balance=100, now_ts=NOW)  # establish week start at 100
    res = evaluate_trade(wtm, gov, s, a, mk, now_ts=NOW)
    assert not res.approved and res.reason.startswith("weekly:")
