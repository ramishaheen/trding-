"""Tests for the strategy's live-signal builders (what the Risk Governor sees)."""

from strategy_logic import StrategyParams, build_entry_signal, build_exit_signal

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
