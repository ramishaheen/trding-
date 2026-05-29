# Turning on REAL-money trading (and turning it off)

> ⚠️ Real money can be lost. This bot is **unproven**. Use money you can afford
> to lose, start tiny, and paper-test first. Turning it on is your decision.

Three independent switches must all be set before a single real order is placed
(and the emergency STOP overrides all of them):

| # | Switch | Where | Who sets it | Purpose |
|---|--------|-------|-------------|---------|
| 1 | `LIVE_BROWSER_TRADING_ENABLED=on` | `.env` | you, once at setup | deploy-time master enable |
| 2 | `trading_mode = REAL_TRADING_STRICT` | `risk_config.json` (default) | already set | governor mode |
| 3 | **ARM** (the ON/OFF button) | dashboard / API | you, day-to-day | start/stop placing real orders |

If any one is off, the bot still watches and shows its decisions, but **places no
real orders** (they show as "observed"). On top of that, every trade must pass
the Risk Governor, and the kill switch beats everything.

## Before you arm — checklist
1. **Fresh BingX API key** in `.env` (spot only, withdrawals OFF, IP-allowlisted).
   Never reuse a key you've shared anywhere.
2. **Fund a small amount** you can afford to lose.
3. **Tighten limits** in `risk_config.json` (`max_risk_per_trade_percent`,
   `max_daily_loss_percent`, `max_total_drawdown_percent`, `max_capital_exposure_percent`).
4. Set `LIVE_BROWSER_TRADING_ENABLED=on` in `.env`.
5. Start the live services:
   ```bash
   docker compose --profile live-browser up -d execution-bridge executor live-watchdog
   docker compose up -d dashboard
   docker compose restart freqtrade
   ```

## Turn real trading ON / OFF

**Easiest — the dashboard** (`http://localhost:8050`):
- The top bar shows **Real trading: ON/OFF**.
- Click **▶ TURN ON**, type `ARM` to confirm. Click **⏸ TURN OFF** anytime.
- The big red **■ STOP NOW** is the emergency brake: it halts everything and
  requires a manual restart.

**Or from the command line:**
```bash
# arm
docker compose exec execution-bridge python -c "from store import set_live_enabled; set_live_enabled(True,'cli')"
# disarm
docker compose exec execution-bridge python -c "from store import set_live_enabled; set_live_enabled(False,'cli')"
```

## What "ARMED" actually does
When armed, an entry that **passes the Weekly Target Manager and the Risk
Governor** is placed as a real order on BingX (API first, browser fallback),
sized by the governor. When disarmed, the same decision is logged as "observed"
and **no order is placed**. Exits (closing positions) always go through so you
can flatten.

## After an emergency STOP / kill switch
The kill switch latches. Fix the cause, then restart deliberately — see
`docs/RISK_GOVERNOR.md` ("Manual restart"). Nothing resumes on its own.

## Reality check
- The strategy must be producing signals (Freqtrade running) for anything to
  happen. With no qualifying setups, the safe outcome is **no trades** — that is
  normal, not a bug.
- "Weekly target not achievable under current risk limits" is a **success**
  message: the bot refused to take unsafe risks to chase a number.
