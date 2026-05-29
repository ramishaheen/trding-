#!/usr/bin/env bash
# Walk-forward tuning: optimise on a TRAIN window, then you validate on a TEST
# window the optimiser never saw. Run on the server (where Freqtrade + data are).
#
#   ./scripts/walk_forward.sh [TRAIN_RANGE] [TEST_RANGE] [EPOCHS]
#
# Defaults split the last ~90 days ~70/30 (train older, test newer).
set -e
cd "$(dirname "$0")/.."

TRAIN="${1:-20260304-20260506}"   # in-sample (optimise here)
TEST="${2:-20260506-20260529}"    # out-of-sample (validate here — never optimised)
EPOCHS="${3:-300}"
DC="docker compose run --rm freqtrade"

echo "=================================================================="
echo " STEP 1/2  Hyperopt on TRAIN window $TRAIN  ($EPOCHS epochs)"
echo "=================================================================="
$DC hyperopt \
  --config user_data/config.json \
  --strategy MyStrategy \
  --hyperopt-loss RiskAdjustedLoss \
  --timeframe 1h \
  --timerange "$TRAIN" \
  --epochs "$EPOCHS" \
  --spaces buy sell \
  -j -1

echo ""
echo "=================================================================="
echo " STEP 2/2  Validate on TEST window $TEST (data NOT used in tuning)"
echo "=================================================================="
echo "1) See the best params:"
echo "     docker compose run --rm freqtrade hyperopt-show --best --print-json"
echo "2) Save those params to user_data/strategies/MyStrategy.json"
echo "   (freqtrade auto-loads that file and it overrides the defaults)."
echo "3) Backtest the TEST window with the tuned params:"
echo "     docker compose run --rm freqtrade backtesting --config user_data/config.json \\"
echo "        --strategy MyStrategy --timeframe 1h --timerange $TEST"
echo ""
echo "ACCEPT the tuning ONLY if the TEST window is profitable, has a sane number"
echo "of trades, and drawdown stays within your limit. If TEST loses, the TRAIN"
echo "result was overfit — discard it. Even a good result must then survive"
echo "forward paper-trading before any real money."
