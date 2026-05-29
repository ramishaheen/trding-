"""Executor — the single live-order queue consumer.

Claims gate-approved orders from `execution_orders` and places them, with:
  ORDER_PATH = api      -> API only
             = browser  -> browser subagent only
             = both     -> API first, browser fallback on API failure (default)

Before EVERY order it re-runs the independent gate against a FRESH account read
(via the BingX API) and re-checks the kill switch. Fails closed.
"""

from __future__ import annotations

import logging
import os
import time

from execution_logic import (
    LiveRiskLimits,
    check_order,
    interpret_kill_switch,
    live_trading_enabled,
    parse_decision,
)
from bingx_api import fetch_account_state, place_order_api
from store import claim_next_pending, finish_order, read_kill_switch, write_account_snapshot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [executor] %(message)s"
)
logger = logging.getLogger("executor")

ORDER_PATH = os.environ.get("ORDER_PATH", "both").strip().lower()
POLL_SECONDS = float(os.environ.get("EXECUTOR_POLL_SECONDS", "2"))


def _limits() -> LiveRiskLimits:
    pairs = os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT")
    return LiveRiskLimits(
        total_capital=float(os.environ.get("TOTAL_CAPITAL_USDT", "1000")),
        per_trade_stake_max=float(os.environ.get("PER_TRADE_STAKE_USDT", "100")),
        max_open_positions=int(os.environ.get("MAX_OPEN_TRADES", "3")),
        daily_max_loss_pct=float(os.environ.get("DAILY_MAX_LOSS_PCT", "5")) / 100.0,
        pair_allowlist=frozenset(p.strip().upper() for p in pairs.split(",") if p.strip()),
    )


def _place(order: dict, browser) -> tuple[bool, str]:
    """Place via API first (if enabled), then browser fallback (if enabled)."""
    decision = parse_decision(order)
    detail = ""

    if ORDER_PATH in ("api", "both"):
        ok, detail = place_order_api(decision, stake=order["stake"])
        if ok:
            return True, detail
        logger.warning("API order %s failed: %s", order["id"], detail)
        if ORDER_PATH == "api":
            return False, detail

    if ORDER_PATH in ("browser", "both"):
        if browser is None or browser.page is None:
            return False, f"{detail};browser_unavailable" if detail else "browser_unavailable"
        from browser_agent import place_order_browser

        ok, b_detail = place_order_browser(browser.page, order)
        return ok, (f"api_failed:{detail}|browser:{b_detail}" if detail else b_detail)

    return False, "no_order_path_enabled"


def _run_loop(limits: LiveRiskLimits, browser) -> None:
    pair_allowlist = limits.pair_allowlist
    while True:
        kill = interpret_kill_switch(read_kill_switch())
        if kill:
            logger.critical("kill switch active — not processing orders")
            time.sleep(POLL_SECONDS * 3)
            continue

        order = claim_next_pending()
        if not order:
            time.sleep(POLL_SECONDS)
            continue

        # Fresh account read for the authoritative pre-trade gate.
        account = fetch_account_state(pair_allowlist)
        write_account_snapshot(account)
        decision = parse_decision(order)
        gate = check_order(decision, account, limits, kill)
        if not gate.allow:
            logger.warning("order %s denied at execution: %s", order["id"], gate.reason)
            finish_order(order["id"], "denied", gate.reason)
            continue
        order["stake"] = gate.stake or order["stake"]

        try:
            ok, detail = _place(order, browser)
            finish_order(order["id"], "done" if ok else "failed", detail)
            logger.info("order %s -> %s (%s)", order["id"], "done" if ok else "failed", detail)
        except Exception as exc:  # noqa: BLE001 - never crash mid-trade
            finish_order(order["id"], "failed", f"exception:{exc}")
            logger.exception("order %s failed: %s", order["id"], exc)


def main() -> int:
    if not live_trading_enabled(os.environ.get("LIVE_BROWSER_TRADING_ENABLED")):
        logger.error("LIVE_BROWSER_TRADING_ENABLED is off; executor idle. Exiting.")
        return 0

    limits = _limits()
    logger.warning(
        "EXECUTOR STARTING — LIVE REAL-MONEY EXECUTION. order_path=%s allowlist=%s",
        ORDER_PATH, sorted(limits.pair_allowlist),
    )

    need_browser = ORDER_PATH in ("browser", "both")
    if need_browser:
        from browser_agent import BrowserSession

        with BrowserSession() as browser:
            _run_loop(limits, browser)
    else:
        _run_loop(limits, browser=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
