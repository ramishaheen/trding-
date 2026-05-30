"""Tests for the market-structure features fed to the LLM."""

import math

from market_data import ema, rsi, atr, compute_pair_features


def _series(n, start=100.0, step=0.0, noise=0.0):
    out = []
    price = start
    for i in range(n):
        price += step + noise * math.sin(i / 5)
        out.append([i, price, price * 1.002, price * 0.998, price, 1000])
    return out


def test_ema_tracks_upward_series():
    vals = [float(i) for i in range(1, 51)]
    assert ema(vals, 10) < vals[-1]            # EMA lags a rising series
    assert ema(vals, 10) > vals[0]


def test_rsi_high_on_uptrend_low_on_downtrend():
    up = [float(i) for i in range(1, 40)]
    down = [float(i) for i in range(40, 1, -1)]
    assert rsi(up) > 70
    assert rsi(down) < 30


def test_atr_positive_with_range():
    highs = [101, 102, 103, 104]; lows = [99, 100, 101, 102]; closes = [100, 101, 102, 103]
    assert atr(highs, lows, closes, length=2) > 0


def test_pair_features_uptrend_classification():
    candles = _series(210, start=100, step=0.3)   # steady uptrend
    feats = compute_pair_features({"1h": candles},
                                  ticker={"bid": 162.0, "ask": 162.1, "last": 162.05})
    tf = feats["per_timeframe"]["1h"]
    assert tf["trend"] == "up"
    assert tf["rsi"] is not None and tf["atr_pct"] is not None
    assert tf["dist_to_support_pct"] >= 0 and tf["dist_to_resistance_pct"] is not None
    assert feats["spread_pct"] is not None and feats["spread_pct"] >= 0
    assert feats["last_price"] == 162.05


def test_pair_features_downtrend_classification():
    candles = _series(210, start=200, step=-0.3)
    tf = compute_pair_features({"1h": candles})["per_timeframe"]["1h"]
    assert tf["trend"] == "down"


def test_pair_features_skips_thin_data():
    feats = compute_pair_features({"1h": _series(10)})   # too few candles
    assert feats["per_timeframe"] == {}


def test_multi_timeframe():
    feats = compute_pair_features({"5m": _series(210, step=0.2),
                                   "1h": _series(210, step=0.2),
                                   "4h": _series(210, step=0.2)})
    assert set(feats["per_timeframe"].keys()) == {"5m", "1h", "4h"}
