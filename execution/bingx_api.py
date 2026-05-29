"""BingX API client (via ccxt) for the live execution path.

Used for:
  * reliable account reads (balance / positions) by the live watchdog and the
    executor's pre-trade gate, and
  * order placement (the PRIMARY order path; the browser subagent is the
    fallback).

Keys are read from the SAME env vars Freqtrade uses (FREQTRADE__EXCHANGE__KEY /
FREQTRADE__EXCHANGE__SECRET), so there is one place to paste them. They live only
in the git-ignored `.env`. ccxt is imported lazily so this module (and its pure
helpers) can be unit-tested without the dependency installed.

Everything FAILS CLOSED: a read failure reports AccountState(known=False) so the
gate denies new entries; an order failure returns (False, reason).
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from execution_logic import AccountState, Decision

logger = logging.getLogger("bingx_api")

# Holdings worth less than this (in quote/USDT) are treated as dust, not an
# open position.
DUST_QUOTE = float(os.environ.get("POSITION_DUST_USDT", "1.0"))


def select_api_keys() -> tuple[str, str]:
    key = os.environ.get("FREQTRADE__EXCHANGE__KEY") or os.environ.get("BINGX_API_KEY", "")
    secret = os.environ.get("FREQTRADE__EXCHANGE__SECRET") or os.environ.get("BINGX_API_SECRET", "")
    return key, secret


def make_client():
    """Construct a ccxt BingX spot client. Raises if keys are missing."""
    import ccxt  # lazy

    key, secret = select_api_keys()
    if not key or not secret:
        raise RuntimeError("BingX API key/secret not set (FREQTRADE__EXCHANGE__KEY/SECRET)")
    return ccxt.bingx({
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def compute_base_amount(stake: float, price: float) -> float:
    """Base-asset amount for a given quote stake at a price."""
    if price <= 0:
        raise ValueError("price must be > 0")
    return stake / price


def summarize_spot_account(
    totals: dict[str, float],
    prices: dict[str, float],
    allowlist: Iterable[str],
) -> AccountState:
    """Turn raw balances + prices into an AccountState.

    * `totals`   : {asset: total_balance}
    * `prices`   : {"BASE/QUOTE": last_price}
    * `allowlist`: iterable of "BASE/QUOTE" pairs

    Equity counts quote-currency balances at face value plus the value of held
    allowlisted base assets. open_positions counts allowlisted bases held above
    the dust threshold.
    """
    quote_assets: set[str] = set()
    base_to_pair: dict[str, str] = {}
    for pair in allowlist:
        base, quote = pair.split("/")
        base_to_pair[base] = pair
        quote_assets.add(quote)

    equity = 0.0
    open_pairs: list[str] = []
    for asset, amt in totals.items():
        if amt <= 0:
            continue
        if asset in quote_assets:
            equity += amt
            continue
        pair = base_to_pair.get(asset)
        if not pair:
            continue  # asset we don't price; ignore for this conservative view
        price = prices.get(pair)
        if price is None:
            continue
        value = amt * price
        equity += value
        if value >= DUST_QUOTE:
            open_pairs.append(pair)

    return AccountState(
        known=True,
        equity=equity,
        open_positions=len(open_pairs),
        day_pnl=0.0,  # caller (watchdog) derives daily P&L from equity over time
        open_pairs=tuple(open_pairs),
    )


# ---------------------------------------------------------------------------
# Live I/O (ccxt)
# ---------------------------------------------------------------------------
def fetch_account_state(allowlist: Iterable[str], client=None) -> AccountState:
    """Read the real spot account. Fails closed (known=False) on any error."""
    allowlist = list(allowlist)
    try:
        client = client or make_client()
        bal = client.fetch_balance()
        totals = {a: float(v) for a, v in bal.get("total", {}).items() if v}
        prices: dict[str, float] = {}
        for pair in allowlist:
            base = pair.split("/")[0]
            if totals.get(base, 0) > 0:
                ticker = client.fetch_ticker(pair)
                last = ticker.get("last") or ticker.get("close")
                if last:
                    prices[pair] = float(last)
        return summarize_spot_account(totals, prices, allowlist)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("API account fetch failed (treated as unknown): %s", exc)
        return AccountState(known=False)


def place_order_api(decision: Decision, stake: float, client=None) -> tuple[bool, str]:
    """Place a single order via the BingX API. Returns (ok, detail)."""
    try:
        client = client or make_client()
        symbol = decision.pair

        if decision.action == "enter":
            if decision.order_type == "market":
                # Market buy sized by quote cost (stake).
                order = client.create_market_buy_order_with_cost(symbol, stake)
            else:
                amount = compute_base_amount(stake, decision.price)
                order = client.create_order(symbol, "limit", "buy", amount, decision.price)
        else:  # exit -> sell base
            amount = decision.amount
            if amount is None:
                base = symbol.split("/")[0]
                bal = client.fetch_balance()
                amount = float(bal.get("free", {}).get(base, 0) or 0)
            if not amount or amount <= 0:
                return False, "no_base_amount_to_sell"
            if decision.order_type == "market":
                order = client.create_order(symbol, "market", "sell", amount)
            else:
                order = client.create_order(symbol, "limit", "sell", amount, decision.price)

        order_id = (order or {}).get("id", "?")
        return True, f"api_order:{order_id}"
    except Exception as exc:  # noqa: BLE001
        return False, f"api_error:{exc}"
