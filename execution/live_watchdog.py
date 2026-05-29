"""Independent LIVE-account risk watchdog for the browser execution path.

Because browser-placed orders are invisible to the Freqtrade watchdog, this
process is the hard risk limit for real money. It periodically reads the real
BingX account (by scraping the logged-in page), writes an account snapshot the
bridge/agent use for pre-trade checks, and on a breach it:
  1. Trips the global kill switch (browser agent stops opening orders).
  2. Flattens: enqueues market-exit orders for every open position.
  3. Alerts via Telegram.

Limits come from the same env as the rest of the system. It uses
risk_logic.evaluate for the breach decision so the rule is shared + tested.
"""

from __future__ import annotations

import logging
import os
import time

import requests

# risk_logic lives in ../risk; PYTHONPATH includes it in the container image.
from risk_logic import RiskLimits, RiskState, evaluate

from execution_logic import AccountState, Decision, live_trading_enabled
from selectors import ACCOUNT_OVERVIEW_URL, SELECTORS
from store import enqueue_order, set_kill_switch, write_account_snapshot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [live-watchdog] %(message)s"
)
logger = logging.getLogger("live_watchdog")

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/data/bingx-profile")
HEADLESS = os.environ.get("BROWSER_HEADLESS", "true").lower() == "true"
INTERVAL = int(os.environ.get("LIVE_WATCHDOG_INTERVAL_SECONDS", "30"))


def load_limits() -> RiskLimits:
    return RiskLimits(
        total_capital=float(os.environ.get("TOTAL_CAPITAL_USDT", "1000")),
        daily_max_loss_pct=float(os.environ.get("DAILY_MAX_LOSS_PCT", "5")) / 100.0,
        max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "10")) / 100.0,
    )


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


def scrape_account(page) -> AccountState:
    """Read the real account from the logged-in BingX page.

    Returns AccountState(known=False) if anything cannot be read with
    confidence — the gate then fails closed. The selectors are best-guess and
    MUST be verified against the live site before use (see selectors.py)."""
    try:
        page.goto(ACCOUNT_OVERVIEW_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        equity_loc = page.locator(SELECTORS["total_equity"])
        if equity_loc.count() != 1:
            return AccountState(known=False)
        equity_text = equity_loc.first.inner_text()
        equity = float("".join(c for c in equity_text if c.isdigit() or c == "."))

        position_rows = page.locator(SELECTORS["position_rows"])
        open_count = position_rows.count()
        open_pairs: list[str] = []
        for i in range(open_count):
            try:
                txt = position_rows.nth(i).inner_text().upper().replace(" ", "")
                # best-effort pair extraction; left conservative
                for p in os.environ.get("LIVE_PAIR_ALLOWLIST", "").upper().split(","):
                    sym = p.replace("/", "").strip()
                    if sym and sym in txt:
                        open_pairs.append(p.strip())
            except Exception:  # noqa: BLE001
                pass

        return AccountState(
            known=True,
            equity=equity,
            open_positions=open_count,
            day_pnl=0.0,  # filled by EquityTracker below
            open_pairs=tuple(open_pairs),
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("account scrape failed (treated as unknown): %s", exc)
        return AccountState(known=False)


def flatten_all(open_pairs) -> None:
    """Enqueue a market exit for every open position so the browser agent
    closes them. Exits are always allowed through the gate."""
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

    from playwright.sync_api import sync_playwright

    limits = load_limits()
    peak_equity = limits.total_capital
    day_start_equity = limits.total_capital
    from datetime import date as _date
    current_day = _date.today()

    logger.warning("LIVE watchdog starting (real account). interval=%ss", INTERVAL)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=HEADLESS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        while True:
            state = scrape_account(page)
            write_account_snapshot(state)

            if state.known:
                # daily reset + peak tracking
                today = _date.today()
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
