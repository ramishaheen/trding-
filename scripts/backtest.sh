#!/usr/bin/env bash
# FAST validation — replay the strategy over historical data (months of 5m
# candles in seconds). Far more signal than days of live paper.
#
#   ./scripts/backtest.sh [TIMERANGE] [TIMEFRAME]
#   ./scripts/backtest.sh 20260101-20260530 5m
set -e
cd "$(dirname "$0")/.."

TR="${1:-20260101-20260530}"
TF="${2:-5m}"
PAIRS="BTC/USDT ETH/USDT SOL/USDT"
DC="docker compose run --rm freqtrade"

echo "==> Downloading $TF data for $TR (skips what's already cached) ..."
$DC download-data --timeframe "$TF" --timerange "$TR" --pairs $PAIRS || true

echo
echo "==> Backtesting MyStrategy on $TF over $TR (fees included) ..."
$DC backtesting --config user_data/config.json --strategy MyStrategy \
    --timeframe "$TF" --timerange "$TR" --export trades

echo
echo "==> Saving the summary to the dashboard ..."
$DC python /freqtrade/user_data/backtest_report.py || \
  echo "(could not store summary — dashboard will just keep the live numbers)"

echo
echo "================================================================"
echo "READ THE SUMMARY TABLE:"
echo "  - Tot Profit %      -> is it NET positive after fees?"
echo "  - Profit factor     -> > 1 means winners outweigh losers"
echo "  - Win%  /  # Trades -> need a believable number of trades"
echo "  - Max Drawdown      -> within your 8% limit?"
echo
echo "Positive total + profit factor > 1 over many trades = worth a"
echo "walk-forward test (./scripts/walk_forward.sh) before ANY real money."
echo "If it's negative, frequent scalping is churning fees -> we tune."
echo "================================================================"
