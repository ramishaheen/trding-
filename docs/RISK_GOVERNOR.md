# Risk Governor Layer

The Risk Governor is the **single authority** that decides whether a trade may
reach the exchange. The mandated flow is:

```
Strategy Engine → Weekly Target Manager → Risk Governor → Execution Engine → BingX API
```

The strategy never sends orders directly. Every order is approved by
`RiskGovernor.approve_trade()` (via `trade_pipeline.evaluate_trade`). If the
governor rejects, the trade does not execute. **It fails closed:** when anything
is unknown or uncertain, it rejects and, where appropriate, halts new trading.

## Files

| File | Role |
|------|------|
| `risk_config.json` | all risk values (never hardcoded in logic); `RG_<KEY>` env overrides |
| `risk_governor/config.py` | config loader |
| `risk_governor/models.py` | `TradeSignal`, `AccountSnapshot`, `MarketSnapshot`, `ApprovalResult`, `RiskStatus` |
| `risk_governor/checks.py` | pure, unit-tested checks (completeness, leverage/margin, SL/TP, RR, spread/slippage/volatility, sizing, martingale, duplicate, quality) |
| `risk_governor/governor.py` | `RiskGovernor` — state machine + `approve_trade()` |
| `risk_governor/alerts.py` | `send_alert(level,title,message,data)` (logs + optional Telegram) |
| `risk_governor/audit.py` | structured JSON audit trail |
| `trade_pipeline.py` | wires Weekly Target Manager → Risk Governor (governor final authority) |

## What every trade is checked against

Fail-closed order: kill switch → reconciliation → trading enabled → news pause →
cooldown → daily/weekly locks → signal completeness → account known → market
valid/fresh → max open positions → consecutive-loss cap → duplicate → leverage &
isolated margin → averaging-down → stop-loss & take-profit present/sane → stop
distance → risk:reward ≥ min → spread → slippage → volatility (ATR spike) →
**position sizing** → martingale → exposure → **trade-quality score ≥ min**.

### Position sizing (governor has final authority over quantity)

```
risk_amount        = balance * max_risk_per_trade_percent/100   (× post-loss/size multipliers)
stop_distance_frac = |entry - stop| / entry
raw_value          = risk_amount / stop_distance_frac
final_value        = min(raw_value, balance * max_capital_exposure_percent/100)
quantity           = final_value / entry
```
Rejected if: no stop, stop distance zero/too wide/too narrow, below BingX min
order size, or exposure exceeded. The strategy may suggest a size; the governor
only ever **reduces** it.

## Risk configuration (defaults, for a $100 account)

| Key | Default | Meaning |
|-----|---------|---------|
| `max_risk_per_trade_percent` | 0.5 | $0.50 risked per trade |
| `max_daily_loss_percent` | 2.0 | $2 → stop new trades until next day |
| `max_weekly_loss_percent` | 5.0 | $5 → stop until next week |
| `max_total_drawdown_percent` | 8.0 | $8 → **kill switch** + manual restart |
| `max_open_positions` | 1 | concurrency cap |
| `max_capital_exposure_percent` | 15.0 | ≤ $15 in one position |
| `max_leverage` | 2 | cross margin rejected; isolated forced |
| `min_risk_reward_ratio` | 1.5 | (preferred 2.0) |
| `max_consecutive_losses` | 3 | → 24h cooldown |
| `max_spread_percent` / `max_slippage_percent` | 0.05 / 0.10 | liquidity guards |
| `atr_spike_multiplier` | 2.0 | volatility cutoff |
| `trade_quality_min_score` | 75 | weak setups rejected |
| `fail_closed` | true | uncertainty → reject |

All values are configurable via `risk_config.json` or `RG_*` env vars.

## Trading modes (spec section 20)

`OBSERVATION_ONLY` (no orders, log only) · `PAPER_TRADING` (simulated) ·
`REAL_TRADING_STRICT` (real orders only after approval — default).

Switch with `RG_TRADING_MODE=...` or `governor.set_trading_mode(...)`. The
executor additionally requires the master `LIVE_BROWSER_TRADING_ENABLED=on`
physical switch before any real order is placed (defense in depth).

## Kill switch

`governor.emergency_kill_switch(reason)` triggers on: daily/weekly/drawdown
breach, missing/unconfirmable stop-loss, API errors over threshold, repeated
order rejections, abnormal spread/slippage/volatility, unverifiable account or
position state, duplicate/unknown orders, reconciliation mismatch, stale price,
or any attempt to execute without approval. It cancels orders, flattens unsafe
positions (callbacks), alerts, and **latches**.

### Manual restart after a kill switch

Automatic restart is disabled by default
(`manual_restart_required_after_kill_switch: true`). An operator must call
`governor.manual_restart(confirm=True)` (or restart the service after fixing the
cause). Nothing resumes on its own.

## How to run the tests

```bash
pip install pytest requests
pytest -q            # 138 tests incl. risk governor + weekly target + pipeline
pytest -q tests/test_risk_governor.py
```

Covers (spec section 24): sizing, per-trade risk, daily/weekly/drawdown locks,
leverage & cross-margin rejection, missing SL/TP/holding-time, martingale &
averaging-down, spread/slippage/volatility, risk-reward, trade quality, cooldown,
kill switch, reconciliation mismatch, duplicate orders, and fail-closed behaviour.

## Remaining risks (cannot be fully eliminated)

- **Exchange/market risk:** gaps, flash crashes, and slippage beyond estimates
  can still cause loss between decision and fill.
- **Spot vs futures:** this build is **spot** (leverage 1, no liquidation). The
  leverage/margin checks are enforced generically; a futures execution adapter
  (leverage set, isolated margin, liquidation-aware sizing) would be required
  before trading perpetuals.
- **Signal completeness:** live approval requires the strategy to emit a full
  signal (stop-loss, take-profit, ATR, etc.). Until it does, the governor
  **rejects** (safe) — see "Integration status" in the README.
- **Connectivity:** API/DB outages cause fail-closed rejection (safe), but mean
  no trading until resolved.
- **No profit guarantee.** Rejecting a bad trade is a successful outcome.
  Capital protection comes first.
