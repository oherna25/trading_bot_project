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
class GexProfile:
    snapshot_id: str
    per_strike_exposure: dict[float, float]
    total_exposure: float
    flip_point: Optional[float]


@dataclass(frozen=True)
class VexProfile:
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
class OrderIntent:
    symbol: str
    side: str
    size: float
    risk_numbers: dict[str, float]
    signal_id: str


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
class DashboardLink:
    snapshot_id: str
    url: str
    created_utc: datetime
    expires_utc: datetime


@dataclass(frozen=True)
class OperatorConfig:
    dashboard_base_url: str
    dashboard_link_expiry_seconds: int
    max_day_loss: float
    day_profit_target: float
    max_trade_size: float


@dataclass(frozen=True)
class RiskLimits:
    max_trade_size: float
    day_profit_target: float
    max_day_loss: float


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
