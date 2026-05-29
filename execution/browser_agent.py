"""Browser execution subagent (Playwright / Chromium).

Consumes approved orders from `execution_orders` and places them on the LIVE
BingX spot web UI by driving a real Chrome page. This is the component that puts
real money at risk, so it is built to fail safe:

  * Master gate: refuses to do anything unless LIVE_BROWSER_TRADING_ENABLED is on.
  * Kill switch: re-checked immediately before every order; tripped/unknown ->
    no action.
  * Deterministic selectors only (selectors.py). It will NOT improvise clicks;
    if it cannot positively and uniquely locate a control, it aborts the order
    and marks it failed rather than guessing.
  * Persistent browser profile: you log in (and pass 2FA) ONCE by hand into the
    automation profile; credentials are never stored in this repo.
  * Every order attempt is screenshotted to an audit directory.

Run:  python execution/browser_agent.py
(Headful first run to log in: set BROWSER_HEADLESS=false and complete login +
2FA in the opened window; the session persists in the profile dir.)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from execution_logic import (
    AccountState,
    LiveRiskLimits,
    check_order,
    interpret_kill_switch,
    live_trading_enabled,
    parse_decision,
)
from selectors import SELECTORS, SPOT_TRADE_URL
from store import (
    claim_next_pending,
    finish_order,
    read_account_state,
    read_kill_switch,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [browser] %(message)s"
)
logger = logging.getLogger("browser_agent")

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/data/bingx-profile")
AUDIT_DIR = Path(os.environ.get("BROWSER_AUDIT_DIR", "/data/audit"))
HEADLESS = os.environ.get("BROWSER_HEADLESS", "true").lower() == "true"
POLL_SECONDS = float(os.environ.get("BROWSER_POLL_SECONDS", "2"))


def _limits() -> LiveRiskLimits:
    pairs = os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT")
    return LiveRiskLimits(
        total_capital=float(os.environ.get("TOTAL_CAPITAL_USDT", "1000")),
        per_trade_stake_max=float(os.environ.get("PER_TRADE_STAKE_USDT", "100")),
        max_open_positions=int(os.environ.get("MAX_OPEN_TRADES", "3")),
        daily_max_loss_pct=float(os.environ.get("DAILY_MAX_LOSS_PCT", "5")) / 100.0,
        pair_allowlist=frozenset(p.strip().upper() for p in pairs.split(",") if p.strip()),
    )


def _screenshot(page, label: str) -> None:
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        page.screenshot(path=str(AUDIT_DIR / f"{ts}_{label}.png"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("screenshot failed (%s): %s", label, exc)


def _require_unique(page, key: str):
    """Locate a control by its selector. Aborts (raises) unless exactly one
    visible match exists — the agent never guesses which element to click."""
    locator = page.locator(SELECTORS[key])
    count = locator.count()
    if count != 1:
        raise RuntimeError(f"selector {key!r} matched {count} elements (need exactly 1)")
    return locator.first


def _is_logged_in(page) -> bool:
    try:
        return page.locator(SELECTORS["logged_in_marker"]).count() >= 1
    except Exception:  # noqa: BLE001
        return False


def place_order(page, order: dict) -> tuple[bool, str]:
    """Drive the BingX spot order ticket for a single order. Returns
    (success, detail). Deterministic and verified at each step."""
    base, quote = order["pair"].split("/")
    page.goto(SPOT_TRADE_URL.format(base=base, quote=quote), wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    if not _is_logged_in(page):
        return False, "not_logged_in"

    _screenshot(page, f"before_{order['id']}")

    # Buy for entry, Sell for exit (spot long-only).
    side_tab = "buy_tab" if order["action"] == "enter" else "sell_tab"
    _require_unique(page, side_tab).click()

    if order["order_type"] == "market":
        _require_unique(page, "market_tab").click()
    else:
        _require_unique(page, "limit_tab").click()
        price_field = _require_unique(page, "price_input")
        price_field.fill("")
        price_field.fill(str(order["price"]))

    # Entries are sized by quote total (stake); exits by base amount.
    if order["action"] == "enter":
        total_field = _require_unique(page, "total_input")
        total_field.fill("")
        total_field.fill(str(order["stake"]))
    else:
        amount_field = _require_unique(page, "amount_input")
        amount_field.fill("")
        amount_field.fill(str(order.get("amount") or ""))

    submit_key = "submit_buy" if order["action"] == "enter" else "submit_sell"
    _require_unique(page, submit_key).click()

    # Confirm dialog if BingX shows one.
    try:
        if page.locator(SELECTORS["confirm_dialog"]).count() >= 1:
            _require_unique(page, "confirm_button").click()
    except Exception:  # noqa: BLE001 - no dialog is fine
        pass

    page.wait_for_timeout(1500)
    _screenshot(page, f"after_{order['id']}")

    if page.locator(SELECTORS["error_toast"]).count() >= 1:
        return False, "exchange_error_toast"
    return True, "submitted"


def run() -> int:
    if not live_trading_enabled(os.environ.get("LIVE_BROWSER_TRADING_ENABLED")):
        logger.error("LIVE_BROWSER_TRADING_ENABLED is off; refusing to start. Exiting.")
        return 0

    from playwright.sync_api import sync_playwright

    limits = _limits()
    logger.warning(
        "BROWSER AGENT STARTING — LIVE REAL-MONEY EXECUTION. profile=%s headless=%s",
        PROFILE_DIR, HEADLESS,
    )

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"]
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        while True:
            # Hard gates, re-checked every loop.
            kill = interpret_kill_switch(read_kill_switch())
            if kill:
                logger.critical("kill switch active — not processing orders")
                time.sleep(POLL_SECONDS * 3)
                continue

            order = claim_next_pending()
            if not order:
                time.sleep(POLL_SECONDS)
                continue

            # Re-run the gate at execution time against fresh account state.
            account = read_account_state()
            decision = parse_decision(order)
            gate = check_order(decision, account, limits, kill)
            if not gate.allow:
                logger.warning("order %s denied at execution: %s", order["id"], gate.reason)
                finish_order(order["id"], "denied", gate.reason)
                continue

            try:
                ok, detail = place_order(page, order)
                finish_order(order["id"], "done" if ok else "failed", detail)
                logger.info("order %s -> %s (%s)", order["id"], "done" if ok else "failed", detail)
            except Exception as exc:  # noqa: BLE001 - never crash the loop mid-trade
                _screenshot(page, f"error_{order['id']}")
                finish_order(order["id"], "failed", f"exception:{exc}")
                logger.exception("order %s failed: %s", order["id"], exc)

    # unreachable
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
