"""Unit tests for the LIVE browser-execution guardrails.

These prove the fail-closed behaviour that is the primary safety layer for the
real-money path: bad decisions rejected, kill switch honoured, account-unknown
denies entries, caps enforced, exits never blocked by caps.
"""

import pytest

from execution_logic import (
    AccountState,
    Decision,
    DecisionError,
    LiveRiskLimits,
    check_order,
    interpret_kill_switch,
    live_trading_enabled,
    parse_decision,
)

LIMITS = LiveRiskLimits(
    total_capital=1000.0,
    per_trade_stake_max=100.0,
    max_open_positions=3,
    daily_max_loss_pct=0.05,           # 50 USDT
    pair_allowlist=frozenset({"BTC/USDT", "ETH/USDT", "SOL/USDT"}),
)

KNOWN_FLAT = AccountState(known=True, equity=1000, open_positions=0, day_pnl=0.0, open_pairs=())


# --- decision parsing ------------------------------------------------------
def test_parse_valid_market_entry():
    d = parse_decision({"action": "enter", "pair": "btc/usdt", "stake": 50})
    assert d.action == "enter"
    assert d.pair == "BTC/USDT"
    assert d.order_type == "market"
    assert d.stake == 50.0


def test_parse_limit_requires_price():
    with pytest.raises(DecisionError):
        parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 10, "order_type": "limit"})


def test_parse_rejects_unknown_action():
    with pytest.raises(DecisionError):
        parse_decision({"action": "yolo", "pair": "BTC/USDT", "stake": 10})


def test_parse_rejects_shorting():
    with pytest.raises(DecisionError):
        parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 10, "side": "short"})


def test_parse_rejects_bad_pair():
    with pytest.raises(DecisionError):
        parse_decision({"action": "enter", "pair": "BTCUSDT", "stake": 10})


def test_parse_rejects_nonpositive_entry_stake():
    with pytest.raises(DecisionError):
        parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 0})


# --- kill switch -----------------------------------------------------------
def test_kill_switch_fail_closed_on_unknown():
    assert interpret_kill_switch(None) is True
    assert interpret_kill_switch("garbage") is True


def test_kill_switch_off_and_on():
    assert interpret_kill_switch("off") is False
    assert interpret_kill_switch("on") is True
    assert interpret_kill_switch("TRUE") is True


def test_live_trading_disabled_by_default():
    assert live_trading_enabled(None) is False
    assert live_trading_enabled("false") is False
    assert live_trading_enabled("on") is True
    assert live_trading_enabled("enabled") is True


# --- gate: kill switch + master gate ---------------------------------------
def test_gate_blocks_when_kill_switch_tripped():
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    r = check_order(d, KNOWN_FLAT, LIMITS, kill_switch_tripped=True)
    assert not r.allow and r.reason == "kill_switch_active_or_unknown"


def test_gate_blocks_when_kill_switch_unknown():
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    r = check_order(d, KNOWN_FLAT, LIMITS, kill_switch_tripped=None)
    assert not r.allow


# --- gate: allowlist -------------------------------------------------------
def test_gate_blocks_non_allowlisted_pair():
    d = parse_decision({"action": "enter", "pair": "DOGE/USDT", "stake": 50})
    r = check_order(d, KNOWN_FLAT, LIMITS, kill_switch_tripped=False)
    assert not r.allow and "allowlist" in r.reason


# --- gate: exits always allowed (within allowlist) -------------------------
def test_gate_allows_exit_even_when_account_unknown():
    d = Decision(action="exit", pair="BTC/USDT", side="long", order_type="market", stake=0.0)
    r = check_order(d, AccountState(known=False), LIMITS, kill_switch_tripped=False)
    assert r.allow and r.reason == "exit_allowed"


def test_gate_blocks_exit_when_kill_switch_tripped():
    # Kill switch is absolute; the watchdog uses the queue+agent to flatten,
    # which it only does after deciding to halt — so a tripped switch blocks the
    # ad-hoc path here. (Flatten exits are enqueued by the watchdog itself.)
    d = Decision(action="exit", pair="BTC/USDT", side="long", order_type="market", stake=0.0)
    r = check_order(d, KNOWN_FLAT, LIMITS, kill_switch_tripped=True)
    assert not r.allow


# --- gate: entries require known account -----------------------------------
def test_gate_blocks_entry_when_account_unknown():
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    r = check_order(d, AccountState(known=False), LIMITS, kill_switch_tripped=False)
    assert not r.allow and r.reason == "account_state_unknown"


# --- gate: concurrency cap -------------------------------------------------
def test_gate_blocks_when_max_positions_reached():
    acct = AccountState(known=True, equity=1000, open_positions=3, day_pnl=0,
                        open_pairs=("ETH/USDT", "SOL/USDT", "BTC/USDT"))
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    # adding to an already-open pair is allowed
    r = check_order(d, acct, LIMITS, kill_switch_tripped=False)
    assert r.allow
    # a NEW pair beyond the cap is blocked
    acct2 = AccountState(known=True, equity=1000, open_positions=3, day_pnl=0,
                         open_pairs=("ETH/USDT", "SOL/USDT", "LTC/USDT"))
    d2 = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    r2 = check_order(d2, acct2, LIMITS, kill_switch_tripped=False)
    assert not r2.allow and r2.reason == "max_open_positions_reached"


# --- gate: daily loss cap --------------------------------------------------
def test_gate_blocks_when_daily_loss_cap_reached():
    acct = AccountState(known=True, equity=940, open_positions=0, day_pnl=-60, open_pairs=())
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    r = check_order(d, acct, LIMITS, kill_switch_tripped=False)
    assert not r.allow and r.reason == "daily_loss_cap_reached"


def test_gate_allows_within_daily_loss():
    acct = AccountState(known=True, equity=960, open_positions=0, day_pnl=-40, open_pairs=())
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 50})
    r = check_order(d, acct, LIMITS, kill_switch_tripped=False)
    assert r.allow


# --- gate: per-trade stake clamp (never up) --------------------------------
def test_gate_clamps_stake_down_to_cap():
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 500})
    r = check_order(d, KNOWN_FLAT, LIMITS, kill_switch_tripped=False)
    assert r.allow
    assert r.stake == 100.0  # clamped to per_trade_stake_max, never increased


def test_gate_never_increases_stake():
    d = parse_decision({"action": "enter", "pair": "BTC/USDT", "stake": 25})
    r = check_order(d, KNOWN_FLAT, LIMITS, kill_switch_tripped=False)
    assert r.allow
    assert r.stake == 25.0  # left as-is, not bumped up to the cap
