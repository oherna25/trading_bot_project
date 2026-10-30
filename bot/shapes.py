from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

DEFAULT_CONTRACT_MULTIPLIER = 100
DEFAULT_RISK_PER_TRADE = 0.01


@dataclass(frozen=True)
class Bar:
    interval: str
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OptionContract:
    strike: float
    expiry: str
    call_or_put: str
    open_interest: float
    gamma: Optional[float] = None
    vega: Optional[float] = None
    delta: Optional[float] = None
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    iv: Optional[float] = None


@dataclass(frozen=True)
class ChainSnapshot:
    underlying: str
    timestamp_utc: datetime
    source_name: str
    latency_ms: float
    contracts_list: tuple[OptionContract, ...] = ()


@dataclass(frozen=True)
class Holding:
    symbol: str
    kind: str
    side: str
    strike: Optional[float] = None
    expiry: Optional[str] = None
    qty: float = 1.0


@dataclass(frozen=True)
class GexProfile:
    snapshot_id: str
    per_strike_exposure: dict[float, float]
    total_exposure: float
    flip_point: Optional[float]
    spot: Optional[float] = None
    front_expiry: Optional[str] = None
    front_exposure: float = 0.0
    call_exposure: float = 0.0
    put_exposure: float = 0.0
    per_strike_call_exposure: dict[float, float] = field(default_factory=dict)
    per_strike_put_exposure: dict[float, float] = field(default_factory=dict)


@dataclass(frozen=True)
class VexProfile:
    snapshot_id: str
    per_strike_exposure: dict[float, float]
    total_exposure: float


@dataclass(frozen=True)
class DexProfile:
    snapshot_id: str
    per_strike_exposure: dict[float, float]
    total_exposure: float


@dataclass(frozen=True)
class Signal:
    direction: str
    thesis: str
    confidence: float
    snapshot_id: str
    status: str = "unknown"
    trigger: str = ""
    entry_band: str = ""
    checklist: tuple[str, ...] = ()
    coverage: float = 0.0


@dataclass(frozen=True)
class TradeIdea:
    symbol: str
    direction: str
    thesis: str
    trigger: str
    strike: float
    expiry: str
    call_or_put: str
    confidence: float
    snapshot_id: str
    hedge_strike: Optional[float] = None
    spread: str = ""
    max_loss: Optional[float] = None
    breakeven: Optional[float] = None
    net_debit: Optional[float] = None
    spread_pct: Optional[float] = None
    earnings_date: Optional[str] = None
    iv_rank: Optional[float] = None


@dataclass(frozen=True)
class ModelReceipt:
    model_id: str
    role_name: str
    prompt_hash: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float


@dataclass(frozen=True)
class OrderLeg:
    symbol: str
    side: str
    size: float
    strike: Optional[float] = None
    expiry: str = ""
    call_or_put: str = ""


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    size: float
    risk_numbers: dict[str, float]
    signal_id: str
    legs: tuple[OrderLeg, ...] = ()


@dataclass(frozen=True)
class Fill:
    price: float
    size: float
    timestamp_utc: datetime
    venue: str


@dataclass(frozen=True)
class Position:
    symbol: str
    net_size: float
    average_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class RiskState:
    account_size: float
    risk_per_trade: float
    day_pnl: float
    halt_active: bool
    kill_active: bool


@dataclass(frozen=True)
class JournalEntry:
    timestamp_utc: datetime
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorConfig:
    max_day_loss: float
    day_profit_target: float
    max_trade_size: float


def default_operator_config() -> OperatorConfig:
    return OperatorConfig(
        max_day_loss=1000.0,
        day_profit_target=500.0,
        max_trade_size=5000.0,
    )


@dataclass(frozen=True)
class RiskLimits:
    max_trade_size: float
    day_profit_target: float
    max_day_loss: float
    max_positions: int = 0
    drawdown_pause_pct: float = 0.0
    max_per_sector: int = 0


@dataclass(frozen=True)
class PaperEvidence:
    clean_days: int
    clean_days_target: int
    max_drawdown: float
    drawdown_bound: float
    halt_drills_passed: bool


@dataclass(frozen=True)
class OperatorProfile:
    risk_tolerance: str
    portfolio_size: float
    knowledge: str
    options_ok: bool


@dataclass(frozen=True)
class PremiumCandidate:
    symbol: str
    action: str
    strike: float
    expiry: str
    dte: int
    bid: float
    ask: Optional[float]
    delta: float
    iv: Optional[float]
    open_interest: float
    premium_return: float
    capital_at_risk: float
    needs_shares: bool
    iv_rank: Optional[float]
    snapshot_id: str


@dataclass(frozen=True)
class PortfolioView:
    open_positions: int = 0
    open_symbols: tuple[str, ...] = ()
    peak_day_pnl: Optional[float] = None
