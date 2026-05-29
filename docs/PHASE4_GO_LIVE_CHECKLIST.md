# Phase 4 — Tiny Live Checklist (MANUAL, HUMAN-ONLY)

> ⚠️ **There is no code path that flips this system to live trading.**
> Going live is a deliberate, manual edit performed by the operator, only after
> Phases 2–3 have been stable. This document is the checklist. Nothing here is
> automated, and nothing should be.

Do **not** start this until **all** of the following are true:

- [ ] Phase 2 ran unattended in dry-run for **at least 2–4 weeks**.
- [ ] Every risk limit has been **demonstrably triggered** in testing
      (see `tests/test_watchdog.py` and a live forced-breach drill).
- [ ] The kill switch (`/stop`) and watchdog flatten path have been tested live.
- [ ] The LLM soft gate has been observed blocking entries on `risk_off`.
- [ ] Telegram alerts + heartbeat are arriving reliably.
- [ ] Backtest / walk-forward metrics were reviewed honestly (drawdown, win
      rate, profit factor, trade count) and overfitting risk was assessed.

## Manual steps to go live with the smallest possible stake

1. **Fund a dedicated sub-account** on BingX with only the capital you are
   prepared to lose entirely. Do not use your main balance.
2. **Generate trade-enabled API keys** (spot only, no withdrawal permission,
   IP-allowlisted). Put them in `.env` as `FREQTRADE__EXCHANGE__KEY` /
   `FREQTRADE__EXCHANGE__SECRET`.
3. **Tighten the limits** in `.env` and `user_data/config.json`:
   - `dry_run_wallet` → your tiny real balance (e.g. 50 USDT)
   - `stake_amount` → the smallest viable per-trade stake
   - `max_open_trades` → `1`
   - `DAILY_MAX_LOSS_PCT` / `MAX_DRAWDOWN_PCT` → tighten (e.g. 2% / 4%)
4. **Confirm protections + `stoploss_on_exchange: true`** are active.
5. **Edit the single switch by hand:** in `user_data/config.json` set
   `"dry_run": false`. (This is the only place. It is intentionally not
   parameterised by env so it cannot be flipped by mistake or by automation.)
6. **Restart only the freqtrade service** and **watch it live** for the first
   trades. Keep FreqUI open and a finger on the STOP button.
7. **Verify the watchdog is running** against the live bot and that a forced
   small breach halts and flattens as expected.

## Rolling back to paper

Set `"dry_run": true` again in `user_data/config.json` and restart the
freqtrade service. Revoke the trade-enabled API keys if you are pausing.

---

**Reminder:** leverage is not used anywhere in this build. Spot only.
