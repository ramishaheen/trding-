"""Pure, dependency-free logic for the live browser-execution path.

⚠️  This module supports REAL-MONEY trading via browser automation. The browser
subagent places real orders on the live BingX web UI, which the Freqtrade risk
watchdog cannot see. Therefore the guardrails here are the PRIMARY safety layer
for the live path and are deliberately strict and fail-closed.

Kept free of FastAPI / Playwright / DB imports so the rules can be unit-tested
deterministically. The bridge, browser agent, and live watchdog all import and
reuse these functions — there is no second, untested copy of the rules.

Fail-closed principles
----------------------
* Unknown / malformed decisions are rejected, never executed.
* If account state is unknown, the gate denies (we cannot prove we are within
  limits, so we do not trade).
* The kill switch, when tripped or unknown, blocks all new orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Decisions coming from the trading brain (Freqtrade webhook payloads)
# ---------------------------------------------------------------------------
VALID_ACTIONS = {"enter", "exit"}
VALID_SIDES = {"long"}            # spot, long-only — no shorting
VALID_ORDER_TYPES = {"market", "limit"}


@dataclass(frozen=True)
class Decision:
    action: str          # "enter" | "exit"
    pair: str            # e.g. "BTC/USDT"
    side: str            # "long"
    order_type: str      # "market" | "limit"
    stake: float         # quote-currency stake for entries (USDT)
    amount: Optional[float] = None   # base amount, mainly for exits
    price: Optional[float] = None     # limit price (None for market)
    tag: str = ""


class DecisionError(ValueError):
    """Raised when a webhook payload cannot be turned into a safe Decision."""


def parse_decision(payload: dict) -> Decision:
    """Validate and normalize a raw webhook payload into a Decision.

    Raises DecisionError on anything we don't fully understand. We never guess.
    """
    if not isinstance(payload, dict):
        raise DecisionError("payload is not an object")

    action = str(payload.get("action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        raise DecisionError(f"invalid action: {action!r}")

    pair = str(payload.get("pair", "")).strip().upper()
    if "/" not in pair:
        raise DecisionError(f"invalid pair: {pair!r}")

    side = str(payload.get("side", "long")).strip().lower()
    if side not in VALID_SIDES:
        raise DecisionError(f"unsupported side: {side!r} (spot long-only)")

    order_type = str(payload.get("order_type", "market")).strip().lower()
    if order_type not in VALID_ORDER_TYPES:
        raise DecisionError(f"invalid order_type: {order_type!r}")

    def _num(key) -> Optional[float]:
        v = payload.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise DecisionError(f"non-numeric {key}: {v!r}")

    stake = _num("stake") or 0.0
    amount = _num("amount")
    price = _num("price")

    if action == "enter" and stake <= 0:
        raise DecisionError("enter decision requires a positive stake")
    if order_type == "limit" and (price is None or price <= 0):
        raise DecisionError("limit order requires a positive price")

    return Decision(
        action=action,
        pair=pair,
        side=side,
        order_type=order_type,
        stake=stake,
        amount=amount,
        price=price,
        tag=str(payload.get("tag", ""))[:64],
    )


# ---------------------------------------------------------------------------
# Live account state + independent pre-trade risk gate
# ---------------------------------------------------------------------------
@dataclass
class AccountState:
    """Snapshot of the REAL account, provided by the caller (browser scrape or
    read-only API). `known` must be True for the gate to allow trading."""
    known: bool
    equity: float = 0.0
    open_positions: int = 0
    day_pnl: float = 0.0           # realized + unrealized for the day (neg = loss)
    open_pairs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LiveRiskLimits:
    total_capital: float
    per_trade_stake_max: float
    max_open_positions: int
    daily_max_loss_pct: float       # fraction, e.g. 0.05
    pair_allowlist: frozenset[str]

    @property
    def daily_max_loss_abs(self) -> float:
        return self.total_capital * self.daily_max_loss_pct


@dataclass
class GateResult:
    allow: bool
    reason: str
    # if allowed, the (possibly clamped) stake to actually use
    stake: float = 0.0

    def __bool__(self) -> bool:
        return self.allow


def check_order(
    decision: Decision,
    account: AccountState,
    limits: LiveRiskLimits,
    kill_switch_tripped: Optional[bool],
) -> GateResult:
    """The independent pre-trade gate for the live browser path. Fail-closed.

    Order of checks (most protective first):
      1. Kill switch (unknown == tripped == block).
      2. Account state must be known.
      3. Exits are always allowed through (reducing risk), within allowlist.
      4. Entries: pair allowlist, max open positions, daily loss cap,
         per-trade stake cap (clamped down, never up).
    """
    # 1. Kill switch — unknown state is treated as tripped (fail closed).
    if kill_switch_tripped is None or kill_switch_tripped:
        return GateResult(False, "kill_switch_active_or_unknown")

    # Pair must be on the allowlist regardless of direction.
    if decision.pair not in limits.pair_allowlist:
        return GateResult(False, f"pair_not_allowlisted:{decision.pair}")

    # 3. Exits reduce exposure — allow them even if account read is degraded,
    #    as long as the kill switch is clear. Flattening must never be blocked
    #    by a risk cap.
    if decision.action == "exit":
        return GateResult(True, "exit_allowed", stake=0.0)

    # From here we are opening/adding exposure -> require known account state.
    if not account.known:
        return GateResult(False, "account_state_unknown")

    # 4a. Concurrency cap. Adding to an already-open pair does not increase count.
    if decision.pair not in account.open_pairs and \
            account.open_positions >= limits.max_open_positions:
        return GateResult(False, "max_open_positions_reached")

    # 4b. Daily loss cap.
    loss = -min(account.day_pnl, 0.0)
    if loss >= limits.daily_max_loss_abs:
        return GateResult(False, "daily_loss_cap_reached")

    # 4c. Per-trade stake cap — clamp down, never up.
    stake = min(decision.stake, limits.per_trade_stake_max)
    if stake <= 0:
        return GateResult(False, "non_positive_stake")

    return GateResult(True, "ok", stake=stake)


# ---------------------------------------------------------------------------
# Kill switch semantics (shared by bridge / browser agent / watchdogs)
# ---------------------------------------------------------------------------
KILL_ON_VALUES = {"1", "true", "on", "tripped", "halt", "stop"}
KILL_OFF_VALUES = {"0", "false", "off", "clear", "run"}


def interpret_kill_switch(raw: Optional[str]) -> bool:
    """Interpret a kill-switch flag value. Fail-closed: any unrecognized or
    missing value is treated as TRIPPED (True == do not trade)."""
    if raw is None:
        return True
    v = str(raw).strip().lower()
    if v in KILL_OFF_VALUES:
        return False
    if v in KILL_ON_VALUES:
        return True
    return True  # unknown -> tripped


def live_trading_enabled(raw: Optional[str]) -> bool:
    """Master enable flag. Defaults to DISABLED unless explicitly turned on.
    This is the operator's deliberate go-live act."""
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "on", "yes", "enabled"}
