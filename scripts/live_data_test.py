#!/usr/bin/env python3
"""Quick LIVE-DATA test — no Docker, no real money, read-only.

Fetches recent candles for the allowlisted pairs from BingX (public data, no API
key), runs the real strategy rules + Weekly Target Manager + Risk Governor, and
prints what the system WOULD do right now.

Usage (from the repo root):
    pip install ccxt
    python scripts/live_data_test.py            # live BingX data
    python scripts/live_data_test.py --demo      # synthetic data (no network)

Nothing here can place an order. It only reads prices and shows decisions.
"""

from __future__ import annotations

import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "user_data", "strategies"))

from strategy_logic import StrategyParams, build_entry_signal  # noqa: E402
from risk_governor import RiskGovernor, load_config  # noqa: E402
from risk_governor.models import AccountSnapshot, MarketSnapshot, TradeSignal  # noqa: E402
from weekly_target_manager import WeeklyTargetManager, load_weekly_config  # noqa: E402
from trade_pipeline import evaluate_trade  # noqa: E402

PAIRS = [p.strip() for p in os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT").split(",")]
ACCOUNT_USDT = float(os.environ.get("TOTAL_CAPITAL_USDT", "100"))


# --- tiny indicator helpers (no pandas needed) -----------------------------
def ema(vals, length):
    k = 2 / (length + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, length=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    if len(gains) < length:
        return 50.0
    ag = sum(gains[:length]) / length
    al = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        ag = (ag * (length - 1) + gains[i]) / length
        al = (al * (length - 1) + losses[i]) / length
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr(highs, lows, closes, length=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < length:
        return sum(trs) / max(1, len(trs))
    a = sum(trs[:length]) / length
    for i in range(length, len(trs)):
        a = (a * (length - 1) + trs[i]) / length
    return a


def fetch_ohlcv(pair):
    import ccxt
    ex = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    candles = ex.fetch_ohlcv(pair, timeframe="1h", limit=240)
    ticker = ex.fetch_ticker(pair)
    return candles, ticker


def demo_ohlcv(pair, scenario):
    """Build 240 synthetic 1h candles for a named scenario so the test shows a
    realistic mix of outcomes."""
    base = {"BTC/USDT": 60000, "ETH/USDT": 3000, "SOL/USDT": 150}.get(pair, 100)
    price, candles = base, []
    for i in range(240):
        if scenario == "downtrend":
            step = -0.0010
        elif scenario == "overbought":
            step = 0.0018
        else:  # "buy": strong uptrend, then a gentle recent pullback toward the fast EMA
            step = 0.0016 if i < 224 else -0.0016
        price *= (1 + step + 0.0015 * math.sin(i / 6))
        hi, lo = price * 1.003, price * 0.997
        candles.append([i, price * 0.999, hi, lo, price, 1000])
    last = candles[-1][4]
    return candles, {"bid": last * 0.9999, "ask": last * 1.0001, "last": last,
                     "timestamp": time.time() * 1000}


def analyse(pair, candles, ticker, p: StrategyParams):
    closes = [c[4] for c in candles]; highs = [c[2] for c in candles]; lows = [c[3] for c in candles]
    ema_f = ema(closes, p.ema_fast)[-1]
    ema_s = ema(closes, p.ema_slow)[-1]
    ema_t = ema(closes, p.ema_trend)[-1]
    r = rsi(closes, p.rsi_period)
    a = atr(highs, lows, closes, p.atr_period)
    a_avg = atr(highs, lows, closes, p.atr_period)  # simple proxy
    close = closes[-1]
    dist_fast = (close - ema_f) / ema_f
    uptrend = close > ema_t and ema_s > ema_t
    pullback = abs(dist_fast) <= p.pullback_pct
    rsi_ok = p.rsi_entry_min <= r <= p.rsi_entry_max
    signal = uptrend and pullback and rsi_ok
    return {"close": close, "rsi": r, "atr": a, "atr_avg": a_avg, "uptrend": uptrend,
            "pullback": pullback, "rsi_ok": rsi_ok, "signal": signal, "ticker": ticker}


def main(argv):
    demo = "--demo" in argv
    p = StrategyParams()
    gov = RiskGovernor(config=load_config())
    wtm = WeeklyTargetManager(config=load_weekly_config())
    now = time.time()
    wtm.update(equity=ACCOUNT_USDT, balance=ACCOUNT_USDT, now_ts=now)

    print("=" * 70)
    print(f"LIVE-DATA TEST  ({'SYNTHETIC demo data' if demo else 'live BingX data'})  "
          f"account=${ACCOUNT_USDT:.0f}")
    print("=" * 70)

    scenarios = {"BTC/USDT": "overbought", "ETH/USDT": "downtrend", "SOL/USDT": "buy"}
    for pair in PAIRS:
        try:
            candles, ticker = demo_ohlcv(pair, scenarios.get(pair, "buy")) if demo else fetch_ohlcv(pair)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{pair}: could not fetch data ({exc})"); continue

        a = analyse(pair, candles, ticker, p)
        print(f"\n{pair}  price={a['close']:.2f}  rsi={a['rsi']:.0f}  atr={a['atr']:.2f}")
        print(f"   trend_up={a['uptrend']}  pullback={a['pullback']}  rsi_in_range={a['rsi_ok']}"
              f"  -> strategy signal: {'YES' if a['signal'] else 'no'}")
        if not a["signal"]:
            print("   => no qualifying setup right now (the safe default is to wait).")
            continue

        raw = build_entry_signal(pair, a["close"], a["atr"], a["atr_avg"], p, now)
        suggested_qty = round((ACCOUNT_USDT * 0.1) / a["close"], 8)  # governor resizes this
        sig = TradeSignal(symbol=raw["pair"], side="long", entry_price=raw["entry_price"],
                          stop_loss_price=raw["stop_loss_price"], take_profit_price=raw["take_profit_price"],
                          quantity=suggested_qty, leverage=1, margin_mode="spot",
                          max_holding_time_minutes=raw["max_holding_time_minutes"],
                          strategy_reason=raw["strategy_reason"], timestamp=now,
                          signal_id=raw["signal_id"], quality_components=raw["quality_components"])
        t = a["ticker"]
        market = MarketSnapshot(known=True, bid=t.get("bid"), ask=t.get("ask"),
                                last_price=t.get("last") or a["close"],
                                price_timestamp=(t.get("timestamp") or now * 1000) / 1000.0,
                                atr=a["atr"], atr_avg_20=a["atr_avg"],
                                orderbook_depth_quote=1e6, estimated_slippage_percent=0.02)
        acct = AccountSnapshot(known=True, balance=ACCOUNT_USDT, equity=ACCOUNT_USDT,
                               available_margin=ACCOUNT_USDT, open_positions=0, open_orders=0,
                               open_symbols=(), margin_mode_confirmed=True, leverage_confirmed=True)
        res = evaluate_trade(wtm, gov, sig, acct, market, now)
        if res.approved:
            print(f"   => APPROVED: buy ~${res.quantity * a['close']:.2f}  "
                  f"stop={sig.stop_loss_price:.2f}  target={sig.take_profit_price:.2f}  "
                  f"(R:R {res.governor_result.risk_reward_ratio:.1f}, quality {res.governor_result.trade_quality_score:.0f})")
            print("      (Would place a REAL order only if you have ARMED live trading.)")
        else:
            print(f"   => BLOCKED by safety system: {res.reason}")

    d = wtm.dashboard(now)
    print("\n" + "-" * 70)
    print(f"Weekly goal: ${d['weekly_start_balance']:.0f} -> ${d['weekly_target_balance']:.0f}  "
          f"status: {d['target_status']}  (needs {d['required_daily_return_percent']:.0f}%/day)")
    print("Real orders are OFF unless you arm them. This test never trades.")
    print("-" * 70)


if __name__ == "__main__":
    main(sys.argv[1:])
