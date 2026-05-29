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

from strategy_logic import (
    StrategyParams,
    apply_context_gate,
    atr_stoploss_ratio,
)

logger = logging.getLogger(__name__)


def _read_latest_market_context() -> tuple[Optional[str], Optional[float]]:
    """Fetch the latest (risk_state, confidence) from the market_context table.

    Fails open: any error (no DB, no driver, empty table) returns (None, None),
    which the soft gate treats as "no context -> allow". Hard risk controls
    (watchdog + on-exchange stops) are independent of this.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None, None
    try:
        import psycopg  # imported lazily; absent in pure backtests

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT risk_state, confidence FROM market_context "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0], float(row[1])
    except Exception as exc:  # noqa: BLE001 - fail open by design
        logger.warning("market_context read failed (gate fails open): %s", exc)
    return None, None


class MyStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False  # spot, long-only

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
        uptrend = (dataframe["close"] > dataframe["ema_trend"]) & (
            dataframe["ema_slow"] > dataframe["ema_trend"]
        )
        pullback = dataframe["dist_fast"].between(-p.pullback_pct, p.pullback_pct)
        rsi_ok = dataframe["rsi"].between(p.rsi_entry_min, p.rsi_entry_max)

        dataframe.loc[
            uptrend & pullback & rsi_ok & (dataframe["volume"] > 0),
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
        gate = apply_context_gate(*_read_latest_market_context(), params=self._params())
        if not gate.allow_new_entries:
            logger.info("Entry on %s blocked by soft gate: %s", pair, gate.reason)
            return False
        return True

    def custom_stake_amount(
        self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
        leverage, entry_tag, side, **kwargs
    ) -> float:
        gate = apply_context_gate(*_read_latest_market_context(), params=self._params())
        stake = proposed_stake * gate.stake_multiplier
        if min_stake is not None and stake < min_stake:
            # If the reduced stake falls below the venue minimum, skip rather
            # than upsize (the gate must never increase exposure).
            return 0.0
        return stake
