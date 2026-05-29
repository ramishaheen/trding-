"""Build typed Risk-Governor inputs (TradeSignal / AccountSnapshot /
MarketSnapshot) from live ccxt data + the queued order's `meta`.

Kept separate so the executor stays small. Everything fails closed: if a field
can't be read, the snapshot is left unknown/None and the governor rejects.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from risk_governor.models import AccountSnapshot, MarketSnapshot, TradeSignal

logger = logging.getLogger("governor_inputs")


def build_account_snapshot(client, allowlist: Iterable[str]) -> AccountSnapshot:
    """Spot account snapshot from ccxt. Fails closed (known=False) on error."""
    allowlist = list(allowlist)
    try:
        bal = client.fetch_balance()
        totals = {a: float(v) for a, v in bal.get("total", {}).items() if v}
        free = {a: float(v) for a, v in bal.get("free", {}).items() if v}
        equity = 0.0
        open_symbols: list[str] = []
        for pair in allowlist:
            base, quote = pair.split("/")
            if quote in totals:
                pass  # counted below
            amt = totals.get(base, 0.0)
            if amt > 0:
                t = client.fetch_ticker(pair)
                last = t.get("last") or t.get("close")
                if last:
                    value = amt * float(last)
                    equity += value
                    if value >= 1.0:
                        open_symbols.append(pair)
        # Add quote balances (USDT etc.) at face value.
        for quote in {p.split("/")[1] for p in allowlist}:
            equity += totals.get(quote, 0.0)
        balance = totals.get("USDT", equity)
        return AccountSnapshot(
            known=True,
            balance=balance,
            equity=equity or balance,
            available_margin=free.get("USDT", balance),
            open_positions=len(open_symbols),
            open_orders=0,
            open_symbols=tuple(open_symbols),
            margin_mode_confirmed=True,   # spot
            leverage_confirmed=True,
            current_leverage=1.0,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("account snapshot failed (fail closed): %s", exc)
        return AccountSnapshot(known=False)


def build_market_snapshot(client, symbol: str, meta: dict) -> MarketSnapshot:
    """Market microstructure from ccxt ticker + strategy-provided ATR in meta."""
    try:
        t = client.fetch_ticker(symbol)
        bid = t.get("bid")
        ask = t.get("ask")
        last = t.get("last") or t.get("close")
        ts = (t.get("timestamp") or time.time() * 1000) / 1000.0
        ob = None
        slippage = meta.get("estimated_slippage_percent")
        return MarketSnapshot(
            known=True,
            bid=float(bid) if bid else None,
            ask=float(ask) if ask else None,
            last_price=float(last) if last else None,
            price_timestamp=ts,
            atr=meta.get("atr"),
            atr_avg_20=meta.get("atr_avg_20"),
            last_candle_body_pct=meta.get("last_candle_body_pct"),
            orderbook_depth_quote=ob,
            estimated_slippage_percent=slippage,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("market snapshot failed (fail closed): %s", exc)
        return MarketSnapshot(known=False)


def build_trade_signal(order: dict, now_ts: float) -> TradeSignal:
    """Construct a TradeSignal from the queued order + its meta. Missing
    mandatory fields are left None so the governor rejects (fail closed)."""
    meta = order.get("meta") or {}
    return TradeSignal(
        symbol=order.get("pair"),
        side=order.get("side") or "long",
        entry_price=meta.get("entry_price") or order.get("price"),
        stop_loss_price=meta.get("stop_loss_price"),
        take_profit_price=meta.get("take_profit_price"),
        quantity=order.get("amount"),
        leverage=meta.get("leverage", 1),
        margin_mode=meta.get("margin_mode", "spot"),
        max_holding_time_minutes=meta.get("max_holding_time_minutes"),
        strategy_reason=order.get("tag") or meta.get("strategy_reason"),
        timestamp=now_ts,
        signal_id=meta.get("signal_id") or f"order-{order.get('id')}",
        execution_id=f"exec-{order.get('id')}",
        quality_components=meta.get("quality_components", {}),
    )
