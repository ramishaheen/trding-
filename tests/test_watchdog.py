"""Unit tests proving each hard risk limit triggers a halt.

Exercises `risk_logic` directly (no network / Freqtrade required) plus the
watchdog's halt path with a fake Freqtrade client.
"""

import importlib
import sys

import pytest

from risk_logic import (
    RiskLimits,
    RiskState,
    daily_loss_breached,
    drawdown_breached,
    evaluate,
    current_drawdown_abs,
)

LIMITS = RiskLimits(total_capital=1000.0, daily_max_loss_pct=0.05, max_drawdown_pct=0.10)


# --- daily loss ------------------------------------------------------------
def test_daily_loss_not_breached_within_cap():
    assert daily_loss_breached(-49.0, LIMITS) is False  # cap is 50


def test_daily_loss_breached_at_cap():
    assert daily_loss_breached(-50.0, LIMITS) is True


def test_daily_loss_breached_beyond_cap():
    assert daily_loss_breached(-75.0, LIMITS) is True


def test_profit_never_breaches_daily_loss():
    assert daily_loss_breached(+200.0, LIMITS) is False


# --- drawdown --------------------------------------------------------------
def test_drawdown_abs_computation():
    state = RiskState(day_pnl=0, peak_equity=1100, current_equity=1000)
    assert current_drawdown_abs(state) == pytest.approx(100.0)


def test_drawdown_not_breached_within_cap():
    state = RiskState(day_pnl=0, peak_equity=1050, current_equity=1000)  # 50 < 100
    assert drawdown_breached(state, LIMITS) is False


def test_drawdown_breached_at_cap():
    state = RiskState(day_pnl=0, peak_equity=1100, current_equity=1000)  # 100 == cap
    assert drawdown_breached(state, LIMITS) is True


# --- combined evaluate -----------------------------------------------------
def test_evaluate_no_halt_when_healthy():
    state = RiskState(day_pnl=-10, peak_equity=1010, current_equity=1005)
    decision = evaluate(state, LIMITS)
    assert decision.halt is False
    assert decision.reasons == []
    assert bool(decision) is False


def test_evaluate_halts_on_daily_loss():
    state = RiskState(day_pnl=-60, peak_equity=1000, current_equity=940)
    decision = evaluate(state, LIMITS)
    assert decision.halt is True
    assert any("daily loss" in r for r in decision.reasons)


def test_evaluate_halts_on_drawdown():
    state = RiskState(day_pnl=-10, peak_equity=1200, current_equity=1080)  # 120 dd
    decision = evaluate(state, LIMITS)
    assert decision.halt is True
    assert any("drawdown" in r for r in decision.reasons)


def test_evaluate_reports_both_reasons():
    state = RiskState(day_pnl=-80, peak_equity=1200, current_equity=1000)
    decision = evaluate(state, LIMITS)
    assert decision.halt is True
    assert len(decision.reasons) == 2


# --- watchdog halt path with a fake client ---------------------------------
class FakeClient:
    def __init__(self):
        self.stopped = False
        self.flattened = False

    def stop(self):
        self.stopped = True
        return {"status": "stopped"}

    def force_exit_all(self):
        self.flattened = True
        return {"status": "flattened"}


@pytest.fixture
def watchdog(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    # import fresh so module-level logging config is applied once
    if "watchdog" in sys.modules:
        return importlib.reload(sys.modules["watchdog"])
    return importlib.import_module("watchdog")


def test_watchdog_triggers_stop_and_flatten_on_breach(watchdog):
    from risk_logic import RiskLimits as RL

    client = FakeClient()
    tracker = watchdog.EquityTracker(1000.0)
    # Force a breach: equity dropped 200 from a 1000 baseline => daily loss + dd
    tracker.peak_equity = 1000.0
    tracker.day_start_equity = 1000.0

    # Monkeypatch equity read to return a breaching equity
    watchdog.read_equity = lambda c, cap: (800.0, 0.0)  # type: ignore
    limits = RL(total_capital=1000.0, daily_max_loss_pct=0.05, max_drawdown_pct=0.10)

    halted = watchdog.run_cycle(client, tracker, limits, flatten=True)
    assert halted is True
    assert client.stopped is True
    assert client.flattened is True


def test_watchdog_no_halt_when_healthy(watchdog):
    from risk_logic import RiskLimits as RL

    client = FakeClient()
    tracker = watchdog.EquityTracker(1000.0)
    watchdog.read_equity = lambda c, cap: (995.0, 0.0)  # type: ignore
    limits = RL(total_capital=1000.0, daily_max_loss_pct=0.05, max_drawdown_pct=0.10)

    halted = watchdog.run_cycle(client, tracker, limits, flatten=True)
    assert halted is False
    assert client.stopped is False
    assert client.flattened is False
