#!/usr/bin/env bash
# One-command launcher for Mac/Linux. Run from the project folder:  ./start.sh
set -e
cd "$(dirname "$0")"

echo "==> Checking Docker..."
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker isn't installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running. Open Docker Desktop, wait for the whale icon, then run this again."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "==> Created .env. Open it and paste your BingX keys into:"
  echo "      FREQTRADE__EXCHANGE__KEY=..."
  echo "      FREQTRADE__EXCHANGE__SECRET=..."
  echo "    (Use FRESH keys: spot only, withdrawals off.) Then run ./start.sh again."
  exit 0
fi

echo "==> Starting everything (first time downloads/builds; be patient)..."
docker compose up -d

echo ""
echo "==> Done. Open the dashboard:  http://localhost:8050"
echo "    It starts in safe paper mode. Real trading stays OFF until you press TURN ON."
( command -v open >/dev/null && open http://localhost:8050 ) || \
( command -v xdg-open >/dev/null && xdg-open http://localhost:8050 ) || true
echo "    Stop everything later with:  docker compose down"
