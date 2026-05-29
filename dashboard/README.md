# Dashboard

**Phases 0–2: use FreqUI.** It ships with Freqtrade and is served by the
`freqtrade` container on http://localhost:8080 (login with
`FREQTRADE_USERNAME` / `FREQTRADE_PASSWORD` from your `.env`). It already shows
positions, the equity curve, recent trades, logs, and a stop control — this is
"the page you open" for the first phases.

**Phase 3+ (optional): thin custom panel.** A dark/glassmorphism React + Tailwind
panel that reads the Freqtrade REST API (`/api/v1/...`) and Postgres
(`market_context`) and surfaces:

- open positions + equity curve + recent trades (from the Freqtrade REST API)
- the current LLM regime / risk_state / confidence (from `market_context`)
- a prominent **STOP** button wired to `POST /api/v1/stop`

This panel is intentionally not built yet — FreqUI covers Phases 0–2. When you
reach Phase 3 and want the custom panel, scaffold it here (e.g. Vite + React +
Tailwind) and point it at:

- `FREQTRADE_API_URL` for trades/positions/equity and the STOP action
- `DATABASE_URL` (or a tiny FastAPI read proxy) for the latest `market_context`

Keep the STOP button calling the same `/stop` endpoint the risk watchdog uses,
so there is a single, well-understood kill switch.
