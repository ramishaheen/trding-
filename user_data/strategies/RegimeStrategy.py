"""RegimeStrategy — a regime-aware alternative to MyStrategy, for honest A/B
backtesting.

Idea: most of MyStrategy's losses came from longing a weak/choppy market. This
variant classifies the regime each candle and only acts where a long has an
edge:

  * trending_up  -> buy pullbacks (trend-following), same as the core idea
  * ranging      -> buy oversold dips to the lower Bollinger band (mean reversion)
  * trending_down / high_vol -> DO NOTHING (no longs into weakness or chaos)

Spot, long-only, no leverage. ATR-based stop (shared with MyStrategy via
strategy_logic). No look-ahead: only closed candles are used.

Backtest it head-to-head:
  docker compose run --rm freqtrade backtesting --config user_data/config.json \
    --strategy RegimeStrategy --timeframe 1h --timerange <range>
Then compare Tot Profit % / Profit factor / Drawdown against MyStrategy.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as pta
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter

from strategy_logic import StrategyParams, atr_stoploss_ratio


class RegimeStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"          # regime + mean-reversion suit 1h; override via --timeframe
    can_short = False

    # Fee-aware targets (round-trip ~0.2%): every ROI step nets positive.
    minimal_roi = {"0": 0.012, "60": 0.006, "180": 0.003}
    stoploss = -0.05
    use_custom_stoploss = True
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 220

    # --- tunable parameters ---
    buy_ema_fast = IntParameter(10, 40, default=21, space="buy", optimize=True)
    buy_ema_slow = IntParameter(30, 100, default=50, space="buy", optimize=True)
    buy_ema_trend = IntParameter(150, 250, default=200, space="buy", optimize=False)
    buy_rsi_min = DecimalParameter(25, 50, default=35, decimals=0, space="buy", optimize=True)
    buy_rsi_max = DecimalParameter(50, 70, default=62, decimals=0, space="buy", optimize=True)
    buy_pullback_pct = DecimalParameter(0.005, 0.06, default=0.03, decimals=3, space="buy", optimize=True)
    buy_rsi_oversold = DecimalParameter(15, 40, default=30, decimals=0, space="buy", optimize=True)
    high_vol_atr_pct = DecimalParameter(0.02, 0.10, default=0.05, decimals=3, space="buy", optimize=True)
    sell_rsi = DecimalParameter(60, 85, default=72, decimals=0, space="sell", optimize=True)
    atr_period = IntParameter(7, 28, default=14, space="sell", optimize=True)
    atr_stop_mult = DecimalParameter(1.0, 4.0, default=1.8, decimals=1, space="sell", optimize=True)

    def _params(self) -> StrategyParams:
        return StrategyParams(
            ema_fast=int(self.buy_ema_fast.value), ema_slow=int(self.buy_ema_slow.value),
            ema_trend=int(self.buy_ema_trend.value), rsi_entry_min=float(self.buy_rsi_min.value),
            rsi_entry_max=float(self.buy_rsi_max.value), rsi_exit=float(self.sell_rsi.value),
            atr_period=int(self.atr_period.value), atr_stop_mult=float(self.atr_stop_mult.value),
            pullback_pct=float(self.buy_pullback_pct.value),
        )

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        p = self._params()
        dataframe["ema_fast"] = pta.ema(dataframe["close"], length=p.ema_fast)
        dataframe["ema_slow"] = pta.ema(dataframe["close"], length=p.ema_slow)
        dataframe["ema_trend"] = pta.ema(dataframe["close"], length=p.ema_trend)
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=p.rsi_period)
        dataframe["atr"] = pta.atr(dataframe["high"], dataframe["low"], dataframe["close"],
                                   length=p.atr_period)
        bb = pta.bbands(dataframe["close"], length=20, std=2.0)
        dataframe["bb_lower"] = bb.iloc[:, 0] if bb is not None else dataframe["close"]
        dataframe["dist_fast"] = (dataframe["close"] - dataframe["ema_fast"]) / dataframe["ema_fast"]
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        p = self._params()
        high_vol = dataframe["atr_pct"] >= float(self.high_vol_atr_pct.value)
        strong_up = ((dataframe["close"] > dataframe["ema_trend"])
                     & (dataframe["ema_slow"] > dataframe["ema_trend"])
                     & (dataframe["ema_fast"] > dataframe["ema_slow"]))
        downtrend = ((dataframe["close"] < dataframe["ema_trend"])
                     & (dataframe["ema_slow"] < dataframe["ema_trend"]))
        ranging = ~strong_up & ~downtrend & ~high_vol
        vol = dataframe["volume"] > 0

        # Trend-following: pullback inside a confirmed uptrend.
        trend_long = (strong_up & ~high_vol
                      & dataframe["dist_fast"].between(-p.pullback_pct, p.pullback_pct)
                      & dataframe["rsi"].between(p.rsi_entry_min, p.rsi_entry_max))
        # Mean-reversion: oversold dip to the lower band in a range.
        range_long = (ranging & (dataframe["rsi"] < float(self.buy_rsi_oversold.value))
                      & (dataframe["close"] <= dataframe["bb_lower"] * 1.003))

        dataframe.loc[trend_long & vol, ["enter_long", "enter_tag"]] = (1, "trend_up_pullback")
        dataframe.loc[range_long & ~trend_long & vol, ["enter_long", "enter_tag"]] = (1, "range_meanrev")
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        p = self._params()
        momentum_done = dataframe["rsi"] >= p.rsi_exit
        trend_lost = dataframe["close"] < dataframe["ema_trend"]
        reverted = dataframe["rsi"] >= 55          # mean-reversion target hit
        dataframe.loc[(momentum_done | trend_lost | reverted) & (dataframe["volume"] > 0),
                      ["exit_long", "exit_tag"]] = (1, "regime_exit")
        return dataframe

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit,
                        after_fill, **kwargs) -> float:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return self.stoploss
        atr_value = float(df["atr"].iloc[-1])
        if atr_value <= 0 or pd.isna(atr_value):
            return self.stoploss
        return atr_stoploss_ratio(trade.open_rate, atr_value, self._params())
