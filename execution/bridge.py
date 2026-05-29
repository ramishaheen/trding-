"""Execution bridge.

A small FastAPI service that:
  1. Receives trade *decisions* from Freqtrade's webhook (entry/exit fills).
  2. Runs the independent pre-trade risk gate (execution_logic.check_order)
     against the REAL account state read from the DB.
  3. Enqueues approved orders into `execution_orders` for the browser subagent.
  4. Exposes a STOP endpoint that trips the global kill switch (which the
     browser agent and live watchdog obey).

Freqtrade remains in dry_run mode; its dry-run fills are the decisions we mirror
to the live account via the browser. Real money is at risk on the browser path,
so this gate is fail-closed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from execution_logic import (
    AccountState,
    LiveRiskLimits,
    check_order,
    interpret_kill_switch,
    live_trading_enabled,
    parse_decision,
)
from store import (
    enqueue_order,
    read_account_state,
    read_kill_switch,
    set_kill_switch,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [bridge] %(message)s"
)
logger = logging.getLogger("bridge")

app = FastAPI(title="BingX execution bridge")

WEBHOOK_TOKEN = os.environ.get("EXECUTION_WEBHOOK_TOKEN", "")


def load_limits() -> LiveRiskLimits:
    pairs = os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT")
    return LiveRiskLimits(
        total_capital=float(os.environ.get("TOTAL_CAPITAL_USDT", "1000")),
        per_trade_stake_max=float(os.environ.get("PER_TRADE_STAKE_USDT", "100")),
        max_open_positions=int(os.environ.get("MAX_OPEN_TRADES", "3")),
        daily_max_loss_pct=float(os.environ.get("DAILY_MAX_LOSS_PCT", "5")) / 100.0,
        pair_allowlist=frozenset(p.strip().upper() for p in pairs.split(",") if p.strip()),
    )


class WebhookPayload(BaseModel):
    action: str
    pair: str
    side: str = "long"
    order_type: str = "market"
    stake: float = 0.0
    amount: Optional[float] = None
    price: Optional[float] = None
    tag: str = ""
    # Rich signal fields for the Risk Governor (carried in the order's meta).
    entry_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    leverage: Optional[float] = 1
    margin_mode: Optional[str] = "spot"
    max_holding_time_minutes: Optional[float] = None
    signal_id: Optional[str] = None
    strategy_reason: Optional[str] = None
    atr: Optional[float] = None
    atr_avg_20: Optional[float] = None
    estimated_slippage_percent: Optional[float] = None
    quality_components: dict = {}

    def to_meta(self) -> dict:
        return {
            "entry_price": self.entry_price or self.price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "max_holding_time_minutes": self.max_holding_time_minutes,
            "signal_id": self.signal_id,
            "strategy_reason": self.strategy_reason or self.tag,
            "atr": self.atr,
            "atr_avg_20": self.atr_avg_20,
            "estimated_slippage_percent": self.estimated_slippage_percent,
            "quality_components": self.quality_components,
        }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "live_enabled": live_trading_enabled(os.environ.get("LIVE_BROWSER_TRADING_ENABLED"))}


@app.post("/webhook/decision")
def receive_decision(payload: WebhookPayload, x_webhook_token: str = Header(default="")) -> dict:
    # Shared-secret auth between Freqtrade and the bridge.
    if WEBHOOK_TOKEN and x_webhook_token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="bad webhook token")

    # Master enable: refuse everything unless the operator turned live on.
    if not live_trading_enabled(os.environ.get("LIVE_BROWSER_TRADING_ENABLED")):
        logger.warning("decision dropped: live browser trading is DISABLED")
        return {"accepted": False, "reason": "live_disabled"}

    try:
        decision = parse_decision(payload.model_dump())
    except Exception as exc:  # noqa: BLE001 - DecisionError and friends
        logger.warning("rejected malformed decision: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))

    limits = load_limits()
    account = read_account_state()          # AccountState (known=False if unavailable)
    kill = interpret_kill_switch(read_kill_switch())

    result = check_order(decision, account, limits, kill)
    if not result.allow:
        logger.warning("gate DENIED %s %s: %s", decision.action, decision.pair, result.reason)
        return {"accepted": False, "reason": result.reason}

    # The bridge gate is a cheap pre-filter; the executor's pipeline
    # (Weekly Target Manager -> Risk Governor) is the authoritative approval.
    order_id = enqueue_order(decision, stake=result.stake, meta=payload.to_meta())
    logger.info(
        "pre-filter PASSED %s %s stake=%.4f -> queued order %s (final approval at executor)",
        decision.action, decision.pair, result.stake, order_id,
    )
    return {"accepted": True, "order_id": order_id, "stake": result.stake}


@app.post("/stop")
def stop() -> dict:
    """Kill switch. Trips the global flag; the browser agent stops opening
    orders immediately and the live watchdog will flatten."""
    set_kill_switch(True, reason="manual /stop")
    logger.critical("KILL SWITCH TRIPPED via /stop")
    return {"kill_switch": "tripped"}


@app.post("/resume")
def resume() -> dict:
    """Clear the kill switch. Deliberate operator action only."""
    set_kill_switch(False, reason="manual /resume")
    logger.warning("kill switch cleared via /resume")
    return {"kill_switch": "cleared"}
