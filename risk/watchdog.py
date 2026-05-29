"""Independent risk watchdog.

Polls the Freqtrade REST API for realized + unrealized P&L and equity, evaluates
the hard risk limits (daily max loss, max drawdown) using `risk_logic`, and on a
breach it:
  1. Calls Freqtrade `/stop` (kill switch — stops opening new trades).
  2. Optionally calls `/forceexit all` to flatten open positions.
  3. Sends a Telegram alert.
  4. Logs the breach.

This process is deliberately independent of the strategy so that a strategy bug
cannot bypass the limits. It also emits a heartbeat log line each cycle.

Limits come from the environment (see .env.example):
  TOTAL_CAPITAL_USDT, DAILY_MAX_LOSS_PCT, MAX_DRAWDOWN_PCT.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import date

import requests

from risk_logic import RiskLimits, RiskState, evaluate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [watchdog] %(message)s",
)
logger = logging.getLogger("watchdog")

_running = True


def _handle_signal(signum, frame):  # noqa: ANN001
    global _running
    logger.info("received signal %s, stopping after current cycle", signum)
    _running = False


class FreqtradeClient:
    """Thin Freqtrade REST client with HTTP basic auth."""

    def __init__(self) -> None:
        self.base = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080").rstrip("/")
        self.auth = (
            os.environ.get("FREQTRADE_USERNAME", "freqtrader"),
            os.environ.get("FREQTRADE_PASSWORD", ""),
        )

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self.base}/api/v1/{path}", auth=self.auth, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: dict | None = None) -> dict:
        r = requests.post(f"{self.base}/api/v1/{path}", auth=self.auth, json=json or {}, timeout=10)
        r.raise_for_status()
        return r.json()

    def profit(self) -> dict:
        return self._get("profit")

    def balance(self) -> dict:
        return self._get("balance")

    def stop(self) -> dict:
        logger.warning("KILL SWITCH: calling Freqtrade /stop")
        return self._post("stop")

    def force_exit_all(self) -> dict:
        logger.warning("flattening: calling Freqtrade /forceexit all")
        return self._post("forceexit", {"tradeid": "all"})


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


def load_limits() -> RiskLimits:
    return RiskLimits(
        total_capital=float(os.environ.get("TOTAL_CAPITAL_USDT", "1000")),
        daily_max_loss_pct=float(os.environ.get("DAILY_MAX_LOSS_PCT", "5")) / 100.0,
        max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "10")) / 100.0,
    )


class EquityTracker:
    """Tracks peak equity and resets the daily baseline at UTC midnight."""

    def __init__(self, total_capital: float) -> None:
        self.peak_equity = total_capital
        self.day = date.today()
        self.day_start_equity = total_capital

    def update(self, current_equity: float) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.day_start_equity = current_equity
            logger.info("new UTC day; daily baseline reset to %.2f", current_equity)
        self.peak_equity = max(self.peak_equity, current_equity)

    def day_pnl(self, current_equity: float) -> float:
        return current_equity - self.day_start_equity


def read_equity(client: FreqtradeClient, fallback_capital: float) -> tuple[float, float]:
    """Return (current_equity, day_pnl_from_freqtrade).

    Uses /profit for closed-trade P&L and /balance for total value (incl. open).
    Falls back conservatively if a field is missing.
    """
    profit = client.profit()
    balance = client.balance()
    # Freqtrade /balance returns 'value' (total in stake currency) on recent versions.
    current_equity = float(
        balance.get("value")
        or balance.get("total")
        or fallback_capital
    )
    # profit_closed_coin + profit_all (incl. unrealized) — use the all-time as a
    # cross-check; daily P&L is tracked locally via EquityTracker.
    return current_equity, float(profit.get("profit_all_coin", 0.0))


def run_cycle(client: FreqtradeClient, tracker: EquityTracker, limits: RiskLimits,
              flatten: bool) -> bool:
    """One evaluation. Returns True if a halt was triggered."""
    current_equity, _ = read_equity(client, limits.total_capital)
    tracker.update(current_equity)
    day_pnl = tracker.day_pnl(current_equity)

    state = RiskState(
        day_pnl=day_pnl,
        peak_equity=tracker.peak_equity,
        current_equity=current_equity,
    )
    decision = evaluate(state, limits)

    logger.info(
        "heartbeat equity=%.2f peak=%.2f day_pnl=%.2f halt=%s",
        current_equity, tracker.peak_equity, day_pnl, decision.halt,
    )

    if decision.halt:
        reason_text = "; ".join(decision.reasons)
        logger.critical("RISK BREACH -> halting bot. %s", reason_text)
        try:
            client.stop()
            if flatten:
                client.force_exit_all()
        finally:
            send_telegram(f"🚨 RISK HALT: {reason_text}. Bot stopped.")
        return True
    return False


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = int(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "30"))
    flatten = os.environ.get("WATCHDOG_FLATTEN_ON_BREACH", "true").lower() == "true"
    limits = load_limits()
    client = FreqtradeClient()
    tracker = EquityTracker(limits.total_capital)

    logger.info(
        "watchdog starting: capital=%.0f daily_max_loss=%.1f%% max_drawdown=%.1f%% interval=%ss flatten=%s",
        limits.total_capital, limits.daily_max_loss_pct * 100,
        limits.max_drawdown_pct * 100, interval, flatten,
    )

    halted = False
    while _running:
        start = time.time()
        try:
            if not halted:
                halted = run_cycle(client, tracker, limits, flatten)
                if halted:
                    logger.critical(
                        "watchdog latched in HALT state; will keep alerting but "
                        "will not auto-resume. Operator must restart the bot."
                    )
            else:
                # stay latched; periodic reminder
                logger.warning("bot is HALTED by watchdog (latched).")
        except requests.RequestException as exc:
            logger.warning("freqtrade API unreachable: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("watchdog cycle error: %s", exc)

        elapsed = time.time() - start
        remaining = max(0.0, interval - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0

    logger.info("watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
