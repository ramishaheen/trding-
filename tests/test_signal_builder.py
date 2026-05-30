"""Tests for the strategy's live-signal builders (what the Risk Governor sees)."""

from strategy_logic import (
    StrategyParams,
    bias_quality_overrides,
    build_entry_signal,
    build_exit_signal,
    correlation_cap_ok,
    volatility_size_multiplier,
)


# --- tighter risk controls -------------------------------------------------
def test_volatility_throttle_full_size_when_calm():
    # atr% well under half the cap -> full size
    assert volatility_size_multiplier(0.01, 0.04) == 1.0


def test_volatility_throttle_floors_when_hot():
    # at/above the cap -> floor (0.4 default)
    assert volatility_size_multiplier(0.04, 0.04) == 0.4
    assert volatility_size_multiplier(0.06, 0.04) == 0.4


def test_volatility_throttle_scales_between():
    m = volatility_size_multiplier(0.03, 0.04)   # between half(0.02) and cap(0.04)
    assert 0.4 < m < 1.0


def test_correlation_cap():
    assert correlation_cap_ok(0, 2) is True
    assert correlation_cap_ok(1, 2) is True
    assert correlation_cap_ok(2, 2) is False     # already at the cap -> block
    assert correlation_cap_ok(3, 2) is False

P = StrategyParams(atr_period=14, atr_stop_mult=2.0)


def test_entry_signal_has_all_mandatory_fields():
    s = build_entry_signal("BTC/USDT", 100.0, atr=1.0, atr_avg_20=1.0, params=P,
                           now_ts=1_700_000_000, take_profit_rr=2.0, max_holding_minutes=60)
    for f in ["symbol" if False else "pair", "side", "entry_price", "stop_loss_price",
              "take_profit_price", "leverage", "margin_mode", "max_holding_time_minutes",
              "signal_id", "strategy_reason", "atr", "quality_components"]:
        assert f in s and s[f] not in (None, "")


def test_entry_stop_and_target_match_atr_and_rr():
    # entry 100, atr 1, mult 2 -> stop 98; rr 2 -> target 100 + 2*2 = 104
    s = build_entry_signal("BTC/USDT", 100.0, atr=1.0, atr_avg_20=1.0, params=P,
                           now_ts=1, take_profit_rr=2.0)
    assert s["stop_loss_price"] == 98.0
    assert s["take_profit_price"] == 104.0
    assert s["side"] == "long" and s["leverage"] == 1 and s["margin_mode"] == "spot"


def test_entry_risk_reward_is_at_least_min():
    # stop distance 2, target distance 4 -> RR 2.0, which clears the 1.5 floor
    s = build_entry_signal("ETH/USDT", 100.0, atr=1.0, atr_avg_20=1.0, params=P, now_ts=1)
    risk = s["entry_price"] - s["stop_loss_price"]
    reward = s["take_profit_price"] - s["entry_price"]
    assert reward / risk >= 1.5


def test_exit_signal_shape():
    s = build_exit_signal("BTC/USDT", amount=0.01, now_ts=1, reason="exit_trend")
    assert s["action"] == "exit" and s["pair"] == "BTC/USDT" and s["amount"] == 0.01


# --- LLM per-pair bias only ever tightens (soft gate) ----------------------
def test_bias_overrides_only_for_bearish():
    assert bias_quality_overrides("bullish") == {}
    assert bias_quality_overrides("neutral") == {}
    assert bias_quality_overrides(None) == {}
    bearish = bias_quality_overrides("bearish")
    assert bearish and all(v <= 50 for v in bearish.values())


def test_bearish_bias_lowers_signal_quality():
    base = build_entry_signal("BTC/USDT", 100.0, 1.0, 1.0, P, now_ts=1)
    bear = build_entry_signal("BTC/USDT", 100.0, 1.0, 1.0, P, now_ts=1,
                              extra_quality=bias_quality_overrides("bearish"))
    # The bearish read pushes components down vs the neutral default.
    assert bear["quality_components"]["regime_quality"] < base["quality_components"]["regime_quality"]
    assert bear["quality_components"]["trend_alignment"] < base["quality_components"]["trend_alignment"]


def test_bearish_bias_can_make_governor_reject_on_quality():
    """End-to-end: a bearish LLM read can push the governor's quality score
    below the minimum -> trade rejected. (Tightening only — never forces.)"""
    import time
    from risk_governor import RiskGovernor, load_config
    from risk_governor.models import AccountSnapshot, MarketSnapshot, TradeSignal
    now = time.time()
    raw = build_entry_signal("BTC/USDT", 100.0, 1.0, 1.0, P, now_ts=now,
                             extra_quality=bias_quality_overrides("bearish"))
    sig = TradeSignal(symbol="BTC/USDT", side="long", entry_price=100, stop_loss_price=99,
                      take_profit_price=102, quantity=0.1, leverage=1, margin_mode="spot",
                      max_holding_time_minutes=60, strategy_reason="t", timestamp=now,
                      signal_id="b1", quality_components=raw["quality_components"])
    acct = AccountSnapshot(known=True, balance=100, equity=100, available_margin=100,
                           open_positions=0, open_orders=0, open_symbols=(),
                           margin_mode_confirmed=True, leverage_confirmed=True)
    mkt = MarketSnapshot(known=True, bid=100.0, ask=100.02, last_price=100.01,
                         price_timestamp=now, atr=1.0, atr_avg_20=1.0,
                         orderbook_depth_quote=1000.0, estimated_slippage_percent=0.01)
    res = RiskGovernor(config=load_config()).approve_trade(sig, acct, mkt, now_ts=now)
    assert not res.approved and "score" in res.reason
