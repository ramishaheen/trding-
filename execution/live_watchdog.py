"""Independent LIVE-account risk watchdog for the live execution path.

Reads the REAL BingX account via the API (reliable; not affected by UI changes),
writes the account snapshot the bridge/executor use for pre-trade checks, and on
a daily-loss / drawdown breach it:
  1. Trips the global kill switch (executor stops opening orders immediately).
  2. Flattens: enqueues market-exit orders for every open position.
  3. Alerts via Telegram.

Browser-placed orders are invisible to the Freqtrade watchdog, so this process
is the hard risk limit for real money. It reuses risk_logic.evaluate so the
breach rule is the shared, unit-tested one.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import requests

# risk_logic lives in ../risk; copied into the image's PYTHONPATH (see Dockerfile).
from risk_logic import RiskLimits, RiskState, evaluate

from execution_logic import Decision, live_trading_enabled
from bingx_api import fetch_account_state
from store import enqueue_order, set_kill_switch, write_account_snapshot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [live-watchdog] %(message)s"
)
logger = logging.getLogger("live_watchdog")

INTERVAL = int(os.environ.get("LIVE_WATCHDOG_INTERVAL_SECONDS", "30"))


def load_limits() -> RiskLimits:
    return RiskLimits(
        total_capital=float(os.environ.get("TOTAL_CAPITAL_USDT", "1000")),
        daily_max_loss_pct=float(os.environ.get("DAILY_MAX_LOSS_PCT", "5")) / 100.0,
        max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "10")) / 100.0,
    )


def pair_allowlist() -> list[str]:
    pairs = os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT")
    return [p.strip().upper() for p in pairs.split(",") if p.strip()]


def send_telegram(message: str) -> None:
    if os.environ.get("TELEGRAM_ENABLED", "false").lower() != "true":
        return
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram alert failed: %s", exc)


def flatten_all(open_pairs) -> None:
    """Enqueue a market exit for every open position so the executor closes
    them. Exits are always allowed through the gate."""
    for pair in open_pairs:
        try:
            enqueue_order(
                Decision(action="exit", pair=pair, side="long",
                         order_type="market", stake=0.0, tag="watchdog_flatten"),
                stake=0.0,
            )
            logger.warning("queued flatten exit for %s", pair)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to queue flatten for %s: %s", pair, exc)


def main() -> int:
    if not live_trading_enabled(os.environ.get("LIVE_BROWSER_TRADING_ENABLED")):
        logger.error("LIVE_BROWSER_TRADING_ENABLED is off; live watchdog idle. Exiting.")
        return 0

    limits = load_limits()
    allowlist = pair_allowlist()
    peak_equity = limits.total_capital
    day_start_equity = limits.total_capital
    current_day = date.today()

    logger.warning("LIVE watchdog starting (real account, API reads). interval=%ss", INTERVAL)

    while True:
        state = fetch_account_state(allowlist)
        write_account_snapshot(state)

        if state.known:
            today = date.today()
            if today != current_day:
                current_day = today
                day_start_equity = state.equity
            peak_equity = max(peak_equity, state.equity)
            day_pnl = state.equity - day_start_equity

            risk_state = RiskState(
                day_pnl=day_pnl, peak_equity=peak_equity, current_equity=state.equity
            )
            decision = evaluate(risk_state, limits)
            logger.info(
                "heartbeat equity=%.2f peak=%.2f day_pnl=%.2f open=%d halt=%s",
                state.equity, peak_equity, day_pnl, state.open_positions, decision.halt,
            )
            if decision.halt:
                reasons = "; ".join(decision.reasons)
                logger.critical("LIVE RISK BREACH -> kill + flatten. %s", reasons)
                set_kill_switch(True, reason=f"live breach: {reasons}")
                flatten_all(state.open_pairs)
                send_telegram(f"🚨 LIVE RISK HALT: {reasons}. Kill switch tripped, flattening.")
        else:
            logger.warning("account state unknown; gate will fail closed this cycle")

        time.sleep(INTERVAL)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
