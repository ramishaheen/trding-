"""Professional discipline layer — the things most bots ignore.

Pure, dependency-free, unit-tested. These functions encode the unglamorous edges
that separate survivable systems from blow-ups:

  * overtrading guard      — cap trades per hour/day + min spacing
  * correlation cap        — don't stack many highly-correlated positions
  * abnormal-market detect — kill-switch trigger on spread/vol/gap anomalies
  * slippage / exec guard  — reject fills worse than a tolerance
  * fee-aware edge filter   — skip trades whose edge can't clear fees

They are deliberately separate from strategy logic so a strategy change can't
silently weaken them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Overtrading guard
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeRateLimits:
    max_per_day: int = 10
    max_per_hour: int = 4
    min_seconds_between: int = 300  # 5 min spacing


@dataclass
class RateDecision:
    allow: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allow


def check_overtrading(
    now_ts: float,
    recent_entry_ts: Sequence[float],
    limits: TradeRateLimits,
) -> RateDecision:
    """`recent_entry_ts` = unix timestamps of recent entries (any order)."""
    if not recent_entry_ts:
        return RateDecision(True, "ok")

    last = max(recent_entry_ts)
    if now_ts - last < limits.min_seconds_between:
        return RateDecision(False, "min_spacing_not_elapsed")

    in_last_hour = sum(1 for t in recent_entry_ts if now_ts - t < 3600)
    if in_last_hour >= limits.max_per_hour:
        return RateDecision(False, "hourly_trade_cap_reached")

    in_last_day = sum(1 for t in recent_entry_ts if now_ts - t < 86400)
    if in_last_day >= limits.max_per_day:
        return RateDecision(False, "daily_trade_cap_reached")

    return RateDecision(True, "ok")


# ---------------------------------------------------------------------------
# Correlation exposure cap
# ---------------------------------------------------------------------------
def correlation_exposure_ok(
    new_pair: str,
    open_pairs: Iterable[str],
    correlations: dict[tuple[str, str], float],
    max_correlated: int = 1,
    threshold: float = 0.8,
) -> bool:
    """True if opening `new_pair` would not exceed `max_correlated` positions
    that are correlated to it above `threshold`.

    `correlations` maps an unordered pair-of-symbols to a coefficient in [-1,1].
    Missing entries are treated as uncorrelated (0).
    """
    open_pairs = [p for p in open_pairs if p != new_pair]
    correlated = 0
    for p in open_pairs:
        key = tuple(sorted((new_pair, p)))
        corr = correlations.get(key, correlations.get((new_pair, p), 0.0))
        if abs(corr) >= threshold:
            correlated += 1
    return correlated < max_correlated


# ---------------------------------------------------------------------------
# Abnormal-market detector (kill-switch trigger)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AbnormalThresholds:
    max_spread_pct: float = 0.005    # 0.5% bid/ask spread
    max_atr_pct: float = 0.08        # 8% ATR/price = chaotic
    max_gap_pct: float = 0.05        # 5% candle-to-candle gap


@dataclass
class AbnormalResult:
    abnormal: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.abnormal


def detect_abnormal_market(
    spread_pct: float | None,
    atr_pct: float | None,
    gap_pct: float | None,
    th: AbnormalThresholds,
) -> AbnormalResult:
    """Flag market conditions where automated trading should pause/halt.
    Unknown (None) metrics are skipped (not treated as abnormal here; the gate's
    own fail-closed rules still apply)."""
    reasons: list[str] = []
    if spread_pct is not None and spread_pct > th.max_spread_pct:
        reasons.append(f"spread {spread_pct:.4f}>{th.max_spread_pct}")
    if atr_pct is not None and atr_pct > th.max_atr_pct:
        reasons.append(f"volatility {atr_pct:.4f}>{th.max_atr_pct}")
    if gap_pct is not None and abs(gap_pct) > th.max_gap_pct:
        reasons.append(f"gap {gap_pct:.4f}>{th.max_gap_pct}")
    return AbnormalResult(bool(reasons), reasons)


# ---------------------------------------------------------------------------
# Slippage / execution-quality guard
# ---------------------------------------------------------------------------
def slippage_pct(expected_price: float, fill_price: float, side: str) -> float:
    """Signed adverse slippage as a fraction. Positive == worse than expected.
    For a buy, paying more is adverse; for a sell, receiving less is adverse."""
    if expected_price <= 0:
        return 0.0
    if side == "buy":
        return (fill_price - expected_price) / expected_price
    return (expected_price - fill_price) / expected_price


def slippage_ok(expected_price: float, fill_price: float, side: str, max_slippage_pct: float) -> bool:
    return slippage_pct(expected_price, fill_price, side) <= max_slippage_pct


# ---------------------------------------------------------------------------
# Fee-aware edge filter
# ---------------------------------------------------------------------------
def edge_clears_fees(expected_gain_pct: float, fee_pct: float, safety_mult: float = 2.0) -> bool:
    """A trade should only fire if its expected move comfortably exceeds the
    round-trip fee (entry + exit), with a safety multiple. Avoids churning
    capital on trades the fees eat alive."""
    round_trip_fees = 2.0 * fee_pct
    return expected_gain_pct >= round_trip_fees * safety_mult
