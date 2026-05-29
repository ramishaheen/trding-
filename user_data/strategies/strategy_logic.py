"""Pure, dependency-free trading logic for MyStrategy.

This module deliberately avoids importing freqtrade, pandas, or pandas-ta so
that the *decision rules* (the part that actually matters for correctness) can
be unit-tested in any environment, including CI, without the heavy trading
stack installed.

`MyStrategy` (the freqtrade class) reuses the thresholds and the scalar
decision functions defined here, so the rules tested in `tests/test_strategy.py`
are the same rules the live bot runs. There is no second, untested copy.

Strategy summary
----------------
Higher-timeframe trend filter + pullback entry + ATR-based stop:

  * Trend filter: price must be above a slow trend EMA (e.g. EMA200) for longs.
    (Spot, long-only — no shorting, no leverage.)
  * Pullback entry: within an uptrend, enter when price dips toward a faster
    EMA (e.g. EMA21) and RSI shows the pullback is not yet overbought, i.e.
    momentum is resetting rather than breaking down.
  * Exit: momentum exhaustion (RSI high) or loss of trend (close below the
    trend EMA). A hard ATR-based stoploss is enforced separately on-exchange.

No look-ahead bias: every function consumes values that, in the freqtrade
strategy, come from already-closed candles only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# Tunable thresholds (all hyperopt-able; mirrored as parameters in MyStrategy)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyParams:
    ema_fast: int = 21          # pullback reference EMA
    ema_slow: int = 50          # intermediate trend EMA
    ema_trend: int = 200        # higher-timeframe trend filter EMA
    rsi_period: int = 14
    rsi_entry_max: float = 55.0  # don't buy into overbought pullbacks
    rsi_entry_min: float = 35.0  # don't catch falling knives
    rsi_exit: float = 75.0       # momentum-exhaustion exit
    atr_period: int = 14
    atr_stop_mult: float = 2.0   # hard stop = entry - mult * ATR
    pullback_pct: float = 0.02   # "near" the fast EMA = within 2%

    # Soft gate (driven by the LLM market_context row).
    min_context_confidence: float = 0.0  # 0 disables the confidence gate


# ---------------------------------------------------------------------------
# Minimal pure-python indicators (used by unit tests; MyStrategy uses pandas-ta)
# ---------------------------------------------------------------------------
def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average. Returns a list aligned with `values`.

    The first `period-1` entries are seeded with a running simple average so
    the series is fully defined (matches the common pandas-ta behaviour closely
    enough for the decision rules; tests assert on direction, not exact ties).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out: list[float] = []
    prev = values[0]
    for i, v in enumerate(values):
        if i == 0:
            out.append(v)
        else:
            prev = v * k + prev * (1.0 - k)
            out.append(prev)
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Returns list aligned with `values`; warm-up filled with 50."""
    n = len(values)
    out = [50.0] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float]:
    """Average True Range (Wilder smoothing). Returns list aligned with input."""
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows, closes must be equal length")
    if n == 0:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    out = [0.0] * n
    if n < period:
        # not enough data; return running mean
        running = 0.0
        for i in range(n):
            running += trs[i]
            out[i] = running / (i + 1)
        return out
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Scalar decision rules (single source of truth, reused by MyStrategy)
# ---------------------------------------------------------------------------
def is_uptrend(close: float, ema_slow: float, ema_trend: float) -> bool:
    """Higher-timeframe trend filter: only consider longs in a clear uptrend."""
    return close > ema_trend and ema_slow > ema_trend


def is_pullback(close: float, ema_fast: float, pullback_pct: float) -> bool:
    """Price has dipped toward (within pullback_pct of, or just below) the
    fast EMA — a healthy pause inside the trend rather than a breakdown."""
    if ema_fast <= 0:
        return False
    distance = (close - ema_fast) / ema_fast
    # near the fast EMA from either side, but not extended far above it
    return -pullback_pct <= distance <= pullback_pct


def entry_signal(
    close: float,
    ema_fast: float,
    ema_slow: float,
    ema_trend: float,
    rsi_value: float,
    params: StrategyParams,
) -> bool:
    """Long entry rule. Pure function of closed-candle indicator values."""
    if not is_uptrend(close, ema_slow, ema_trend):
        return False
    if not is_pullback(close, ema_fast, params.pullback_pct):
        return False
    if not (params.rsi_entry_min <= rsi_value <= params.rsi_entry_max):
        return False
    return True


def exit_signal(
    close: float,
    ema_trend: float,
    rsi_value: float,
    params: StrategyParams,
) -> bool:
    """Long exit rule: momentum exhaustion OR loss of the trend filter."""
    if rsi_value >= params.rsi_exit:
        return True
    if close < ema_trend:
        return True
    return False


def atr_stop_price(entry_price: float, atr_value: float, params: StrategyParams) -> float:
    """Absolute hard-stop price = entry - mult * ATR."""
    return entry_price - params.atr_stop_mult * atr_value


def atr_stoploss_ratio(entry_price: float, atr_value: float, params: StrategyParams) -> float:
    """Stoploss as a negative ratio relative to entry (freqtrade convention).

    e.g. -0.04 means a 4% stop. Clamped to (-0.99, 0) for safety.
    """
    if entry_price <= 0:
        return -0.99
    stop = atr_stop_price(entry_price, atr_value, params)
    ratio = (stop - entry_price) / entry_price
    return max(min(ratio, -1e-4), -0.99)


# ---------------------------------------------------------------------------
# Risk-based position sizing (the highest-leverage discipline upgrade)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SizingConfig:
    capital: float                       # account equity in quote (USDT)
    risk_per_trade_pct: float = 0.01     # risk 1% of capital per trade
    max_stake: float = 100.0             # hard per-trade stake cap (quote)
    min_stake: float = 10.0              # venue minimum (quote); below -> skip
    max_stake_pct_of_capital: float = 0.25  # never >25% of capital in one trade


def risk_based_stake(entry_price: float, stop_price: float, cfg: SizingConfig) -> float:
    """Quote-currency stake such that being stopped out costs ~risk_per_trade_pct
    of capital. Size is driven by the distance to the stop, not a flat amount.

        risk_amount        = capital * risk_per_trade_pct
        stop_distance_frac = (entry - stop) / entry
        notional           = risk_amount / stop_distance_frac

    Clamped by max_stake and max_stake_pct_of_capital; returns 0 (skip) if the
    sized notional would fall below min_stake or the stop is invalid.
    """
    if entry_price <= 0 or cfg.capital <= 0:
        return 0.0
    if stop_price <= 0 or stop_price >= entry_price:
        return 0.0
    stop_distance_frac = (entry_price - stop_price) / entry_price
    if stop_distance_frac <= 0:
        return 0.0
    notional = (cfg.capital * cfg.risk_per_trade_pct) / stop_distance_frac
    notional = min(notional, cfg.max_stake, cfg.capital * cfg.max_stake_pct_of_capital)
    return notional if notional >= cfg.min_stake else 0.0


# ---------------------------------------------------------------------------
# Volatility filter + market-microstructure detectors (entry quality)
# ---------------------------------------------------------------------------
def volatility_filter_ok(atr_value: float, close: float, max_atr_pct: float) -> bool:
    """True when volatility is within sane bounds. Skipping entries in
    chaotic, high-ATR conditions is one of the cheapest edges available."""
    if close <= 0:
        return False
    return (atr_value / close) <= max_atr_pct


def liquidity_sweep_long(prior_low: float, low: float, close: float, open_: float) -> bool:
    """Detect a bullish liquidity sweep (stop hunt): price dips BELOW the prior
    low (sweeping resting stops) but CLOSES back above it with a green body.
    Often a higher-quality long entry than a naive breakout."""
    swept = low < prior_low
    reclaimed = close > prior_low
    bullish_body = close >= open_
    return swept and reclaimed and bullish_body


def fake_breakout_up(prior_high: float, high: float, close: float) -> bool:
    """Detect a failed/false upside breakout: price pokes ABOVE the prior high
    but CLOSES back below it. A reason to NOT chase the breakout long."""
    poked = high > prior_high
    failed = close < prior_high
    return poked and failed


def regime_from_prices(
    close: float,
    ema_slow: float,
    ema_trend: float,
    atr_value: float,
    high_vol_atr_pct: float = 0.05,
) -> str:
    """Deterministic regime classifier used to CROSS-CHECK the LLM signal so the
    system never depends solely on the model. Returns one of:
    trending_up | trending_down | ranging | high_vol."""
    if close > 0 and (atr_value / close) >= high_vol_atr_pct:
        return "high_vol"
    if close > ema_trend and ema_slow > ema_trend:
        return "trending_up"
    if close < ema_trend and ema_slow < ema_trend:
        return "trending_down"
    return "ranging"


# ---------------------------------------------------------------------------
# LLM market-context SOFT gate
# ---------------------------------------------------------------------------
@dataclass
class ContextGate:
    """Decision returned by the soft gate. The gate can only *restrict* trading;
    it can never force or open a trade."""
    allow_new_entries: bool = True
    stake_multiplier: float = 1.0
    reason: str = "no_context"


def apply_context_gate(
    risk_state: str | None,
    confidence: float | None,
    params: StrategyParams,
    pause_trading: bool = False,
) -> ContextGate:
    """Translate the latest market_context row into an entry gate.

    Rules (soft — restrict only):
      * pause_trading=True   -> block new entries (NEWS PAUSE MODE)
      * risk_off             -> block new entries
      * neutral              -> half stake
      * confidence below min -> block new entries
      * missing context      -> allow (fail-open on *information*, but the hard
                                risk watchdog and on-exchange stops still apply)
    """
    # News pause mode: the sidecar flagged a high-impact event window.
    if pause_trading:
        return ContextGate(False, 0.0, "news_pause")

    if risk_state is None and confidence is None:
        return ContextGate(True, 1.0, "no_context")

    conf = 0.0 if confidence is None else float(confidence)
    state = (risk_state or "neutral").lower()

    if params.min_context_confidence > 0 and conf < params.min_context_confidence:
        return ContextGate(False, 0.0, f"low_confidence({conf:.2f})")

    if state == "risk_off":
        return ContextGate(False, 0.0, "risk_off")
    if state == "neutral":
        return ContextGate(True, 0.5, "neutral_reduce_stake")
    # risk_on
    return ContextGate(True, 1.0, "risk_on")


# ---------------------------------------------------------------------------
# Live signal builders — the strategy proposes a COMPLETE trade for the
# Risk Governor to approve. (The governor recomputes size and may reject.)
# ---------------------------------------------------------------------------
def build_entry_signal(
    pair: str,
    entry_price: float,
    atr: float,
    atr_avg_20: float,
    params: StrategyParams,
    now_ts: float,
    *,
    take_profit_rr: float = 2.0,
    max_holding_minutes: float = 1440,
    signal_id: Optional[str] = None,
    extra_quality: Optional[dict] = None,
) -> dict:
    """A full long-entry signal with ATR-based stop and an RR-based take-profit.

    The strategy only emits this when its own trend/pullback/RSI rules fire, so
    trend & higher-timeframe components score high; the governor fills in the
    risk:reward / volatility / spread components from live market data.
    """
    stop = entry_price - atr * params.atr_stop_mult
    take_profit = entry_price + atr * params.atr_stop_mult * take_profit_rr
    quality = {
        "trend_alignment": 85, "htf_confirmation": 85, "volume": 80,
        "recent_performance": 75, "news_risk": 75, "orderbook_quality": 80,
        "regime_quality": 80,
    }
    if extra_quality:
        quality.update(extra_quality)
    return {
        "action": "enter", "pair": pair, "side": "long", "order_type": "market",
        "entry_price": round(entry_price, 8), "price": round(entry_price, 8),
        "stop_loss_price": round(stop, 8), "take_profit_price": round(take_profit, 8),
        "leverage": 1, "margin_mode": "spot",
        "max_holding_time_minutes": max_holding_minutes,
        "signal_id": signal_id or f"{pair}-{int(now_ts)}",
        "strategy_reason": "trend_pullback",
        "atr": round(atr, 8), "atr_avg_20": round(atr_avg_20, 8) if atr_avg_20 else None,
        "quality_components": quality,
    }


def build_exit_signal(pair: str, amount: Optional[float], now_ts: float,
                      reason: str = "exit") -> dict:
    """A flatten/exit signal. Exits are never blocked by risk caps."""
    return {
        "action": "exit", "pair": pair, "side": "long", "order_type": "market",
        "amount": amount, "signal_id": f"{pair}-exit-{int(now_ts)}",
        "strategy_reason": reason, "tag": reason,
    }


def bias_quality_overrides(bias: Optional[str]) -> dict:
    """Translate the LLM's per-pair bias into trade-quality component overrides.

    SOFT-GATE INVARIANT: this can only LOWER quality (tighten). A *bearish* read
    pushes several components down so the governor's quality score is more likely
    to fall below the minimum and reject the trade. *bullish* / *neutral* / None
    return {} — the LLM never RAISES the bar to open a trade the strategy
    wouldn't otherwise take. It can restrict, never force.
    """
    if (bias or "").lower() == "bearish":
        return {"trend_alignment": 30, "regime_quality": 15,
                "news_risk": 20, "recent_performance": 40}
    return {}
