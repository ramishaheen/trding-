"""Trade pipeline — wires the layers in the mandated order:

    Strategy -> Weekly Target Manager -> Risk Governor -> Execution

Rules (spec section 12):
  * If the Weekly Target Manager blocks (target reached / risk-locked / over-
    trading / negative expectancy), the trade is rejected.
  * The Weekly Target Manager may only TIGHTEN risk (reduce size, raise the
    quality bar) — it passes a risk_multiplier (<=1) and min_quality_score into
    the governor.
  * The Risk Governor has FINAL authority: if it rejects, the trade does not
    execute, regardless of the target.

The Execution Engine must call evaluate_trade() and only place an order when
PipelineResult.approved is True, using PipelineResult.quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from risk_governor import RiskGovernor
from risk_governor.models import AccountSnapshot, ApprovalResult, MarketSnapshot, TradeSignal
from weekly_target_manager import WeeklyTargetManager
from weekly_target_manager.models import WeeklyDecision


@dataclass
class PipelineResult:
    approved: bool
    reason: str
    quantity: float = 0.0
    governor_result: Optional[ApprovalResult] = None
    weekly_decision: Optional[WeeklyDecision] = None

    def __bool__(self) -> bool:
        return self.approved


def evaluate_trade(
    wtm: WeeklyTargetManager,
    governor: RiskGovernor,
    signal: TradeSignal,
    account: AccountSnapshot,
    market: MarketSnapshot,
    now_ts: float,
    volatility_abnormal: bool = False,
) -> PipelineResult:
    # Keep the weekly manager in sync with the real account + governor state.
    if account and account.known:
        wtm.update(
            equity=account.equity or account.balance,
            balance=account.balance,
            now_ts=now_ts,
            kill_switch_active=governor.kill_switch_active,
            drawdown_percent=governor.current_drawdown_percent(),
        )

    # 1) Weekly Target Manager (can block or tighten only).
    wd = wtm.check_trade(now_ts, volatility_abnormal=volatility_abnormal)
    if not wd.allow:
        wtm.note_rejection(wd.reason)
        return PipelineResult(False, f"weekly:{wd.reason}", 0.0, weekly_decision=wd)

    # 2) Risk Governor — FINAL authority. Weekly verdict only tightens.
    result = governor.approve_trade(
        signal, account, market, now_ts,
        risk_multiplier=wd.risk_multiplier,
        min_quality_override=wd.min_quality_score,
    )
    if not result.approved:
        wtm.note_rejection(result.reason)
        return PipelineResult(False, f"governor:{result.reason}", 0.0,
                              governor_result=result, weekly_decision=wd)

    return PipelineResult(True, "approved", result.quantity,
                          governor_result=result, weekly_decision=wd)
