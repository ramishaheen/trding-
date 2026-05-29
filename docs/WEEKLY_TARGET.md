# Weekly Target Manager

Tracks an **aspirational** weekly profit target (default **4x**), locks profit as
gains accrue, reduces risk in adaptive safe modes, controls trade frequency, and
stops trading when the target is reached or risk limits are hit.

> The target is **never forced**. Capital protection comes first. If 4x is not
> achievable under the risk limits, the system reports
> *"Weekly target not achievable under current risk limits."* — that is a
> successful safety outcome, not a failure.

## Order of authority

```
Strategy → Weekly Target Manager → Risk Governor (FINAL) → Execution → BingX
```

- If the Target Manager **blocks** (target reached, risk-locked, overtrading,
  negative expectancy) → trade rejected.
- The Target Manager may only **tighten** risk: it passes a `risk_multiplier`
  (≤1) and a `min_quality_score` into the governor. It can never loosen a limit.
- The **Risk Governor has final authority**. If it rejects, the trade does not
  execute — even if the Target Manager approved. (`trade_pipeline.evaluate_trade`)

## Files

| File | Role |
|------|------|
| `weekly_target_config.json` | target config (`WT_<KEY>` env overrides) |
| `weekly_target_manager/calculations.py` | pure math (metrics, required daily return, expectancy, profit lock) |
| `weekly_target_manager/manager.py` | `WeeklyTargetManager` — state, safe modes, frequency, reporting |
| `weekly_target_manager/models.py` | statuses, decision, dashboard object |
| `trade_pipeline.py` | enforces WTM → Governor ordering |

## Target math (example: $100 start, 4x)

```
weekly_target_balance   = 100 * 4   = 400
required_weekly_profit  = 400 - 100 = 300
weekly_profit           = equity - 100
weekly_profit_percent   = weekly_profit / 100 * 100
target_completion_percent = weekly_profit / 300 * 100
```

## Profit locking (protect gains)

| Weekly profit | Action |
|---------------|--------|
| ≥ 25% | reduce risk per trade by 25% (×0.75) |
| ≥ 50% | reduce risk by 50% (×0.50) |
| ≥ 100% | reduce risk by 75% (×0.25) |
| ≥ 300% (4x) | **stop trading, lock profit, mark week completed** (cancel/secure positions) |

## Adaptive safe modes

| Mode | Trigger | Action |
|------|---------|--------|
| NORMAL | calm, no limits near | full size |
| CAUTION | weekly loss >2% or daily >1% or 2 losses or abnormal vol | risk ×0.5, min score 85 |
| DEFENSIVE | weekly loss >3.5% or daily >1.5% or vol spike | risk ×0.25, min score 90 |
| LOCKED | daily/weekly/drawdown limit, kill switch, or target reached | no new trades |

## Weekly loss control (never overridden by the target)

Daily loss 2% → stop until next day · weekly 5% → stop until next week ·
drawdown 8% → kill switch + manual restart · 3 consecutive losses → 24h cooldown.

## Trade-frequency control (don't chase the target)

`max_trades_per_day=3`, `max_trades_per_week=10`,
`minimum_minutes_between_trades=60`, `cooldown_after_loss=30m`,
`cooldown_after_win=15m`. Being behind target never increases frequency.

## Expectancy guard

`expectancy = win_rate*avg_win − loss_rate*avg_loss`. With enough recent trades,
**negative expectancy blocks live trading** (switch to observation/paper). A
positive-but-too-low expectancy marks the target *unrealistic under current risk
limits*.

## Weekly reset

At the start of each week (**Asia/Amman, Monday 00:00**): reset weekly start
balance to current equity, weekly realized PnL, and target completion. Long-term
drawdown and performance history are **kept** (losses are never hidden).

## Dashboard + reports

`WeeklyTargetManager.dashboard(now)` returns the section-10 object (start/current/
target, completion %, required daily return, status, risk mode, locks).
`daily_report(now)` and `weekly_report(now)` produce the section-14 summaries
(win rate, profit factor, expectancy, drawdown, biggest win/loss, rejected
trades and top reasons, target realism, whether risk rules were respected).

## Tests

`tests/test_weekly_target.py` covers the metrics, profit-lock levels, expectancy,
loss/drawdown locks, overtrading, spacing, safe modes, realism, the dashboard
object, and the pipeline rules (both approve → execute; governor rejects → no
trade; target reached → no trade even if the governor would approve).
