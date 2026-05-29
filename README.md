# Autonomous BingX Trading System (paper-first)

An autonomous crypto trading system built on **[Freqtrade](https://www.freqtrade.io/)**
for **BingX spot**, running in **dry-run (paper) mode by default**. An LLM
"research" sidecar writes a market-context signal to Postgres that the strategy
uses as a **soft gate**, and an **independent risk watchdog** can halt the bot
and flatten positions if hard limits are breached.

> ## ⚠️ Capital-loss disclaimer
> Trading cryptocurrency carries substantial risk. **You can lose all of your
> capital.** This software is provided as-is, with no warranty and no promise of
> profitability. It runs in **paper mode by default**; there is **no code path
> that flips it to live trading** — going live is a manual, documented,
> human-only step (see `docs/PHASE4_GO_LIVE_CHECKLIST.md`). Only ever risk money
> you are fully prepared to lose. Nothing here is financial advice.

---

## What's in the box

```
.
├── docker-compose.yml          # postgres + freqtrade + sidecar + watchdog
├── .env.example                # documented env vars (copy to .env; never commit .env)
├── user_data/
│   ├── config.json             # Freqtrade config (BingX spot, dry_run:true)
│   └── strategies/
│       ├── MyStrategy.py        # trend filter + pullback entry + ATR stop
│       └── strategy_logic.py    # pure, unit-tested decision rules + soft gate
├── research/
│   ├── sidecar.py              # scheduled LLM market-context service
│   ├── prompts.py              # hardened classifier prompt (news = untrusted)
│   └── schema.sql              # market_context table
├── risk/
│   ├── watchdog.py             # independent daily-loss / drawdown kill switch
│   └── risk_logic.py           # pure, unit-tested limit rules
├── execution/                  # OPTIONAL live browser-execution path (REAL MONEY)
│   ├── execution_logic.py      # pure, unit-tested gate + decision validation
│   ├── bridge.py               # FastAPI: Freqtrade webhook -> gate -> order queue
│   ├── browser_agent.py        # Playwright/Chromium subagent placing live orders
│   ├── live_watchdog.py        # independent live-account halt + flatten
│   ├── selectors.py            # centralized BingX UI selectors (verify before use)
│   └── store.py                # Postgres order queue + kill switch + snapshot
├── dashboard/                  # FreqUI for Phases 0–2; optional panel for 3+
├── docs/PHASE4_GO_LIVE_CHECKLIST.md
├── docs/BROWSER_EXECUTION.md   # live browser path operator guide
└── tests/                      # strategy, watchdog, sidecar, execution tests
```

## Two execution modes

| Mode | What it does | Money | Default |
|------|--------------|-------|---------|
| **Freqtrade dry-run** | Paper trades on live BingX data; full Freqtrade safety stack | Paper | ✅ on |
| **Live browser execution** | A Playwright subagent mirrors decisions onto the live BingX web UI | **REAL** | ⛔ off (opt-in) |

The live browser path is **off by default** and gated behind both the
`LIVE_BROWSER_TRADING_ENABLED` flag and the `live-browser` compose profile.
Because browser-placed orders are invisible to the Freqtrade watchdog, it ships
with its **own** independent gate (pair allowlist, max positions, daily-loss
cap, per-trade stake clamp, fail-closed kill switch) and a **live account
watchdog** that trips the kill switch and flattens on a breach. **Read
`docs/BROWSER_EXECUTION.md` before enabling it — real money is at risk.**

## Design guarantees

1. **No secrets in the repo.** Everything sensitive lives in `.env`
   (git-ignored). The code reads from the environment.
2. **The LLM is *not* in the order path.** The sidecar only writes a
   `market_context` row. The strategy uses it as a **soft gate** that can *block
   or shrink* entries — it can never open or force a trade.
3. **Untrusted news.** All fetched headlines are wrapped as untrusted data; the
   classifier prompt forbids following any instruction embedded in them and
   outputs JSON only. Output is range-checked before storage.
4. **Independent hard risk limits.** The watchdog runs as its own process, so a
   strategy bug cannot bypass it. On breach it calls Freqtrade `/stop`,
   optionally `/forceexit all`, and alerts via Telegram.
5. **Spot only, no leverage. `dry_run` stays `true`** until a human follows the
   Phase 4 checklist.

## Tech stack

Python 3.11+, Freqtrade (official Docker image), CCXT (via Freqtrade),
pandas + pandas-ta, Anthropic Python SDK (model `claude-opus-4-8`),
PostgreSQL/TimescaleDB, Docker Compose. React/Tailwind for the optional Phase 3+
panel.

> Freqtrade's API and exchange support change over time. Check the current
> [Freqtrade docs](https://www.freqtrade.io/en/stable/) and the BingX
> exchange notes at build time rather than relying on memory.

## Quick start (Phase 0 — paper)

```bash
# 1. Configure
cp .env.example .env
#   edit .env: set Freqtrade API user/pass + JWT/WS secrets, Postgres password,
#   ANTHROPIC_API_KEY (for the sidecar), and optionally BingX read keys + Telegram.

# 2. Bring up Postgres + Freqtrade + sidecar + watchdog
docker compose up -d postgres
docker compose up -d freqtrade        # serves FreqUI on http://localhost:8080

# 3. Download historical data for backtesting (inside the freqtrade container)
docker compose run --rm freqtrade download-data \
  --config user_data/config.json --timerange 20240101- \
  --timeframes 1h 4h --pairs BTC/USDT ETH/USDT SOL/USDT

# 4. Start the research sidecar + risk watchdog
docker compose up -d sidecar watchdog
```

Open **http://localhost:8080** for FreqUI (positions, equity, trades, logs, and
a stop control). This is the dashboard for Phases 0–2.

## Backtesting & validation (Phase 1)

Backtest with realistic fees (BingX spot taker ≈ 0.1%) and slippage:

```bash
docker compose run --rm freqtrade backtesting \
  --config user_data/config.json --strategy MyStrategy \
  --timeframe 1h --timerange 20240101-20240601 \
  --fee 0.001 --enable-protections
```

Walk-forward with separate in-sample / out-of-sample windows, then hyperopt
**only** on the in-sample window and evaluate on the held-out window:

```bash
# in-sample hyperopt
docker compose run --rm freqtrade hyperopt \
  --config user_data/config.json --strategy MyStrategy \
  --hyperopt-loss SharpeHyperOptLoss --spaces buy sell \
  --timerange 20240101-20240401 --epochs 100 --fee 0.001

# out-of-sample evaluation with the tuned params
docker compose run --rm freqtrade backtesting \
  --config user_data/config.json --strategy MyStrategy \
  --timerange 20240401-20240601 --fee 0.001 --enable-protections
```

Report **max drawdown, win rate, profit factor, and number of trades** — not
just total return. If out-of-sample results are far worse than in-sample,
**flag overfitting** and re-think before trusting the numbers.

## Risk watchdog

The watchdog (`risk/watchdog.py`) polls the Freqtrade REST API every
`WATCHDOG_INTERVAL_SECONDS`, tracks daily P&L and drawdown from peak equity, and
**halts** the bot when either cap is hit:

- `DAILY_MAX_LOSS_PCT` (default 5% of `TOTAL_CAPITAL_USDT`)
- `MAX_DRAWDOWN_PCT` (default 10%)

On breach it calls `/stop`, optionally `/forceexit all`
(`WATCHDOG_FLATTEN_ON_BREACH=true`), and sends a Telegram alert. It **latches**
in the halted state — a human must restart the bot.

## Tests

The decision rules and risk limits are isolated in dependency-free modules so
they run anywhere:

```bash
pip install -r requirements.txt        # or just: pip install pytest requests
pytest -q
```

These cover the entry/exit signals, the ATR stop math, the LLM soft gate
(including that it can never force a trade), each risk-limit breach, the
watchdog halt/flatten path, and the sidecar's output validation /
prompt-injection hardening.

## Build phases

| Phase | Goal | Accept when |
|------|------|-------------|
| 0 | Env + read-only paper | Live data flows, FreqUI up, no trading logic relied on |
| 1 | Strategy + backtest | Reproducible backtest, out-of-sample evaluated, no look-ahead |
| 2 | Dry-run live (2–4 wks) | Runs unattended; every limit + kill switch demonstrably works |
| 3 | LLM sidecar + panel | Regime updates on schedule; strategy respects `risk_off` gate |
| 4 | Tiny live (manual) | Human follows `docs/PHASE4_GO_LIVE_CHECKLIST.md`; no auto-live path |

## Configuration knobs

See `.env.example` for the full list. Key risk knobs (paper defaults):

| Var | Default | Meaning |
|-----|---------|---------|
| `TOTAL_CAPITAL_USDT` | 1000 | Paper capital (`dry_run_wallet`) |
| `PER_TRADE_STAKE_USDT` | 100 | Per-trade stake (`stake_amount`) |
| `MAX_OPEN_TRADES` | 3 | Concurrent positions |
| `DAILY_MAX_LOSS_PCT` | 5 | Watchdog daily-loss halt |
| `MAX_DRAWDOWN_PCT` | 10 | Watchdog drawdown halt |
| `SIDECAR_INTERVAL_SECONDS` | 120 | LLM research cadence |

> These defaults were chosen so the system is runnable out-of-the-box in paper
> mode. Adjust capital and caps to your own risk tolerance before any live use.
