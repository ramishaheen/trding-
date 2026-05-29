"""Pure risk checks used by the Risk Governor.

Each function is deterministic and dependency-free so every rule is unit-tested
exactly as it runs. They return CheckResult(name, passed, reason) or structured
values. The governor composes them, owns state, and makes the final decision.

FAIL CLOSED: any missing/invalid input results in a FAILED check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .config import RiskConfig
from .models import AccountSnapshot, CheckResult, MarketSnapshot, TradeSignal


# ---------------------------------------------------------------------------
# Completeness / data availability
# ---------------------------------------------------------------------------
MANDATORY_FIELDS = (
    "symbol", "side", "entry_price", "stop_loss_price", "take_profit_price",
    "quantity", "leverage", "margin_mode", "max_holding_time_minutes",
    "strategy_reason", "timestamp", "signal_id",
)


def check_signal_complete(signal: TradeSignal, cfg: RiskConfig) -> CheckResult:
    missing = [f for f in MANDATORY_FIELDS if getattr(signal, f, None) in (None, "")]
    # Stop loss / take profit can be individually allowed off via config, but
    # default (and spec) requires both.
    if cfg.allow_trade_without_stop_loss and "stop_loss_price" in missing:
        missing.remove("stop_loss_price")
    if cfg.allow_trade_without_take_profit and "take_profit_price" in missing:
        missing.remove("take_profit_price")
    if missing:
        return CheckResult("signal_complete", False, f"missing_fields:{','.join(missing)}")
    return CheckResult("signal_complete", True)


def check_account_known(account: AccountSnapshot) -> CheckResult:
    if not account or not account.known:
        return CheckResult("account_known", False, "account_state_unknown")
    if account.balance <= 0 or account.equity <= 0:
        return CheckResult("account_known", False, "non_positive_balance")
    return CheckResult("account_known", True)


def check_market_valid(market: MarketSnapshot, cfg: RiskConfig, now_ts: float) -> CheckResult:
    if not market or not market.known:
        return CheckResult("market_valid", False, "market_data_unavailable")
    if market.bid is None or market.ask is None or market.bid <= 0 or market.ask <= 0:
        return CheckResult("market_valid", False, "orderbook_unavailable")
    if market.last_price is None or market.last_price <= 0:
        return CheckResult("market_valid", False, "no_valid_price")
    if market.price_timestamp is None:
        return CheckResult("market_valid", False, "no_price_timestamp")
    if now_ts - market.price_timestamp > cfg.max_price_staleness_seconds:
        return CheckResult("market_valid", False,
                           f"stale_price({now_ts - market.price_timestamp:.1f}s)")
    if (market.orderbook_depth_quote is not None
            and market.orderbook_depth_quote < cfg.min_orderbook_depth_quote):
        return CheckResult("market_valid", False, "insufficient_orderbook_depth")
    return CheckResult("market_valid", True)


# ---------------------------------------------------------------------------
# Leverage / margin
# ---------------------------------------------------------------------------
def check_leverage_margin(signal: TradeSignal, cfg: RiskConfig) -> CheckResult:
    lev = signal.leverage
    if lev is None or lev <= 0:
        return CheckResult("leverage_margin", False, "leverage_unconfirmed")
    if lev > cfg.max_leverage:
        return CheckResult("leverage_margin", False, f"leverage>{cfg.max_leverage}")
    mode = (signal.margin_mode or "").lower()
    if mode == "cross" and not cfg.allow_cross_margin:
        return CheckResult("leverage_margin", False, "cross_margin_not_allowed")
    if cfg.force_isolated_margin and mode not in {"isolated", "spot"}:
        return CheckResult("leverage_margin", False, "isolated_margin_required")
    return CheckResult("leverage_margin", True)


# ---------------------------------------------------------------------------
# Stop loss / take profit / risk-reward
# ---------------------------------------------------------------------------
def compute_risk_reward(signal: TradeSignal) -> Optional[float]:
    e, sl, tp = signal.entry_price, signal.stop_loss_price, signal.take_profit_price
    if None in (e, sl, tp):
        return None
    if signal.is_long():
        risk = e - sl
        reward = tp - e
    else:
        risk = sl - e
        reward = e - tp
    if risk <= 0:
        return None
    return reward / risk


def check_stop_take_profit(signal: TradeSignal, cfg: RiskConfig) -> CheckResult:
    e = signal.entry_price
    sl = signal.stop_loss_price
    tp = signal.take_profit_price
    if not cfg.allow_trade_without_stop_loss and (sl is None or sl <= 0):
        return CheckResult("stop_take_profit", False, "missing_stop_loss")
    if not cfg.allow_trade_without_take_profit and (tp is None or tp <= 0):
        return CheckResult("stop_take_profit", False, "missing_take_profit")
    if e is None or e <= 0:
        return CheckResult("stop_take_profit", False, "missing_entry_price")
    # Directional sanity: stop on the losing side, target on the winning side.
    if sl is not None:
        if signal.is_long() and sl >= e:
            return CheckResult("stop_take_profit", False, "stop_not_below_entry_long")
        if not signal.is_long() and sl <= e:
            return CheckResult("stop_take_profit", False, "stop_not_above_entry_short")
    return CheckResult("stop_take_profit", True)


def check_risk_reward(signal: TradeSignal, cfg: RiskConfig) -> CheckResult:
    rr = compute_risk_reward(signal)
    if rr is None:
        return CheckResult("risk_reward", False, "risk_reward_invalid")
    if rr < cfg.min_risk_reward_ratio:
        return CheckResult("risk_reward", False, f"rr {rr:.2f}<{cfg.min_risk_reward_ratio}")
    return CheckResult("risk_reward", True)


def stop_distance_percent(signal: TradeSignal) -> Optional[float]:
    e, sl = signal.entry_price, signal.stop_loss_price
    if e is None or sl is None or e <= 0:
        return None
    return abs(e - sl) / e * 100.0


def check_stop_distance(signal: TradeSignal, cfg: RiskConfig) -> CheckResult:
    d = stop_distance_percent(signal)
    if d is None or d <= 0:
        return CheckResult("stop_distance", False, "stop_distance_zero_or_invalid")
    if d < cfg.min_stop_distance_percent:
        return CheckResult("stop_distance", False, f"stop_distance {d:.3f}%<min")
    if d > cfg.max_stop_distance_percent:
        return CheckResult("stop_distance", False, f"stop_distance {d:.3f}%>max")
    return CheckResult("stop_distance", True)


# ---------------------------------------------------------------------------
# Spread / slippage / volatility
# ---------------------------------------------------------------------------
def spread_percent(market: MarketSnapshot) -> Optional[float]:
    mid = market.mid()
    if mid is None or mid <= 0 or market.bid is None or market.ask is None:
        return None
    return (market.ask - market.bid) / mid * 100.0


def check_spread(market: MarketSnapshot, cfg: RiskConfig) -> CheckResult:
    s = spread_percent(market)
    if s is None:
        return CheckResult("spread", False, "spread_unavailable")
    if s > cfg.max_spread_percent:
        return CheckResult("spread", False, f"spread {s:.4f}%>{cfg.max_spread_percent}")
    return CheckResult("spread", True)


def check_slippage(market: MarketSnapshot, cfg: RiskConfig) -> CheckResult:
    s = market.estimated_slippage_percent
    if s is None:
        # Unknown slippage -> fail closed.
        return CheckResult("slippage", False, "slippage_unknown")
    if s > cfg.max_slippage_percent:
        return CheckResult("slippage", False, f"slippage {s:.4f}%>{cfg.max_slippage_percent}")
    return CheckResult("slippage", True)


def check_volatility(market: MarketSnapshot, cfg: RiskConfig) -> CheckResult:
    if market.atr is None or market.atr_avg_20 is None or market.atr_avg_20 <= 0:
        # If we can't assess volatility, do not assume calm.
        return CheckResult("volatility", False, "volatility_unknown")
    if market.atr > cfg.atr_spike_multiplier * market.atr_avg_20:
        return CheckResult("volatility", False, "atr_spike")
    return CheckResult("volatility", True)


# ---------------------------------------------------------------------------
# Position sizing (governor has final authority over quantity)
# ---------------------------------------------------------------------------
@dataclass
class SizingResult:
    ok: bool
    reason: str
    quantity: float = 0.0
    position_value: float = 0.0
    risk_amount: float = 0.0


def compute_position_size(
    signal: TradeSignal,
    account: AccountSnapshot,
    cfg: RiskConfig,
    size_multiplier: float = 1.0,
) -> SizingResult:
    """Risk-based sizing per spec. size_multiplier (<=1) applies post-loss
    reduction. Returns ok=False with a reason if the trade must be rejected."""
    e = signal.entry_price
    sl = signal.stop_loss_price
    if e is None or e <= 0:
        return SizingResult(False, "no_entry_price")
    if sl is None or sl <= 0:
        return SizingResult(False, "no_stop_loss")
    stop_dist_frac = abs(e - sl) / e
    if stop_dist_frac <= 0:
        return SizingResult(False, "stop_distance_zero")

    risk_amount = account.balance * cfg.max_risk_per_trade_percent / 100.0 * max(0.0, min(1.0, size_multiplier))
    raw_position_value = risk_amount / stop_dist_frac
    max_position_value = account.balance * cfg.max_capital_exposure_percent / 100.0
    final_value = min(raw_position_value, max_position_value)

    if final_value < cfg.min_order_value_usdt:
        return SizingResult(False, "below_min_order_size", 0.0, final_value, risk_amount)
    if final_value > max_position_value + 1e-9:
        return SizingResult(False, "exceeds_exposure_limit", 0.0, final_value, risk_amount)

    quantity = final_value / e
    return SizingResult(True, "ok", quantity, final_value, risk_amount)


def check_exposure(position_value: float, account: AccountSnapshot, cfg: RiskConfig) -> CheckResult:
    max_value = account.balance * cfg.max_capital_exposure_percent / 100.0
    if position_value > max_value + 1e-9:
        return CheckResult("exposure", False, "exposure_exceeded")
    return CheckResult("exposure", True)


# ---------------------------------------------------------------------------
# Anti-martingale / averaging-down
# ---------------------------------------------------------------------------
def check_no_averaging_down(signal: TradeSignal, account: AccountSnapshot,
                            cfg: RiskConfig) -> CheckResult:
    if cfg.allow_averaging_down:
        return CheckResult("averaging_down", True)
    if signal.symbol in (account.open_symbols or ()):
        return CheckResult("averaging_down", False, "position_already_open_symbol")
    return CheckResult("averaging_down", True)


def check_no_martingale(requested_value: float, prev_position_value: Optional[float],
                        last_trade_was_loss: bool, cfg: RiskConfig) -> CheckResult:
    if cfg.allow_martingale:
        return CheckResult("martingale", True)
    if last_trade_was_loss and prev_position_value is not None \
            and requested_value > prev_position_value + 1e-9:
        return CheckResult("martingale", False, "size_increase_after_loss")
    return CheckResult("martingale", True)


# ---------------------------------------------------------------------------
# Trade quality score (0..100)
# ---------------------------------------------------------------------------
def _component_from_rr(rr: Optional[float], cfg: RiskConfig) -> float:
    if rr is None:
        return 0.0
    if rr >= cfg.preferred_risk_reward_ratio:
        return 100.0
    if rr >= cfg.min_risk_reward_ratio:
        return 75.0
    return max(0.0, 75.0 * rr / cfg.min_risk_reward_ratio)


def _component_from_volatility(market: MarketSnapshot, cfg: RiskConfig) -> float:
    if market.atr is None or market.atr_avg_20 is None or market.atr_avg_20 <= 0:
        return 0.0
    ratio = market.atr / market.atr_avg_20
    if ratio <= 1.0:
        return 100.0
    if ratio >= cfg.atr_spike_multiplier:
        return 0.0
    # linear between 1.0 -> 100 and spike_mult -> 0
    return 100.0 * (cfg.atr_spike_multiplier - ratio) / (cfg.atr_spike_multiplier - 1.0)


def _component_from_spread(market: MarketSnapshot, cfg: RiskConfig) -> float:
    s = spread_percent(market)
    if s is None:
        return 0.0
    if s <= cfg.max_spread_percent / 2:
        return 100.0
    if s <= cfg.max_spread_percent:
        return 60.0
    return 0.0


def trade_quality_score(signal: TradeSignal, market: MarketSnapshot, cfg: RiskConfig) -> float:
    """Weighted 0..100 quality score. Strategy-provided components (0..100) in
    signal.quality_components override the neutral default for that component."""
    rr = compute_risk_reward(signal)
    computed = {
        "risk_reward": _component_from_rr(rr, cfg),
        "volatility": _component_from_volatility(market, cfg),
        "liquidity_spread": _component_from_spread(market, cfg),
    }
    component_names = [
        "trend_alignment", "htf_confirmation", "volume", "volatility",
        "liquidity_spread", "risk_reward", "recent_performance", "news_risk",
        "orderbook_quality", "regime_quality",
    ]
    provided = signal.quality_components or {}
    total = 0.0
    for name in component_names:
        if name in provided:
            total += max(0.0, min(100.0, float(provided[name])))
        elif name in computed:
            total += computed[name]
        else:
            total += 50.0  # neutral default for components we can't assess
    return total / len(component_names)


def check_trade_quality(signal: TradeSignal, market: MarketSnapshot, cfg: RiskConfig) -> CheckResult:
    score = trade_quality_score(signal, market, cfg)
    if score < cfg.trade_quality_min_score:
        return CheckResult("trade_quality", False, f"score {score:.0f}<{cfg.trade_quality_min_score}")
    return CheckResult("trade_quality", True)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def check_duplicate(signal: TradeSignal, account: AccountSnapshot,
                    recent_signals: Iterable[dict], cfg: RiskConfig, now_ts: float) -> CheckResult:
    """recent_signals: iterable of {signal_id, symbol, side, ts}."""
    for r in recent_signals:
        if r.get("signal_id") and r.get("signal_id") == signal.signal_id:
            return CheckResult("duplicate", False, "duplicate_signal_id")
        same = (r.get("symbol") == signal.symbol and
                (r.get("side") or "").lower() == (signal.side or "").lower())
        if same and (now_ts - float(r.get("ts", 0))) < cfg.duplicate_window_seconds:
            return CheckResult("duplicate", False, "duplicate_symbol_side_window")
    if signal.symbol in (account.open_symbols or ()) and not cfg.allow_averaging_down:
        return CheckResult("duplicate", False, "existing_position_same_symbol")
    return CheckResult("duplicate", True)
