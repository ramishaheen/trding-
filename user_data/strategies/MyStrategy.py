"""MyStrategy — higher-timeframe trend filter + pullback entry + ATR stop.

Spot, long-only, no leverage. Built for BingX spot in dry-run.

The actual *rules* live in `strategy_logic.py` (a dependency-free module that is
unit-tested in CI). This class wires those rules into the freqtrade interface
using pandas-ta for vectorised indicator computation, exposes every threshold as
a hyperopt parameter, and applies the LLM `market_context` as a SOFT gate only.

Design notes
------------
* No look-ahead bias: freqtrade feeds only closed candles into
  populate_* methods; we never reference future rows.
* The market_context soft gate can only *restrict* entries (block or shrink
  stake). It can never open or force a trade.
* A hard ATR-based stoploss is set both via `custom_stoploss` and enforced
  on-exchange (`stoploss_on_exchange` in config.json).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import pandas as pd
import pandas_ta as pta
from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    DecimalParameter,
    informative,
)

import json
import urllib.request

from strategy_logic import (
    StrategyParams,
    apply_context_gate,
    atr_stoploss_ratio,
    bias_quality_overrides,
    build_entry_signal,
    build_exit_signal,
)

logger = logging.getLogger(__name__)

# The bridge that runs the Risk Governor pipeline. The strategy PROPOSES trades
# here; it never sends orders to the exchange itself.
BRIDGE_URL = os.environ.get("EXECUTION_BRIDGE_URL", "http://execution-bridge:8090").rstrip("/")
WEBHOOK_TOKEN = os.environ.get("EXECUTION_WEBHOOK_TOKEN", "")


def _read_latest_market_context() -> tuple[Optional[str], Optional[float], bool]:
    """Fetch the latest (risk_state, confidence, pause_trading) from market_context.

    Fails open: any error (no DB, no driver, empty table) returns
    (None, None, False) -> the soft gate treats it as "no context -> allow".
    Hard risk controls (governor + watchdog + on-exchange stops) are independent.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None, None, False
    try:
        import psycopg  # imported lazily; absent in pure backtests

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT risk_state, confidence, pause_trading FROM market_context "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0], float(row[1]), bool(row[2])
    except Exception as exc:  # noqa: BLE001 - fail open by design
        logger.warning("market_context read failed (gate fails open): %s", exc)
    return None, None, False


def _read_pair_bias(pair: str) -> Optional[str]:
    """Latest LLM per-pair bias (bullish|bearish|neutral) for `pair`, or None.
    Fails open (None) so a missing DB/driver never blocks the paper engine."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT per_pair_bias FROM market_context "
                        "ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                data = row[0] if isinstance(row[0], list) else json.loads(row[0])
                for item in data:
                    if isinstance(item, dict) and str(item.get("pair", "")).upper() == pair.upper():
                        return str(item.get("bias", "")).lower()
    except Exception as exc:  # noqa: BLE001 - fail open
        logger.warning("per-pair bias read failed: %s", exc)
    return None


def _emit_signal(payload: dict) -> None:
    """POST a proposed trade signal to the execution bridge (best-effort).

    The bridge runs it through the Weekly Target Manager + Risk Governor; only an
    approved, armed signal becomes a real order. Never raises into freqtrade.
    """
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{BRIDGE_URL}/webhook/decision", data=data,
                                     headers={"Content-Type": "application/json",
                                              "X-Webhook-Token": WEBHOOK_TOKEN})
        urllib.request.urlopen(req, timeout=4).read()
    except Exception as exc:  # noqa: BLE001 - execution is decoupled; paper engine unaffected
        logger.warning("signal emit failed (paper engine unaffected): %s", exc)


class MyStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False  # spot, long-only

    # Moved from config.json: freqtrade 2026.x rejects 'protections' in the config file.
    protections = [{'method': 'CooldownPeriod', 'stop_duration_candles': 4}, {'method': 'MaxDrawdown', 'lookback_period_candles': 48, 'trade_limit': 10, 'stop_duration_candles': 12, 'max_allowed_drawdown': 0.1}, {'method': 'StoplossGuard', 'lookback_period_candles': 24, 'trade_limit': 4, 'stop_duration_candles': 12, 'only_per_pair': False}, {'method': 'LowProfitPairs', 'lookback_period_candles': 360, 'trade_limit': 4, 'stop_duration_candles': 60, 'required_profit': 0.0}]

    # ROI / stoploss: the real stop is ATR-based via custom_stoploss; this is a
    # conservative static backstop.
    minimal_roi = {"0": 0.10, "240": 0.04, "480": 0.02, "720": 0}
    stoploss = -0.10
    use_custom_stoploss = True

    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 220  # >= ema_trend (200) + buffer

    # --- Hyperopt-able parameters (defaults mirror StrategyParams) ----------
    buy_ema_fast = IntParameter(10, 40, default=21, space="buy", optimize=True)
    buy_ema_slow = IntParameter(30, 100, default=50, space="buy", optimize=True)
    buy_ema_trend = IntParameter(150, 250, default=200, space="buy", optimize=False)
    buy_rsi_min = DecimalParameter(20, 45, default=35, decimals=0, space="buy", optimize=True)
    buy_rsi_max = DecimalParameter(45, 65, default=55, decimals=0, space="buy", optimize=True)
    buy_pullback_pct = DecimalParameter(0.005, 0.05, default=0.02, decimals=3, space="buy", optimize=True)
    sell_rsi = DecimalParameter(65, 85, default=75, decimals=0, space="sell", optimize=True)
    atr_period = IntParameter(7, 28, default=14, space="sell", optimize=True)
    atr_stop_mult = DecimalParameter(1.0, 4.0, default=2.0, decimals=1, space="sell", optimize=True)
    take_profit_rr = DecimalParameter(1.5, 3.0, default=2.0, decimals=1, space="sell", optimize=False)
    max_holding_minutes = IntParameter(60, 4320, default=1440, space="sell", optimize=False)
    # Regime filter: skip entries when volatility (ATR/price) is too high
    # (chaotic / chop). Hyperopt-tunable so walk-forward can optimise it.
    buy_max_atr_pct = DecimalParameter(0.01, 0.08, default=0.04, decimals=3, space="buy", optimize=True)

    def _params(self) -> StrategyParams:
        return StrategyParams(
            ema_fast=int(self.buy_ema_fast.value),
            ema_slow=int(self.buy_ema_slow.value),
            ema_trend=int(self.buy_ema_trend.value),
            rsi_entry_min=float(self.buy_rsi_min.value),
            rsi_entry_max=float(self.buy_rsi_max.value),
            rsi_exit=float(self.sell_rsi.value),
            atr_period=int(self.atr_period.value),
            atr_stop_mult=float(self.atr_stop_mult.value),
            pullback_pct=float(self.buy_pullback_pct.value),
        )

    # -----------------------------------------------------------------------
    # Indicators
    # -----------------------------------------------------------------------
    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        p = self._params()
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=p.ema_fast)
        dataframe["ema_slow"] = pta.ema(dataframe["close"], length=p.ema_slow)
        dataframe["ema_trend"] = pta.ema(dataframe["close"], length=p.ema_trend)
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=p.rsi_period)
        dataframe["atr"] = pta.atr(
            dataframe["high"], dataframe["low"], dataframe["close"], length=p.atr_period
        )
        # distance of close from fast EMA (for the pullback test)
        dataframe["dist_fast"] = (dataframe["close"] - dataframe["ema_fast"]) / dataframe["ema_fast"]
        return dataframe

    # -----------------------------------------------------------------------
    # Entry / exit (vectorised, but using the same thresholds as strategy_logic)
    # -----------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        p = self._params()
        # Uptrend: price and mid-trend EMA above the long trend EMA.
        uptrend = (dataframe["close"] > dataframe["ema_trend"]) & (
            dataframe["ema_slow"] > dataframe["ema_trend"]
        )
        # Volatility filter — skip entries only when ATR/price is high (chaotic /
        # spike conditions). Tunable via buy_max_atr_pct. (The stricter stacked-
        # EMA gate was removed: it made entries far too rare. Re-add via hyperopt
        # if walk-forward shows it helps.)
        calm = (dataframe["atr"] / dataframe["close"]) <= float(self.buy_max_atr_pct.value)
        pullback = dataframe["dist_fast"].between(-p.pullback_pct, p.pullback_pct)
        rsi_ok = dataframe["rsi"].between(p.rsi_entry_min, p.rsi_entry_max)

        dataframe.loc[
            uptrend & calm & pullback & rsi_ok & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "trend_pullback")
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        p = self._params()
        momentum_exhausted = dataframe["rsi"] >= p.rsi_exit
        trend_lost = dataframe["close"] < dataframe["ema_trend"]
        dataframe.loc[
            (momentum_exhausted | trend_lost) & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "exit_trend_or_momentum")
        return dataframe

    # -----------------------------------------------------------------------
    # Hard ATR stoploss (also enforced on-exchange via config)
    # -----------------------------------------------------------------------
    def custom_stoploss(
        self, pair, trade, current_time, current_rate, current_profit, after_fill, **kwargs
    ) -> float:
        p = self._params()
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return self.stoploss
        atr_value = float(df["atr"].iloc[-1])
        if atr_value <= 0 or pd.isna(atr_value):
            return self.stoploss
        return atr_stoploss_ratio(trade.open_rate, atr_value, p)

    # -----------------------------------------------------------------------
    # LLM market-context SOFT gate
    # -----------------------------------------------------------------------
    def confirm_trade_entry(
        self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, **kwargs
    ) -> bool:
        risk_state, confidence, pause = _read_latest_market_context()
        gate = apply_context_gate(risk_state, confidence, self._params(), pause_trading=pause)
        if not gate.allow_new_entries:
            logger.info("Entry on %s blocked by soft gate: %s", pair, gate.reason)
            return False

        # Propose a COMPLETE trade to the Risk Governor pipeline (live path).
        # The paper engine still records its own dry-run trade; the governor
        # independently decides whether a REAL order is placed.
        p = self._params()
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is not None and not df.empty:
            atr_value = float(df["atr"].iloc[-1])
            atr_avg = float(df["atr"].tail(20).mean()) if len(df) >= 20 else atr_value
            if atr_value > 0 and not pd.isna(atr_value):
                # A bearish LLM read on this coin lowers its trade-quality score
                # (can only tighten — never raises the bar to open a trade).
                extra_quality = bias_quality_overrides(_read_pair_bias(pair))
                _emit_signal(build_entry_signal(
                    pair, float(rate), atr_value, atr_avg, p,
                    current_time.timestamp(),
                    take_profit_rr=float(self.take_profit_rr.value),
                    max_holding_minutes=int(self.max_holding_minutes.value),
                    extra_quality=extra_quality,
                ))
        return True

    def confirm_trade_exit(
        self, pair, trade, order_type, amount, rate, time_in_force, exit_reason,
        current_time, **kwargs
    ) -> bool:
        # Mirror the exit to the live path so the governor can flatten the real
        # position. Exits are always allowed through the risk gate.
        _emit_signal(build_exit_signal(pair, float(amount), current_time.timestamp(),
                                       reason=str(exit_reason)))
        return True

    def custom_stake_amount(
        self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
        leverage, entry_tag, side, **kwargs
    ) -> float:
        risk_state, confidence, pause = _read_latest_market_context()
        gate = apply_context_gate(risk_state, confidence, self._params(), pause_trading=pause)
        stake = proposed_stake * gate.stake_multiplier
        if min_stake is not None and stake < min_stake:
            # If the reduced stake falls below the venue minimum, skip rather
            # than upsize (the gate must never increase exposure).
            return 0.0
        return stake
