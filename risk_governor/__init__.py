"""Risk Governor Layer.

The single authority that decides whether a trade may reach the exchange:

    Strategy Engine -> Risk Governor Layer -> Execution Engine -> BingX API

Nothing places an order without RiskGovernor.approve_trade() returning approved.
The layer fails CLOSED: when anything is unknown or uncertain, it rejects the
trade and (where appropriate) halts new trading.
"""

from .config import RiskConfig, load_config
from .models import (
    AccountSnapshot,
    ApprovalResult,
    Decision,
    MarketSnapshot,
    RiskMode,
    RiskStatus,
    TradeSignal,
    TradingMode,
)
from .governor import RiskGovernor

__all__ = [
    "RiskConfig",
    "load_config",
    "RiskGovernor",
    "TradeSignal",
    "AccountSnapshot",
    "MarketSnapshot",
    "ApprovalResult",
    "RiskStatus",
    "Decision",
    "RiskMode",
    "TradingMode",
]
