"""Unit tests for the pure BingX API helpers (no ccxt / network needed).

Covers account summarisation (equity, open-position counting, dust filtering,
allowlist scoping) and base-amount math, plus order placement routed through a
fake ccxt client to verify the API call shapes without touching the network.
"""

import pytest

import bingx_api
from bingx_api import compute_base_amount, summarize_spot_account, place_order_api
from execution_logic import Decision

ALLOW = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


# --- compute_base_amount ---------------------------------------------------
def test_compute_base_amount():
    assert compute_base_amount(100, 50000) == pytest.approx(0.002)


def test_compute_base_amount_rejects_bad_price():
    with pytest.raises(ValueError):
        compute_base_amount(100, 0)


# --- summarize_spot_account ------------------------------------------------
def test_summarize_counts_only_held_allowlisted_pairs():
    totals = {"USDT": 500.0, "BTC": 0.01, "SOL": 0.0}
    prices = {"BTC/USDT": 60000.0}
    state = summarize_spot_account(totals, prices, ALLOW)
    assert state.known is True
    assert state.equity == pytest.approx(500.0 + 0.01 * 60000.0)  # 1100
    assert state.open_positions == 1
    assert state.open_pairs == ("BTC/USDT",)


def test_summarize_filters_dust(monkeypatch):
    monkeypatch.setattr(bingx_api, "DUST_QUOTE", 1.0)
    totals = {"USDT": 100.0, "ETH": 0.0001}   # 0.0001 * 3000 = 0.3 USDT < dust
    prices = {"ETH/USDT": 3000.0}
    state = summarize_spot_account(totals, prices, ALLOW)
    # value still counts toward equity, but not as an open position
    assert state.open_positions == 0
    assert state.equity == pytest.approx(100.0 + 0.3)


def test_summarize_ignores_unpriced_and_non_allowlisted():
    totals = {"USDT": 50.0, "DOGE": 1000.0, "BTC": 0.001}  # DOGE not allowlisted
    prices = {}  # no BTC price available -> not valued/counted
    state = summarize_spot_account(totals, prices, ALLOW)
    assert state.equity == pytest.approx(50.0)
    assert state.open_positions == 0


# --- place_order_api with a fake ccxt client -------------------------------
class FakeClient:
    def __init__(self):
        self.calls = []

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.calls.append(("market_buy_cost", symbol, cost))
        return {"id": "buy123"}

    def create_order(self, symbol, type_, side, amount, price=None):
        self.calls.append(("create_order", symbol, type_, side, amount, price))
        return {"id": "ord456"}

    def fetch_balance(self):
        return {"free": {"BTC": 0.5}}


def test_api_market_entry_uses_cost():
    client = FakeClient()
    d = Decision(action="enter", pair="BTC/USDT", side="long", order_type="market", stake=100)
    ok, detail = place_order_api(d, stake=100, client=client)
    assert ok and "buy123" in detail
    assert client.calls[0] == ("market_buy_cost", "BTC/USDT", 100)


def test_api_limit_entry_computes_amount():
    client = FakeClient()
    d = Decision(action="enter", pair="BTC/USDT", side="long", order_type="limit",
                 stake=100, price=50000)
    ok, detail = place_order_api(d, stake=100, client=client)
    assert ok
    kind, sym, type_, side, amount, price = client.calls[0]
    assert (kind, sym, type_, side) == ("create_order", "BTC/USDT", "limit", "buy")
    assert amount == pytest.approx(0.002)
    assert price == 50000


def test_api_market_exit_sells_amount():
    client = FakeClient()
    d = Decision(action="exit", pair="BTC/USDT", side="long", order_type="market",
                 stake=0, amount=0.01)
    ok, _ = place_order_api(d, stake=0, client=client)
    assert ok
    assert client.calls[0] == ("create_order", "BTC/USDT", "market", "sell", 0.01, None)


def test_api_exit_without_amount_sells_full_free_balance():
    client = FakeClient()
    d = Decision(action="exit", pair="BTC/USDT", side="long", order_type="market", stake=0)
    ok, _ = place_order_api(d, stake=0, client=client)
    assert ok
    # falls back to free BTC balance (0.5 from FakeClient)
    assert client.calls[0] == ("create_order", "BTC/USDT", "market", "sell", 0.5, None)


def test_api_order_failure_is_caught():
    class Boom:
        def create_market_buy_order_with_cost(self, *a, **k):
            raise RuntimeError("exchange down")

    d = Decision(action="enter", pair="BTC/USDT", side="long", order_type="market", stake=10)
    ok, detail = place_order_api(d, stake=10, client=Boom())
    assert ok is False
    assert "api_error" in detail


def test_select_api_keys_prefers_freqtrade_vars(monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "k1")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "s1")
    assert bingx_api.select_api_keys() == ("k1", "s1")
