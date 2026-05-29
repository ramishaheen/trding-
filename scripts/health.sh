#!/usr/bin/env bash
# Quick "is the bot alive and scanning?" check. Run on the VPS:  ./scripts/health.sh
cd "$(dirname "$0")/.."

echo "================ SERVICES ================"
docker compose ps
echo

echo "========== FREQTRADE (loop alive? signals?) =========="
docker compose logs --tail=400 freqtrade 2>/dev/null \
  | grep -iE "heartbeat|entry signal|exit signal|searching|dry.?run|exception|error" | tail -12 \
  || echo "(no matching log lines yet — give it a minute after startup)"
echo

echo "========== AI MARKET READ (sidecar) =========="
docker compose logs --tail=200 sidecar 2>/dev/null | grep -i "market_context" | tail -3 \
  || echo "(no AI context yet — is ANTHROPIC_API_KEY set and the sidecar running?)"
echo

echo "========== FREQTRADE API (open trades / status) =========="
U=$(grep -E '^FREQTRADE__API_SERVER__USERNAME=' .env 2>/dev/null | cut -d= -f2-); U=${U:-freqtrader}
P=$(grep -E '^FREQTRADE__API_SERVER__PASSWORD=' .env 2>/dev/null | cut -d= -f2-); P=${P:-change_me_please}
if curl -fsS -u "$U:$P" http://localhost:8080/api/v1/count 2>/dev/null; then
  echo
  echo "open positions:"
  curl -fsS -u "$U:$P" http://localhost:8080/api/v1/status 2>/dev/null | head -c 600; echo
else
  echo "(Freqtrade API not reachable on localhost:8080 — check 'docker compose ps' and the api_server password)"
fi
echo

echo "Tips: a healthy bot with no trades is normal — it only acts in clean uptrends."
echo "Live tail:  docker compose logs -f freqtrade"
