#!/usr/bin/env bash
# Open a PAPER test trade right now so you can watch the whole pipeline work
# (entry -> open trade -> dashboard -> the bot manages & exits it).
# Safe: the bot is in dry_run, so this spends no real money.
#
#   ./scripts/test_trade.sh           # forces a BTC/USDT paper entry
#   ./scripts/test_trade.sh ETH/USDT  # or another allowlisted pair
cd "$(dirname "$0")/.."

PAIR="${1:-BTC/USDT}"
U=$(grep -E '^FREQTRADE__API_SERVER__USERNAME=' .env 2>/dev/null | cut -d= -f2-); U=${U:-freqtrader}
P=$(grep -E '^FREQTRADE__API_SERVER__PASSWORD=' .env 2>/dev/null | cut -d= -f2-); P=${P:-change_me_please}

echo "Forcing a PAPER entry on $PAIR ..."
RESP=$(curl -fsS -u "$U:$P" -H "Content-Type: application/json" \
  -d "{\"pair\":\"$PAIR\"}" http://localhost:8080/api/v1/forceenter 2>/dev/null)
if [ -z "$RESP" ]; then
  echo "Could not reach Freqtrade API. Check: docker compose ps  (is freqtrade up?)"
  echo "and that force_entry_enable:true is in config.json (then: docker compose restart freqtrade)."
  exit 1
fi
echo "$RESP"
echo
echo "Now refresh the dashboard — you should see it under 'Open trades'."
echo "The bot will manage and exit it per the strategy (ROI / stop / signal)."
