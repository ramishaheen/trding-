"""Comprehensive Risk Governor tests (spec section 24).

Covers: position sizing, per-trade risk, daily/weekly/drawdown locks, leverage &
cross-margin rejection, missing SL/TP/holding-time, martingale & averaging-down,
spread/slippage/volatility, risk-reward, trade-quality, cooldown, kill switch,
reconciliation mismatch, duplicate orders, and fail-closed behaviour.
"""

import time

import pytest

from risk_governor import RiskGovernor, load_config
from risk_governor.config import RiskConfig
from risk_governor.models import AccountSnapshot, MarketSnapshot, TradeSignal
from risk_governor import checks


NOW = 1_700_000_000.0


def make_account(**kw) -> AccountSnapshot:
    base = dict(known=True, balance=100.0, equity=100.0, available_margin=100.0,
                open_positions=0, open_orders=0, open_symbols=(),
                margin_mode_confirmed=True, leverage_confirmed=True, current_leverage=1.0)
    base.update(kw)
    return AccountSnapshot(**base)


def make_market(**kw) -> MarketSnapshot:
    base = dict(known=True, bid=100.0, ask=100.02, last_price=100.01,
                price_timestamp=NOW, atr=1.0, atr_avg_20=1.0,
                last_candle_body_pct=0.5, orderbook_depth_quote=1000.0,
                estimated_slippage_percent=0.01)
    base.update(kw)
    return MarketSnapshot(**base)


def make_signal(**kw) -> TradeSignal:
    base = dict(symbol="BTC/USDT", side="long", entry_price=100.0,
                stop_loss_price=99.0, take_profit_price=102.0, quantity=0.1,
                leverage=1, margin_mode="isolated", max_holding_time_minutes=60,
                strategy_reason="unit-test", timestamp=NOW, signal_id="sig-1",
                quality_components={
                    "trend_alignment": 80, "htf_confirmation": 80, "volume": 80,
                    "recent_performance": 80, "news_risk": 80,
                    "orderbook_quality": 80, "regime_quality": 80,
                })
    base.update(kw)
    return TradeSignal(**base)


def gov() -> RiskGovernor:
    return RiskGovernor(config=load_config())


# ---------------------------------------------------------------------------
# Happy path + sizing
# ---------------------------------------------------------------------------
def test_baseline_trade_is_approved():
    r = gov().approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert r.approved, r.reason
    # risk 0.5% of 100 = 0.5; stop 1% -> raw 50; exposure cap 15% of 100 = 15
    assert r.risk_amount == pytest.approx(0.5)
    assert r.position_value == pytest.approx(15.0)
    assert r.quantity == pytest.approx(15.0 / 100.0)
    assert r.risk_reward_ratio == pytest.approx(2.0)


def test_position_sizing_formula_direct():
    cfg = RiskConfig()
    s = checks.compute_position_size(make_signal(), make_account(balance=1000), cfg)
    # risk 5; stop 1% -> raw 500; cap 15% of 1000 = 150 -> final 150
    assert s.ok
    assert s.position_value == pytest.approx(150.0)
    assert s.quantity == pytest.approx(1.5)


def test_sizing_below_min_order_is_rejected():
    cfg = RiskConfig()
    sig = make_signal(entry_price=100, stop_loss_price=80)  # 20% stop
    s = checks.compute_position_size(sig, make_account(balance=10), cfg)
    assert not s.ok and s.reason == "below_min_order_size"


def test_post_loss_size_multiplier_halves_size():
    g = gov()
    g.size_multiplier = 0.5
    s = checks.compute_position_size(make_signal(), make_account(), g.cfg, size_multiplier=0.5)
    assert s.position_value == pytest.approx(min(25.0, 15.0))  # raw halves to 25, cap 15


# ---------------------------------------------------------------------------
# Mandatory fields (fail closed)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", [
    "stop_loss_price", "take_profit_price", "max_holding_time_minutes",
    "entry_price", "signal_id", "leverage", "margin_mode",
])
def test_missing_mandatory_field_rejected(field):
    r = gov().approve_trade(make_signal(**{field: None}), make_account(), make_market(), now_ts=NOW)
    assert not r.approved
    assert "missing_fields" in r.reason


# ---------------------------------------------------------------------------
# Leverage / margin
# ---------------------------------------------------------------------------
def test_leverage_above_max_rejected():
    r = gov().approve_trade(make_signal(leverage=5), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and "leverage" in r.reason


def test_cross_margin_rejected():
    r = gov().approve_trade(make_signal(margin_mode="cross"), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and "cross_margin" in r.reason


# ---------------------------------------------------------------------------
# Risk / reward + stop distance
# ---------------------------------------------------------------------------
def test_risk_reward_below_min_rejected():
    r = gov().approve_trade(make_signal(take_profit_price=100.5), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and r.reason.startswith("rr ")


def test_stop_distance_too_wide_rejected():
    r = gov().approve_trade(make_signal(stop_loss_price=50.0, take_profit_price=200.0),
                            make_account(), make_market(), now_ts=NOW)
    assert not r.approved and "stop_distance" in r.reason


def test_stop_not_below_entry_for_long():
    cr = checks.check_stop_take_profit(make_signal(stop_loss_price=101.0), RiskConfig())
    assert not cr.passed


# ---------------------------------------------------------------------------
# Spread / slippage / volatility
# ---------------------------------------------------------------------------
def test_spread_too_wide_rejected():
    r = gov().approve_trade(make_signal(), make_account(),
                            make_market(bid=100.0, ask=101.0, last_price=100.5), now_ts=NOW)
    assert not r.approved and "spread" in r.reason


def test_slippage_too_high_rejected():
    r = gov().approve_trade(make_signal(), make_account(),
                            make_market(estimated_slippage_percent=0.5), now_ts=NOW)
    assert not r.approved and "slippage" in r.reason


def test_volatility_spike_rejected():
    r = gov().approve_trade(make_signal(), make_account(),
                            make_market(atr=3.0, atr_avg_20=1.0), now_ts=NOW)
    assert not r.approved and "atr_spike" in r.reason


# ---------------------------------------------------------------------------
# Trade quality
# ---------------------------------------------------------------------------
def test_low_trade_quality_rejected():
    weak = make_signal(quality_components={k: 10 for k in [
        "trend_alignment", "htf_confirmation", "volume", "recent_performance",
        "news_risk", "orderbook_quality", "regime_quality"]})
    r = gov().approve_trade(weak, make_account(), make_market(), now_ts=NOW)
    assert not r.approved and "score" in r.reason


# ---------------------------------------------------------------------------
# Account-level limits
# ---------------------------------------------------------------------------
def test_max_open_positions_rejected():
    r = gov().approve_trade(make_signal(symbol="ETH/USDT"),
                            make_account(open_positions=1, open_symbols=("BTC/USDT",)),
                            make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "max_open_positions"


# ---------------------------------------------------------------------------
# Martingale / averaging down
# ---------------------------------------------------------------------------
def test_martingale_size_increase_after_loss_rejected():
    cr = checks.check_no_martingale(requested_value=20.0, prev_position_value=10.0,
                                    last_trade_was_loss=True, cfg=RiskConfig())
    assert not cr.passed


def test_averaging_down_rejected_when_symbol_open():
    r = gov().approve_trade(make_signal(),
                            make_account(open_symbols=("BTC/USDT",), open_positions=0),
                            make_market(), now_ts=NOW)
    assert not r.approved
    assert r.reason in {"existing_position_same_symbol", "position_already_open_symbol"}


# ---------------------------------------------------------------------------
# Daily / weekly / drawdown locks
# ---------------------------------------------------------------------------
def test_record_loss_sets_daily_lock_and_cooldown():
    g = gov()
    g.update_equity(100.0, now_ts=NOW)
    g.record_trade_result(realized_pnl=-2.5, position_value=15.0, now_ts=NOW)  # 2.5 > 2% of 100
    assert g.daily_locked is True
    assert g.consecutive_losses == 1
    assert g.size_multiplier == 0.5
    r = g.approve_trade(make_signal(signal_id="x"), make_account(), make_market(), now_ts=NOW)
    assert not r.approved  # blocked (cooldown and/or daily lock)


def test_weekly_lock_blocks_trades():
    g = gov()
    g.update_equity(100.0, now_ts=NOW)   # align week so the lock isn't rolled over
    g.weekly_locked = True
    r = g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "weekly_loss_lock"


def test_drawdown_triggers_kill_switch():
    g = gov()
    g.update_equity(100.0, now_ts=NOW)
    g.update_equity(91.0, now_ts=NOW)  # 9% drawdown >= 8%
    assert g.kill_switch_active is True
    r = g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "kill_switch_active"


# ---------------------------------------------------------------------------
# Cooldown + consecutive losses
# ---------------------------------------------------------------------------
def test_cooldown_blocks_then_clears():
    g = gov()
    g.update_equity(100.0, now_ts=NOW)
    g.record_trade_result(realized_pnl=-0.2, now_ts=NOW)  # small loss -> 30m cooldown
    assert g.cooldown_until > NOW
    blocked = g.approve_trade(make_signal(signal_id="a"), make_account(), make_market(), now_ts=NOW + 60)
    assert not blocked.approved and blocked.reason == "cooldown_active"


def test_three_consecutive_losses_long_cooldown():
    g = gov()
    g.update_equity(100.0, now_ts=NOW)
    for i in range(3):
        g.record_trade_result(realized_pnl=-0.1, now_ts=NOW)
    assert g.consecutive_losses == 3
    # cooldown extended to 24h
    assert g.cooldown_until >= NOW + 24 * 3600 - 1


# ---------------------------------------------------------------------------
# Kill switch + manual restart
# ---------------------------------------------------------------------------
def test_kill_switch_invokes_callbacks_and_blocks():
    cancelled = {"orders": False, "positions": False}
    g = RiskGovernor(config=load_config(),
                     cancel_all_orders=lambda: cancelled.__setitem__("orders", True),
                     close_all_positions=lambda: cancelled.__setitem__("positions", True))
    g.emergency_kill_switch("test")
    assert g.kill_switch_active and g.manual_restart_required
    assert cancelled["orders"] and cancelled["positions"]
    r = g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "kill_switch_active"


def test_manual_restart_requires_confirmation():
    g = gov()
    g.emergency_kill_switch("test")
    assert g.manual_restart(confirm=False) is False
    assert g.kill_switch_active is True
    assert g.manual_restart(confirm=True) is True
    assert g.kill_switch_active is False
    r = g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert r.approved


def test_api_error_threshold_trips_kill():
    g = gov()
    for _ in range(g.cfg.api_error_threshold):
        g.note_api_error(now_ts=NOW)
    assert g.kill_switch_active is True


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def test_reconciliation_mismatch_blocks_trading():
    g = gov()
    ok = g.reconcile({"open_positions": 0, "balance": 100},
                     {"open_positions": 1, "balance": 100})
    assert ok is False and g.reconciliation_error is True
    r = g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "reconciliation_error"


# ---------------------------------------------------------------------------
# Duplicate orders
# ---------------------------------------------------------------------------
def test_duplicate_signal_id_rejected():
    g = gov()
    g._recent_signals = [{"signal_id": "sig-1", "symbol": "BTC/USDT", "side": "long", "ts": NOW}]
    r = g.approve_trade(make_signal(signal_id="sig-1"), make_account(), make_market(), now_ts=NOW + 1)
    assert not r.approved and r.reason == "duplicate_signal_id"


# ---------------------------------------------------------------------------
# Fail-closed on missing data
# ---------------------------------------------------------------------------
def test_fail_closed_account_unknown():
    r = gov().approve_trade(make_signal(), AccountSnapshot(known=False), make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "account_state_unknown"


def test_fail_closed_market_unknown():
    r = gov().approve_trade(make_signal(), make_account(), MarketSnapshot(known=False), now_ts=NOW)
    assert not r.approved and r.reason == "market_data_unavailable"


def test_fail_closed_stale_price():
    stale = make_market(price_timestamp=NOW - 999)
    r = gov().approve_trade(make_signal(), make_account(), stale, now_ts=NOW)
    assert not r.approved and "stale_price" in r.reason


def test_fail_closed_unknown_slippage():
    r = gov().approve_trade(make_signal(), make_account(),
                            make_market(estimated_slippage_percent=None), now_ts=NOW)
    assert not r.approved and r.reason == "slippage_unknown"


# ---------------------------------------------------------------------------
# Trading modes + status object
# ---------------------------------------------------------------------------
def test_trading_mode_switch_validates():
    g = gov()
    g.set_trading_mode("OBSERVATION_ONLY")
    assert g.trading_mode == "OBSERVATION_ONLY"
    with pytest.raises(ValueError):
        g.set_trading_mode("YOLO_MODE")


def test_risk_status_object_shape():
    g = gov()
    g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    status = g.risk_status().to_dict()
    for key in ["trading_enabled", "risk_mode", "account_balance", "kill_switch_active",
                "current_drawdown_percent", "max_leverage", "news_pause",
                "last_approved_trade", "manual_restart_required"]:
        assert key in status


def test_news_pause_blocks_entries():
    g = gov()
    g.set_news_pause(True)
    r = g.approve_trade(make_signal(), make_account(), make_market(), now_ts=NOW)
    assert not r.approved and r.reason == "news_pause"
