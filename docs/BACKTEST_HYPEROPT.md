# Honest tuning: walk-forward hyperopt

The strategy as written loses money on recent data. This is how to test —
*without fooling ourselves* — whether it can be tuned into a real edge.

> **The trap:** optimising parameters on a stretch of data will almost always
> "find" settings that look great *on that exact data* and then fail live. That
> is overfitting, and it's the #1 way bots lose money. The defence is
> **walk-forward**: optimise on an older window, then judge the result only on a
> newer window the optimiser never saw.

## What gets tuned
`MyStrategy` exposes these as optimizable: EMA fast/slow lengths, RSI entry
min/max, pullback %, exit RSI, ATR period, ATR stop multiplier. Hyperopt searches
combinations of these to minimise a **risk-adjusted loss**
(`user_data/hyperopts/RiskAdjustedLoss.py`) that rewards return + expectancy and
penalises drawdown and too-few-trades (so it can't win with lucky one-offs).

## Run it (on the server, where Freqtrade + data live)
One command does the optimise step and prints the validate steps:
```bash
./scripts/walk_forward.sh 20260304-20260506 20260506-20260529 300
#                          ^TRAIN (optimise)  ^TEST (validate)   ^epochs
```
Or manually:
```bash
# 1) Optimise on the TRAIN window only
docker compose run --rm freqtrade hyperopt \
  --config user_data/config.json --strategy MyStrategy \
  --hyperopt-loss RiskAdjustedLoss --timeframe 1h \
  --timerange 20260304-20260506 --epochs 300 --spaces buy sell -j -1

# 2) See the best parameters
docker compose run --rm freqtrade hyperopt-show --best --print-json

# 3) Save them to user_data/strategies/MyStrategy.json (freqtrade auto-loads it)

# 4) Validate on the TEST window the optimiser never saw
docker compose run --rm freqtrade backtesting \
  --config user_data/config.json --strategy MyStrategy \
  --timeframe 1h --timerange 20260506-20260529
```

## Accept / reject (be strict)
**Accept the tuned settings only if ALL hold on the TEST window:**
- Total return is **positive**.
- A **believable number of trades** (not 3 lucky ones).
- **Max drawdown within your limit** (≤ ~8–10%).
- Win rate / expectancy aren't wildly different from the TRAIN window
  (similar behaviour = real; very different = overfit).

**Reject if the TEST window loses** — that means the TRAIN result was curve-fit.
Don't ship it.

## Even a good result is not "go live"
A config that passes walk-forward has only earned the right to **forward
paper-trade**: run it in dry-run on live data for a couple of weeks and confirm
it behaves like the backtest. Only then consider real money, tiny size first.

## Honest expectation
Tuning a simple trend-pullback strategy may yield a marginal improvement, or it
may confirm there's no durable edge here — both are valid, useful answers. If
walk-forward keeps failing out-of-sample, the right move is a different strategy
idea, not more aggressive optimisation. Capital protection comes first; not
trading is a perfectly good outcome.
