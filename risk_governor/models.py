"""Data models for the Risk Governor: signals, snapshots, results, status."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TradingMode(str, Enum):
    OBSERVATION_ONLY = "OBSERVATION_ONLY"   # no orders; log signals + risk decisions
    PAPER_TRADING = "PAPER_TRADING"         # simulated orders only
    REAL_TRADING_STRICT = "REAL_TRADING_STRICT"  # real orders only after approval


class RiskMode(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    LOCKED = "locked"
    KILLED = "killed"
    COOLDOWN = "cooldown"
    NEWS_PAUSE = "news_pause"
    RECONCILIATION_ERROR = "reconciliation_error"


class Decision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class TradeSignal:
    """A proposed trade from the strategy. Mandatory fields are validated by the
    governor; missing/invalid -> rejection (fail closed)."""
    symbol: Optional[str] = None
    side: Optional[str] = None                 # "long"/"buy" or "short"/"sell"
    entry_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    quantity: Optional[float] = None           # suggested; governor has final say
    leverage: Optional[float] = None
    margin_mode: Optional[str] = None          # "isolated" | "cross" | "spot"
    max_holding_time_minutes: Optional[float] = None
    strategy_reason: Optional[str] = None
    timestamp: Optional[float] = None          # unix seconds
    signal_id: Optional[str] = None
    execution_id: Optional[str] = None
    # Optional pre-computed quality sub-scores (0..100). Missing -> conservative.
    quality_components: dict = field(default_factory=dict)

    def is_long(self) -> bool:
        return (self.side or "").lower() in {"long", "buy"}


@dataclass
class AccountSnapshot:
    """Current REAL account state from the exchange. `known=False` -> fail closed."""
    known: bool = False
    balance: float = 0.0
    equity: float = 0.0
    available_margin: float = 0.0
    open_positions: int = 0
    open_orders: int = 0
    open_symbols: tuple[str, ...] = ()
    margin_mode_confirmed: bool = False
    leverage_confirmed: bool = False
    current_leverage: float = 1.0


@dataclass
class MarketSnapshot:
    """Current market microstructure. Missing/stale -> fail closed."""
    known: bool = False
    bid: Optional[float] = None
    ask: Optional[float] = None
    last_price: Optional[float] = None
    price_timestamp: Optional[float] = None    # unix seconds of last price
    atr: Optional[float] = None
    atr_avg_20: Optional[float] = None
    last_candle_body_pct: Optional[float] = None
    orderbook_depth_quote: Optional[float] = None
    estimated_slippage_percent: Optional[float] = None

    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last_price


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""


@dataclass
class ApprovalResult:
    decision: Decision
    reason: str = ""
    quantity: float = 0.0                       # final, governor-authorized size
    position_value: float = 0.0
    risk_amount: float = 0.0
    risk_reward_ratio: float = 0.0
    trade_quality_score: float = 0.0
    risk_mode: RiskMode = RiskMode.NORMAL
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision == Decision.APPROVED

    def __bool__(self) -> bool:
        return self.approved


@dataclass
class RiskStatus:
    trading_enabled: bool = True
    risk_mode: str = RiskMode.NORMAL.value
    trading_mode: str = TradingMode.REAL_TRADING_STRICT.value
    account_balance: float = 0.0
    available_margin: float = 0.0
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    current_drawdown_percent: float = 0.0
    equity_peak: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    open_orders: int = 0
    risk_per_trade_percent: float = 0.5
    max_daily_loss_percent: float = 2.0
    max_weekly_loss_percent: float = 5.0
    max_total_drawdown_percent: float = 8.0
    max_leverage: float = 2
    max_capital_exposure_percent: float = 15.0
    news_pause: bool = False
    last_rejection_reason: str = ""
    last_approved_trade: str = ""
    last_kill_switch_reason: str = ""
    kill_switch_active: bool = False
    manual_restart_required: bool = False

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
