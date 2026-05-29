"""Risk-adjusted hyperopt loss for walk-forward tuning.

Goal: prefer configurations that make money *with controlled risk and enough
trades to be believable* — not curve-fit flukes. Lower return value = better.

It penalises:
  * too few trades (overfit / lucky one-offs),
  * drawdown above the account limit (capital protection first),
  * negative expectancy,
and rewards total return and positive expectancy.

Use it with:  freqtrade hyperopt --hyperopt-loss RiskAdjustedLoss ...
This file is NOT imported by the test suite (it needs freqtrade at runtime);
CI only syntax-checks it.
"""

from __future__ import annotations

try:  # import path has moved across freqtrade versions
    from freqtrade.optimize.hyperopt_loss_interface import IHyperOptLoss
except Exception:  # noqa: BLE001
    from freqtrade.optimize.hyperopt import IHyperOptLoss  # type: ignore

# A config that trades only a handful of times over the train window is almost
# always overfit. Require a believable sample.
MIN_TRADES = 40
# Capital-protection target: drawdown beyond this is penalised hard.
TARGET_MAX_DRAWDOWN = 0.10


class RiskAdjustedLoss(IHyperOptLoss):
    @staticmethod
    def hyperopt_loss_function(results, trade_count, min_date, max_date,
                               config=None, **kwargs) -> float:
        # Strongly reject thin samples (they make hyperopt chase noise).
        if trade_count < MIN_TRADES:
            return 100.0 + (MIN_TRADES - trade_count)

        cfg = config or {}
        starting_balance = (cfg.get("dry_run_wallet")
                            or kwargs.get("starting_balance") or 1000.0)

        profit_abs = results["profit_abs"]
        total_ratio = float(profit_abs.sum()) / float(starting_balance)

        # Max drawdown from the equity curve (relative to running peak).
        equity = starting_balance + profit_abs.cumsum()
        peak = equity.cummax()
        drawdown = float(((peak - equity) / peak).max()) if len(equity) else 0.0

        expectancy = float(results["profit_ratio"].mean())

        # Lower is better: maximise return, reward expectancy, punish drawdown.
        loss = -total_ratio
        loss -= 5.0 * expectancy
        if drawdown > TARGET_MAX_DRAWDOWN:
            loss += 20.0 * (drawdown - TARGET_MAX_DRAWDOWN)
        return float(loss)
