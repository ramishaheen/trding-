"""Real market-structure features for the LLM (the 'gather more information'
upgrade). Instead of only news headlines, the sidecar now gives Claude grounded,
multi-timeframe price structure: trend, volatility regime, support/resistance
distance, RSI, and bid/ask spread per coin.

`ema`/`rsi`/`atr`/`compute_pair_features` are PURE and unit-tested. The ccxt
fetch is best-effort and fails open (returns {} so the sidecar still runs on
headlines if market data is unavailable).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("market_data")


# --- pure indicator helpers ------------------------------------------------
def ema(vals: list[float], length: int) -> float:
    if not vals:
        return 0.0
    k = 2 / (length + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], length: int = 14) -> float:
    if len(closes) < length + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains[:length]) / length
    al = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        ag = (ag * (length - 1) + gains[i]) / length
        al = (al * (length - 1) + losses[i]) / length
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(highs: list[float], lows: list[float], closes: list[float], length: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < length:
        return sum(trs) / max(1, len(trs))
    a = sum(trs[:length]) / length
    for i in range(length, len(trs)):
        a = (a * (length - 1) + trs[i]) / length
    return a


def _round(v, n=4):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None


def compute_pair_features(ohlcv_by_tf: dict, ticker: Optional[dict] = None,
                          lookback: int = 50) -> dict:
    """Per-timeframe structure from OHLCV (list of [ts,o,h,l,c,v]) + ticker."""
    out: dict = {"per_timeframe": {}}
    last_price = None
    for tf, candles in (ohlcv_by_tf or {}).items():
        if not candles or len(candles) < 30:
            continue
        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        close = closes[-1]
        last_price = close
        ema50 = ema(closes, 50)
        ema200 = ema(closes, 200) if len(closes) >= 200 else ema(closes, min(len(closes), 100))
        if close > ema50 > ema200:
            trend = "up"
        elif close < ema50 < ema200:
            trend = "down"
        else:
            trend = "sideways"
        atr_v = atr(highs, lows, closes)
        recent_high = max(highs[-lookback:])
        recent_low = min(lows[-lookback:])
        out["per_timeframe"][tf] = {
            "trend": trend,
            "rsi": _round(rsi(closes), 1),
            "atr_pct": _round((atr_v / close * 100) if close else 0, 3),
            "dist_to_resistance_pct": _round((recent_high - close) / close * 100 if close else None, 2),
            "dist_to_support_pct": _round((close - recent_low) / close * 100 if close else None, 2),
        }
    if ticker:
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if bid and ask:
            mid = (bid + ask) / 2
            out["spread_pct"] = _round((ask - bid) / mid * 100 if mid else None, 4)
        last_price = ticker.get("last") or last_price
    out["last_price"] = _round(last_price, 6)
    return out


# --- best-effort live fetch (ccxt) -----------------------------------------
def build_market_data(pairs: list[str], timeframes: Optional[list[str]] = None) -> dict:
    """Fetch OHLCV + ticker per pair from BingX (public) and compute features.
    Fails open: returns {} on any error so the sidecar still runs on headlines."""
    timeframes = timeframes or ["5m", "1h", "4h"]
    try:
        import ccxt
    except Exception as exc:  # noqa: BLE001
        logger.info("ccxt unavailable; market data skipped: %s", exc)
        return {}
    try:
        ex = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("ccxt bingx init failed: %s", exc)
        return {}

    data: dict = {}
    for pair in pairs:
        ohlcv_by_tf = {}
        for tf in timeframes:
            try:
                ohlcv_by_tf[tf] = ex.fetch_ohlcv(pair, timeframe=tf, limit=210)
            except Exception as exc:  # noqa: BLE001
                logger.info("ohlcv %s %s failed: %s", pair, tf, exc)
        ticker = None
        try:
            ticker = ex.fetch_ticker(pair)
        except Exception:  # noqa: BLE001
            pass
        if ohlcv_by_tf:
            data[pair] = compute_pair_features(ohlcv_by_tf, ticker)
    return data
