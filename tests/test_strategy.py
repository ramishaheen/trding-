"""Unit tests for the strategy signal logic and the LLM soft gate.

These exercise `strategy_logic` directly (no freqtrade / pandas required), so the
exact rules the live bot runs are the rules under test.
"""

import pytest

from strategy_logic import (
    StrategyParams,
    ContextGate,
    apply_context_gate,
    atr,
    atr_stop_price,
    atr_stoploss_ratio,
    ema,
    entry_signal,
    exit_signal,
    is_pullback,
    is_uptrend,
    rsi,
)

P = StrategyParams()


# --- indicators ------------------------------------------------------------
def test_ema_tracks_rising_series():
    series = list(range(1, 51))
    e = ema(series, 10)
    assert len(e) == len(series)
    # EMA of a strictly rising series rises and lags below the latest value
    assert e[-1] > e[0]
    assert e[-1] < series[-1]


def test_rsi_high_for_uptrend_low_for_downtrend():
    up = list(range(1, 40))
    down = list(range(40, 1, -1))
    assert rsi(up, 14)[-1] > 70
    assert rsi(down, 14)[-1] < 30


def test_rsi_warmup_is_neutral():
    assert rsi([100, 101], 14)[-1] == 50.0


def test_atr_positive_and_aligned():
    highs = [10 + i for i in range(30)]
    lows = [9 + i for i in range(30)]
    closes = [9.5 + i for i in range(30)]
    a = atr(highs, lows, closes, 14)
    assert len(a) == 30
    assert a[-1] > 0


# --- trend filter / pullback ----------------------------------------------
def test_is_uptrend_requires_price_and_ema_above_trend():
    assert is_uptrend(close=110, ema_slow=105, ema_trend=100) is True
    assert is_uptrend(close=95, ema_slow=105, ema_trend=100) is False   # price below trend
    assert is_uptrend(close=110, ema_slow=98, ema_trend=100) is False   # slow ema below trend


def test_is_pullback_near_fast_ema():
    assert is_pullback(close=100.5, ema_fast=100, pullback_pct=0.02) is True
    assert is_pullback(close=99.5, ema_fast=100, pullback_pct=0.02) is True
    assert is_pullback(close=105, ema_fast=100, pullback_pct=0.02) is False  # extended
    assert is_pullback(close=100, ema_fast=0, pullback_pct=0.02) is False    # guard


# --- entry / exit signals --------------------------------------------------
def test_entry_signal_true_in_textbook_setup():
    # uptrend, price hugging fast EMA, RSI mid-range
    assert entry_signal(
        close=101, ema_fast=100, ema_slow=98, ema_trend=95, rsi_value=45, params=P
    ) is True


def test_entry_signal_blocked_when_not_uptrend():
    assert entry_signal(
        close=90, ema_fast=100, ema_slow=98, ema_trend=95, rsi_value=45, params=P
    ) is False


def test_entry_signal_blocked_when_rsi_overbought():
    assert entry_signal(
        close=101, ema_fast=100, ema_slow=98, ema_trend=95, rsi_value=70, params=P
    ) is False


def test_entry_signal_blocked_when_rsi_too_low():
    assert entry_signal(
        close=101, ema_fast=100, ema_slow=98, ema_trend=95, rsi_value=20, params=P
    ) is False


def test_exit_signal_on_momentum_exhaustion():
    assert exit_signal(close=120, ema_trend=100, rsi_value=80, params=P) is True


def test_exit_signal_on_trend_loss():
    assert exit_signal(close=95, ema_trend=100, rsi_value=50, params=P) is True


def test_exit_signal_false_in_healthy_trend():
    assert exit_signal(close=110, ema_trend=100, rsi_value=55, params=P) is False


# --- ATR stoploss ----------------------------------------------------------
def test_atr_stop_price_below_entry():
    assert atr_stop_price(100, 2.0, P) == pytest.approx(96.0)  # 100 - 2*2


def test_atr_stoploss_ratio_is_negative_and_clamped():
    ratio = atr_stoploss_ratio(100, 2.0, P)
    assert ratio == pytest.approx(-0.04)
    # huge ATR clamps to -0.99, never positive
    assert atr_stoploss_ratio(100, 1000, P) == pytest.approx(-0.99)
    assert atr_stoploss_ratio(0, 2.0, P) == pytest.approx(-0.99)


# --- LLM soft gate ---------------------------------------------------------
def test_gate_allows_when_no_context():
    g = apply_context_gate(None, None, P)
    assert g.allow_new_entries is True
    assert g.stake_multiplier == 1.0


def test_gate_blocks_on_risk_off():
    g = apply_context_gate("risk_off", 0.9, P)
    assert g.allow_new_entries is False
    assert g.stake_multiplier == 0.0


def test_gate_reduces_stake_on_neutral():
    g = apply_context_gate("neutral", 0.6, P)
    assert g.allow_new_entries is True
    assert g.stake_multiplier == 0.5


def test_gate_full_stake_on_risk_on():
    g = apply_context_gate("risk_on", 0.8, P)
    assert g.allow_new_entries is True
    assert g.stake_multiplier == 1.0


def test_gate_blocks_below_min_confidence_when_enabled():
    params = StrategyParams(min_context_confidence=0.5)
    g = apply_context_gate("risk_on", 0.3, params)
    assert g.allow_new_entries is False


def test_gate_cannot_force_a_trade():
    # The gate has no "force buy" output: the most permissive result is
    # allow_new_entries with multiplier <= 1.0. It never exceeds 1.0.
    for state in ("risk_on", "neutral", "risk_off", None):
        g = apply_context_gate(state, 0.99, P)
        assert g.stake_multiplier <= 1.0
        assert isinstance(g, ContextGate)
