# Live Browser Execution (REAL MONEY) — operator guide

> ## 🛑 Read this first
> This path makes the system place **real orders with real money** on the live
> BingX website by driving a Chrome browser. Unlike the rest of this project, it
> is **not paper trading**. The Freqtrade risk watchdog **cannot see or stop**
> browser-placed orders — the safety here is the independent gate
> (`execution/execution_logic.py`) and the live account watchdog
> (`execution/live_watchdog.py`). You can lose all of your capital. There is no
> warranty and no promise of profitability. Only ever risk money you are fully
> prepared to lose.

## How it works

```
 Freqtrade (dry_run:true)            decision brain — paper accounting
        │  webhook (entry_fill / exit_fill)
        ▼
 execution-bridge  ──▶ independent pre-trade gate (kill switch, allowlist,
        │                max positions, daily-loss cap, per-trade stake clamp)
        │  approved order
        ▼
 execution_orders  (Postgres queue)
        ▼
 executor  ──▶ API order (ccxt, PRIMARY)  ──▶  LIVE BingX  ──▶  REAL ORDER
        │         └─ on failure ─▶ browser subagent (Playwright fallback) ─▶ same
        ▲
 live-watchdog ─▶ reads real account via API, trips kill switch + flattens on breach
```

Order path is configurable via `ORDER_PATH` (`api` | `browser` | `both`,
default `both` = API first, browser fallback). **Account reads always use the
API.** Freqtrade stays in **dry-run** as the decision engine — its config is
never flipped to live. Its dry-run fills are forwarded as *decisions* that the
executor mirrors onto the live account. **Real money is at risk regardless of
Freqtrade's dry-run setting.**

## Safety design (fail-closed)

- **Master switch:** nothing runs unless `LIVE_BROWSER_TRADING_ENABLED=on`
  **and** you start the `live-browser` compose profile. Default: off.
- **Kill switch:** a Postgres flag (`system_flags.kill_switch`). Re-checked
  before every order. Tripped/unknown/DB-unreachable ⇒ no new orders.
  `POST /stop` on the bridge trips it; `POST /resume` clears it (deliberate
  operator action).
- **Independent gate** (`check_order`): pair allowlist, max open positions,
  daily-loss cap, per-trade stake clamp (clamps **down**, never up). Entries are
  denied if the real account state can't be read; **exits are never blocked by a
  cap** so flattening always works.
- **Live watchdog:** reads the real account every cycle, and on a daily-loss or
  drawdown breach it trips the kill switch, enqueues market exits for all open
  positions, and alerts Telegram.
- **Deterministic clicks only:** the browser agent uses centralized selectors
  (`execution/selectors.py`) and refuses to act unless it can locate a control
  *uniquely*. It never improvises clicks. Every attempt is screenshotted to the
  audit directory.

## ⚠️ You MUST verify selectors before live use

`execution/selectors.py` ships with best-guess selectors. The BingX UI changes,
so before any live order you must confirm each selector against the current site
(DevTools → inspect element → prefer stable `data-testid` attributes). The agent
will abort an order rather than click the wrong thing, but wrong selectors mean
nothing gets placed.

## One-time setup

1. **Put your API keys in `.env`** (git-ignored): `FREQTRADE__EXCHANGE__KEY` and
   `FREQTRADE__EXCHANGE__SECRET`. These are used for account reads and the
   primary (API) order path. Restrict the key: **spot only, withdrawals
   disabled, IP-allowlisted.**
2. **Choose the order path** via `ORDER_PATH` (`api` | `browser` | `both`).
   `both` (default) tries the API first and falls back to the browser subagent.
3. **Fund a dedicated BingX sub-account** with only what you can lose.
4. **Tighten limits** in `.env`: `TOTAL_CAPITAL_USDT`, `PER_TRADE_STAKE_USDT`,
   `MAX_OPEN_TRADES`, `DAILY_MAX_LOSS_PCT`, `MAX_DRAWDOWN_PCT`,
   `LIVE_PAIR_ALLOWLIST`.
5. **(Only if using the browser fallback) Verify selectors** in
   `execution/selectors.py` against the live site, then **log in once, by hand**,
   into the automation browser profile (handles 2FA):
   ```bash
   # headful login — opens a real Chrome window using the persistent profile
   docker compose run --rm -e BROWSER_HEADLESS=false \
     -e LIVE_BROWSER_TRADING_ENABLED=on \
     executor python -c "from playwright.sync_api import sync_playwright; \
import os; pw=sync_playwright().start(); \
ctx=pw.chromium.launch_persistent_context(os.environ['BROWSER_PROFILE_DIR'], headless=False); \
ctx.new_page().goto('https://bingx.com/en/login/'); input('Log in + 2FA, then press Enter...')"
   ```
   The session persists in the `browser_data` volume; the repo never stores
   your password. (Pure-API mode, `ORDER_PATH=api`, needs no browser login.)

## Going live

1. Set `LIVE_BROWSER_TRADING_ENABLED=on` in `.env`.
2. Set `"enabled": true` in the `webhook` block of `user_data/config.json`
   (this only forwards decisions; Freqtrade stays dry-run).
3. Start the gated services:
   ```bash
   docker compose --profile live-browser up -d execution-bridge executor live-watchdog
   docker compose restart freqtrade   # pick up the webhook config
   ```
4. **Watch the first trades.** Tail the logs, watch the audit screenshots in the
   `browser_data` volume (browser fallback only), and keep the kill switch handy:
   ```bash
   curl -X POST http://localhost:8090/stop   # only if you publish the port; otherwise exec into the container
   ```

## Stopping / rolling back

- **Halt now:** `POST /stop` on the bridge (trips kill switch; watchdog flattens).
- **Disable the path:** set `LIVE_BROWSER_TRADING_ENABLED=false`, set the webhook
  back to `"enabled": false`, and `docker compose stop execution-bridge
  executor live-watchdog`.

## Tests

The gate, decision parsing, kill-switch semantics, caps, and the API helpers
(account summarisation + order shapes via a fake client) are unit-tested in
`tests/test_execution.py` and `tests/test_bingx_api.py` (`pytest -q`). The
browser-driving code is integration-level and must be validated manually against
the live site (in a tiny-stake dry run) before you trust it.
