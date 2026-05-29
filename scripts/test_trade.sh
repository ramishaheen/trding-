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
# No -f: we want the real response body + status even on a 4xx/5xx (e.g.
# "Failed to enter position"), not a generic connection error.
BODY=$(curl -sS -u "$U:$P" -H "Content-Type: application/json" \
  -d "{\"pair\":\"$PAIR\"}" -w "\n%{http_code}" \
  http://localhost:8080/api/v1/forceenter 2>&1)
CODE=$(printf '%s' "$BODY" | tail -n1)
echo "HTTP $CODE"
printf '%s\n' "$BODY" | sed '$d'   # body without the trailing status line
echo
case "$CODE" in
  200) echo "Entered. Refresh the dashboard — it should appear under 'Open trades'." ;;
  000) echo "No response. Is Freqtrade up?  docker compose ps  /  logs freqtrade" ;;
  *)   echo "Freqtrade rejected it (see body above). Common causes: force_entry_enable"
       echo "not active (restart freqtrade after deploy), max_open_trades reached, or"
       echo "the pair already has an open trade." ;;
esac
