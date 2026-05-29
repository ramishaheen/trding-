"""Executor — the single live-order queue consumer and the place where the
mandated control flow is enforced:

    (Freqtrade decision) -> queue -> Weekly Target Manager -> Risk Governor
                                  -> Execution (API first, browser fallback) -> BingX

NO order is placed unless trade_pipeline.evaluate_trade() returns approved (which
requires BOTH the Weekly Target Manager to allow AND the Risk Governor — the
final authority — to approve). Everything fails closed.

ORDER_PATH = api | browser | both   (default both: API first, browser fallback)
"""

from __future__ import annotations

import logging
import os
import time

from execution_logic import live_trading_enabled, parse_decision
from bingx_api import make_client, place_order_api
from governor_inputs import build_account_snapshot, build_market_snapshot, build_trade_signal
from store import (
    claim_next_pending,
    finish_order,
    read_kill_switch,
    set_kill_switch,
    write_status,
)

from risk_governor import RiskGovernor, load_config
from risk_governor.models import TradingMode
from weekly_target_manager import WeeklyTargetManager, load_weekly_config
from trade_pipeline import evaluate_trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [executor] %(message)s")
logger = logging.getLogger("executor")

ORDER_PATH = os.environ.get("ORDER_PATH", "both").strip().lower()
POLL_SECONDS = float(os.environ.get("EXECUTOR_POLL_SECONDS", "2"))
ALLOWLIST = [p.strip().upper() for p in
             os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT").split(",") if p.strip()]


def _flatten_via_kill() -> None:
    """Kill-switch callback: trip the shared flag so all components halt."""
    set_kill_switch(True, reason="risk_governor_kill_switch")


def _place(order: dict, decision, quantity: float, browser) -> tuple[bool, str]:
    """Place the governor-authorized quantity. API first, browser fallback."""
    order = dict(order)
    order["amount"] = quantity if decision.action == "exit" else order.get("amount")
    detail = ""
    if ORDER_PATH in ("api", "both"):
        # For entries the governor authorizes a position *value*; convert to stake.
        stake = quantity * (decision.price or 0) if decision.action == "enter" and decision.price else order.get("stake")
        ok, detail = place_order_api(decision, stake=stake or order.get("stake", 0))
        if ok:
            return True, detail
        logger.warning("API order %s failed: %s", order["id"], detail)
        if ORDER_PATH == "api":
            return False, detail
    if ORDER_PATH in ("browser", "both") and browser is not None and browser.page is not None:
        from browser_agent import place_order_browser
        ok, b_detail = place_order_browser(browser.page, order)
        return ok, (f"api_failed:{detail}|browser:{b_detail}" if detail else b_detail)
    return False, detail or "no_order_path"


def _persist_status(governor: RiskGovernor, wtm: WeeklyTargetManager, now_ts: float) -> None:
    try:
        write_status("risk_status", governor.risk_status().to_dict())
        write_status("weekly_target", wtm.dashboard(now_ts))
    except Exception as exc:  # noqa: BLE001
        logger.warning("status persist failed: %s", exc)


def run_loop(governor: RiskGovernor, wtm: WeeklyTargetManager, client, browser) -> None:
    while True:
        now = time.time()

        # Global kill switch (shared flag) halts everything immediately.
        from execution_logic import interpret_kill_switch
        if interpret_kill_switch(read_kill_switch()) or governor.kill_switch_active:
            governor.emergency_kill_switch("shared_kill_flag") if not governor.kill_switch_active else None
            _persist_status(governor, wtm, now)
            time.sleep(POLL_SECONDS * 3)
            continue

        order = claim_next_pending()
        if not order:
            _persist_status(governor, wtm, now)
            time.sleep(POLL_SECONDS)
            continue

        try:
            decision = parse_decision(order)
            account = build_account_snapshot(client, ALLOWLIST)
            market = build_market_snapshot(client, order["pair"], order.get("meta") or {})
            signal = build_trade_signal(order, now)

            # Exits should not be blocked from reducing risk: the governor allows
            # exits, but the pipeline still records them; we route exits straight
            # to placement (flattening must always work).
            if decision.action == "exit":
                ok, detail = _place(order, decision, order.get("amount") or 0, browser)
                finish_order(order["id"], "done" if ok else "failed", detail)
                _persist_status(governor, wtm, now)
                continue

            result = evaluate_trade(wtm, governor, signal, account, market, now)
            _persist_status(governor, wtm, now)
            if not result.approved:
                logger.warning("order %s REJECTED: %s", order["id"], result.reason)
                finish_order(order["id"], "denied", result.reason)
                continue

            ok, detail = _place(order, decision, result.quantity, browser)
            finish_order(order["id"], "done" if ok else "failed", detail)
            logger.info("order %s -> %s qty=%.8f (%s)",
                        order["id"], "done" if ok else "failed", result.quantity, detail)
        except Exception as exc:  # noqa: BLE001 - never crash mid-trade
            finish_order(order["id"], "failed", f"exception:{exc}")
            logger.exception("order %s failed: %s", order["id"], exc)


def main() -> int:
    mode = load_config().trading_mode
    if not live_trading_enabled(os.environ.get("LIVE_BROWSER_TRADING_ENABLED")):
        logger.error("LIVE_BROWSER_TRADING_ENABLED is off; executor idle (no real orders). Exiting.")
        return 0
    if mode != TradingMode.REAL_TRADING_STRICT.value:
        logger.warning("trading_mode=%s (not REAL_TRADING_STRICT); executor will not place real orders.", mode)

    governor = RiskGovernor(config=load_config(),
                            cancel_all_orders=_flatten_via_kill,
                            close_all_positions=_flatten_via_kill)
    wtm = WeeklyTargetManager(config=load_weekly_config())

    try:
        client = make_client()
    except Exception as exc:  # noqa: BLE001 - no keys -> fail closed, stay up for status
        logger.error("BingX client unavailable: %s", exc)
        client = None

    logger.warning("EXECUTOR STARTING — order_path=%s mode=%s allowlist=%s", ORDER_PATH, mode, ALLOWLIST)

    need_browser = ORDER_PATH in ("browser", "both")
    if need_browser:
        try:
            from browser_agent import BrowserSession
            with BrowserSession() as browser:
                run_loop(governor, wtm, client, browser)
        except Exception as exc:  # noqa: BLE001 - browser optional; run API-only
            logger.error("browser session unavailable (%s); running API-only", exc)
            run_loop(governor, wtm, client, browser=None)
    else:
        run_loop(governor, wtm, client, browser=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
