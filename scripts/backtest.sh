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
# Download a WIDE range (default: well before the backtest start) so the backtest
# isn't silently clipped to the few weeks already cached. BingX may not have data
# all the way back — that's an exchange limit, not a bug; you get what's available.
DL="${DOWNLOAD_RANGE:-20250601-20260530}"
PAIRS="BTC/USDT ETH/USDT SOL/USDT"
DC="docker compose run --rm freqtrade"

echo "==> Downloading $TF data for $DL (this can take a while the first time) ..."
$DC download-data --timeframe "$TF" --timerange "$DL" --pairs $PAIRS || true
echo "    (If a WARNING says data only goes back to a recent date, that's how far"
echo "     BingX serves $TF history — the backtest will use the available window.)"

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
echo
echo "0 TRADES? Two causes: (1) a leftover user_data/strategies/MyStrategy.json"
echo "from hyperopt is overriding the code with a too-narrow filter — remove it:"
echo "     rm -f user_data/strategies/MyStrategy.json && docker compose restart freqtrade"
echo "(2) long-only strategy correctly sits out a downtrend — that can be right."
echo "================================================================"
