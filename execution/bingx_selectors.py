"""Centralized BingX web-UI selectors for the browser subagent.

The BingX front-end changes over time, so every selector the automation depends
on lives here in one place. When the UI changes, update this file only.

These are placeholders / best-guess selectors. BEFORE any live use you MUST
verify each one against the current BingX site (open DevTools, confirm the
element, and prefer stable attributes like data-testid over volatile class
names). The browser agent refuses to click anything it cannot positively
identify (see browser_agent.py).

Selectors support either CSS or XPath; entries prefixed with "xpath=" are
treated as XPath by Playwright.
"""

from __future__ import annotations

# Base URLs (spot trading). Override via env if BingX changes paths.
SPOT_TRADE_URL = "https://bingx.com/en/spot/{base}{quote}/"   # e.g. BTCUSDT
ACCOUNT_OVERVIEW_URL = "https://bingx.com/en/assets/overview/"

SELECTORS = {
    # Logged-in detection: an element only present when authenticated.
    "logged_in_marker": "[data-testid='user-avatar'], .header-avatar",

    # Order ticket — spot.
    "order_panel": "[data-testid='spot-order-panel'], .order-form",
    "buy_tab": "[data-testid='order-side-buy'], button:has-text('Buy')",
    "sell_tab": "[data-testid='order-side-sell'], button:has-text('Sell')",
    "market_tab": "[data-testid='order-type-market'], button:has-text('Market')",
    "limit_tab": "[data-testid='order-type-limit'], button:has-text('Limit')",

    # Inputs.
    "price_input": "[data-testid='order-price-input'] input, input[name='price']",
    "amount_input": "[data-testid='order-amount-input'] input, input[name='amount']",
    "total_input": "[data-testid='order-total-input'] input, input[name='total']",

    # Submit + confirmation.
    "submit_buy": "[data-testid='order-submit-buy'], button:has-text('Buy')",
    "submit_sell": "[data-testid='order-submit-sell'], button:has-text('Sell')",
    "confirm_dialog": "[data-testid='order-confirm-dialog'], .confirm-modal",
    "confirm_button": "[data-testid='order-confirm-ok'], .confirm-modal button:has-text('Confirm')",

    # Toast / result.
    "success_toast": ".ant-message-success, [data-testid='toast-success']",
    "error_toast": ".ant-message-error, [data-testid='toast-error']",

    # Open positions / balances (for the live watchdog scrape).
    "open_orders_rows": "[data-testid='open-orders'] tbody tr, .open-orders-row",
    "position_rows": "[data-testid='positions'] tbody tr, .position-row",
    "total_equity": "[data-testid='total-equity'], .total-asset-value",
}
