# Self-improvement loop (learn + recommend)

The bot accumulates **experience** (every trade outcome joined with the
conditions at entry) and a dedicated **analyst agent** turns that into
**knowledge** — a growing set of reviews and testable improvement proposals.

> **Recommend-only by design.** The analyst never trades and never changes
> settings. It proposes; *you* (and a walk-forward backtest) decide. This is the
> safe way to "get better over time" — blindly auto-rewiring a live strategy
> from recent results overfits and blows up.

## How the loop works

```
trade (paper) ─▶ trade_outcomes (experience)  ─▶ analytics (what worked, by
                  = each closed trade + the         regime / AI confidence /
                  regime, AI confidence, per-        per-coin bias / exit reason)
                  coin bias at entry                        │
                                                            ▼
                                              Claude analyst agent reviews it ─▶
                                              strategy_insights (knowledge):
                                              findings + TESTABLE proposals
                                                            │
                                              you validate with walk-forward ─▶
                                              apply only if it survives out-of-
                                              sample ─▶ better params ─▶ repeat
```

Each cycle the journal grows and the proposals get sharper. The edge still has
to prove out — this compounds *insight*, not guaranteed profit.

## Components
- `research/journal.py` — reads closed trades from Freqtrade's SQLite and joins
  each with the `market_context` at entry → `trade_outcomes`.
- `research/analytics.py` — pure, unit-tested performance maths (win rate,
  expectancy, profit factor) segmented by regime / confidence / pair-bias / exit.
- `research/analyst.py` — the scheduled Claude agent (`claude-opus-4-8`,
  structured output) that writes a review + ranked, testable proposals into
  `strategy_insights`. Runs daily by default (`ANALYST_INTERVAL_SECONDS`); waits
  until there are at least `ANALYST_MIN_TRADES` closed trades.

## Reading the knowledge base
```sql
-- latest review
SELECT created_at, trades_analyzed, summary FROM strategy_insights
ORDER BY created_at DESC LIMIT 1;
-- the proposed, testable changes
SELECT hypotheses FROM strategy_insights ORDER BY created_at DESC LIMIT 1;
```
Or watch it run: `docker compose logs -f analyst`.

## Acting on a proposal (the human-approved step)
1. Read the latest `hypotheses` (each names a `parameter` + `suggested_range`).
2. Validate with walk-forward (optimise on older data, test on unseen data):
   `./scripts/walk_forward.sh` — see `docs/BACKTEST_HYPEROPT.md`.
3. Apply the tuned params **only if the out-of-sample test is profitable** with a
   believable trade count and drawdown within limits.
4. The Risk Governor still vets every resulting trade — a bad lesson can't bypass
   capital protection.

## Honest limits
- Needs a meaningful number of closed trades before its conclusions mean
  anything — early reviews will say "too thin to conclude," which is correct.
- It finds *correlations* in your own history; markets change, so always confirm
  out-of-sample and in forward paper-trading before trusting a change.
