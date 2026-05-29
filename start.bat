@echo off
REM One-command launcher for Windows. Double-click this file, or run: start.bat
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker isn't installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/
  pause & exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is installed but not running. Open Docker Desktop, wait for the whale icon, then run this again.
  pause & exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo ==^> Created .env. Open it in Notepad and paste your BingX keys into:
  echo       FREQTRADE__EXCHANGE__KEY=...
  echo       FREQTRADE__EXCHANGE__SECRET=...
  echo     ^(Use FRESH keys: spot only, withdrawals off.^) Then run start.bat again.
  pause & exit /b 0
)

echo ==^> Starting everything (first time downloads/builds; be patient)...
docker compose up -d

echo.
echo ==^> Done. Open the dashboard:  http://localhost:8050
echo     It starts in safe paper mode. Real trading stays OFF until you press TURN ON.
start "" http://localhost:8050
echo     Stop everything later with:  docker compose down
pause
