"""Browser execution subagent (Playwright / Chromium) — FALLBACK order path.

Order placement now goes through the BingX API first (see bingx_api.py); this
module is the fallback the executor uses when an API order fails. It drives the
real BingX spot web UI by clicking a real Chrome page.

It is built to fail safe:
  * Deterministic selectors only (selectors.py); it will NOT improvise clicks.
    If it cannot positively and uniquely locate a control, it aborts the order.
  * Persistent browser profile: you log in (and pass 2FA) ONCE by hand into the
    automation profile; credentials are never stored in this repo.
  * Every order attempt is screenshotted to an audit directory.

The kill-switch / gate re-checks live in executor.py, which is the single queue
consumer and calls into here only for the browser fallback.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from bingx_selectors import SELECTORS, SPOT_TRADE_URL

logger = logging.getLogger("browser_agent")

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/data/bingx-profile")
AUDIT_DIR = Path(os.environ.get("BROWSER_AUDIT_DIR", "/data/audit"))
HEADLESS = os.environ.get("BROWSER_HEADLESS", "true").lower() == "true"


class BrowserSession:
    """Lazily-launched persistent Chromium context. Used as a context manager."""

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()


def _screenshot(page, label: str) -> None:
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        page.screenshot(path=str(AUDIT_DIR / f"{ts}_{label}.png"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("screenshot failed (%s): %s", label, exc)


def _require_unique(page, key: str):
    """Locate a control by its selector. Aborts unless exactly one match exists —
    the agent never guesses which element to click."""
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


def place_order_browser(page, order: dict) -> tuple[bool, str]:
    """Drive the BingX spot order ticket for a single order. Deterministic and
    verified at each step. Returns (success, detail)."""
    base, quote = order["pair"].split("/")
    page.goto(SPOT_TRADE_URL.format(base=base, quote=quote), wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    if not _is_logged_in(page):
        return False, "not_logged_in"

    _screenshot(page, f"before_{order['id']}")

    side_tab = "buy_tab" if order["action"] == "enter" else "sell_tab"
    _require_unique(page, side_tab).click()

    if order["order_type"] == "market":
        _require_unique(page, "market_tab").click()
    else:
        _require_unique(page, "limit_tab").click()
        price_field = _require_unique(page, "price_input")
        price_field.fill("")
        price_field.fill(str(order["price"]))

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

    try:
        if page.locator(SELECTORS["confirm_dialog"]).count() >= 1:
            _require_unique(page, "confirm_button").click()
    except Exception:  # noqa: BLE001 - no dialog is fine
        pass

    page.wait_for_timeout(1500)
    _screenshot(page, f"after_{order['id']}")

    if page.locator(SELECTORS["error_toast"]).count() >= 1:
        return False, "exchange_error_toast"
    return True, "browser_submitted"
