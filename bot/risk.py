from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from bot.journal import Journal
from bot.shapes import (
    Fill,
    OperatorProfile,
    OrderIntent,
    OrderLeg,
    PortfolioView,
    Position,
    RiskLimits,
    RiskState,
    Signal,
)

SECTORS = {
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Tech", "GOOGL": "Tech", "GOOG": "Tech",
    "META": "Tech", "AMZN": "Tech", "AMD": "Tech", "INTC": "Tech", "ORCL": "Tech",
    "CRM": "Tech", "ADBE": "Tech", "AVGO": "Tech", "QCOM": "Tech",
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "C": "Financials",
    "GS": "Financials", "MS": "Financials", "AXP": "Financials", "V": "Financials",
    "MA": "Financials", "SCHW": "Financials",
    "TSLA": "Auto", "F": "Auto", "GM": "Auto", "RIVN": "Auto",
    "DAL": "Travel", "UAL": "Travel", "AAL": "Travel", "LUV": "Travel",
    "MAR": "Travel", "ABNB": "Travel", "BKNG": "Travel",
    "JNJ": "Healthcare", "UNH": "Healthcare", "PFE": "Healthcare", "LLY": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    "WMT": "Consumer", "COST": "Consumer", "HD": "Consumer", "NKE": "Consumer",
    "SBUX": "Consumer", "MCD": "Consumer", "KO": "Consumer", "PEP": "Consumer",
    "DIS": "Media", "NFLX": "Media", "WBD": "Media",
    "T": "Telecom", "VZ": "Telecom", "TMUS": "Telecom",
}


def positions_capped(view: PortfolioView, limits: RiskLimits) -> bool:
    return limits.max_positions > 0 and view.open_positions >= limits.max_positions


def drawdown_paused(day_pnl: float, view: PortfolioView, limits: RiskLimits) -> bool:
    peak = view.peak_day_pnl
    return (
        limits.drawdown_pause_pct > 0
        and peak is not None
        and peak > 0
        and (peak - day_pnl) >= limits.drawdown_pause_pct * max(peak, 0.0)
    )


def sector_capped(view: PortfolioView, symbol: str, limits: RiskLimits) -> bool:
    sector = SECTORS.get(symbol)
    if limits.max_per_sector <= 0 or sector is None:
        return False
    held = sum(1 for open_symbol in view.open_symbols if SECTORS.get(open_symbol) == sector)
    return held >= limits.max_per_sector


def fire_halt(state: RiskState) -> RiskState:
    return replace(state, halt_active=True)


def fire_kill(state: RiskState) -> RiskState:
    return replace(state, halt_active=True, kill_active=True)


def operator_reset(state: RiskState) -> RiskState:
    return replace(state, halt_active=False, kill_active=False)


def is_options_leg(leg: OrderLeg) -> bool:
    return leg.call_or_put in ("call", "put")


def is_options_intent(intent: OrderIntent) -> bool:
    return any(is_options_leg(leg) for leg in intent.legs)


def intent_notional(intent: OrderIntent) -> float:
    if intent.legs:
        return sum(leg.size for leg in intent.legs)
    return intent.size


def even_legs(total_size: float, specs: Sequence[tuple[str, str, Optional[float], str, str]]) -> tuple[OrderLeg, ...]:
    if not specs:
        raise ValueError("combo needs at least one leg spec")
    share = total_size / len(specs)
    return tuple(
        OrderLeg(symbol=symbol, side=side, size=share, strike=strike, expiry=expiry, call_or_put=call_or_put)
        for symbol, side, strike, expiry, call_or_put in specs
    )


def _leg_summary(leg: OrderLeg) -> dict[str, object]:
    return {
        "symbol": leg.symbol,
        "side": leg.side,
        "size": leg.size,
        "strike": leg.strike,
        "expiry": leg.expiry,
        "call_or_put": leg.call_or_put,
    }


def build_intent(
    signal: Signal,
    signal_id: str,
    state: RiskState,
    journal: Journal,
    now: datetime,
    legs: Sequence[OrderLeg] = (),
    max_loss_per_contract: Optional[float] = None,
) -> Optional[OrderIntent]:
    if signal.direction not in ("long", "short"):
        journal.append(now, "intent_skipped", {"signal_id": signal_id, "reason": f"direction {signal.direction}"})
        return None
    budget = state.account_size * state.risk_per_trade
    risk_numbers = {
        "account_size": state.account_size,
        "risk_per_trade": state.risk_per_trade,
    }
    if max_loss_per_contract is not None and max_loss_per_contract > 0:
        contracts = max(1, int(budget // max_loss_per_contract))
        size = contracts * max_loss_per_contract
        risk_numbers["max_loss_per_contract"] = max_loss_per_contract
        risk_numbers["contracts"] = float(contracts)
    else:
        size = budget
    leg_tuple = tuple(legs)
    total = sum(leg.size for leg in leg_tuple) if leg_tuple else size
    risk_numbers["size"] = total
    if leg_tuple:
        risk_numbers["legs"] = float(len(leg_tuple))
    intent = OrderIntent(
        symbol=signal.snapshot_id.split(":")[0],
        side="buy" if signal.direction == "long" else "sell",
        size=total,
        risk_numbers=risk_numbers,
        signal_id=signal_id,
        legs=leg_tuple,
    )
    journal.append(
        now,
        "intent_proposed",
        {"signal_id": signal_id, "size": total, "legs": [_leg_summary(leg) for leg in leg_tuple]},
    )
    return intent


def _leg_error(leg: OrderLeg) -> Optional[str]:
    if not leg.symbol or leg.side not in ("buy", "sell"):
        return "bad leg symbol or side"
    if leg.size <= 0:
        return "non-positive leg size"
    if leg.call_or_put and leg.call_or_put not in ("call", "put"):
        return "bad leg call_or_put"
    if leg.call_or_put and (leg.strike is None or leg.strike <= 0):
        return "option leg needs a positive strike"
    if leg.call_or_put and not leg.expiry:
        return "option leg needs an expiry"
    if leg.call_or_put:
        try:
            datetime.fromisoformat(leg.expiry)
        except ValueError:
            return "bad leg expiry"
    if not leg.call_or_put and leg.strike is not None:
        return "stock leg must not carry a strike"
    return None


def _schema_error(intent: OrderIntent) -> Optional[str]:
    if not intent.symbol or intent.side not in ("buy", "sell"):
        return "bad symbol or side"
    if intent.size <= 0:
        return "non-positive size"
    if not intent.signal_id:
        return "missing signal id"
    for leg in intent.legs:
        error = _leg_error(leg)
        if error is not None:
            return error
    return None


def review_intent(
    intent: OrderIntent,
    state: RiskState,
    limits: RiskLimits,
    journal: Journal,
    now: datetime,
    profile: Optional[OperatorProfile] = None,
    view: Optional[PortfolioView] = None,
) -> str:
    error = _schema_error(intent)
    if error is not None:
        journal.append(now, "intent_vetoed", {"reason": error})
        return "rejected"
    if state.halt_active:
        journal.append(now, "intent_blocked", {"reason": "halt active"})
        return "rejected"
    if state.kill_active:
        journal.append(now, "intent_blocked", {"reason": "kill active"})
        return "rejected"
    if view is not None and positions_capped(view, limits):
        journal.append(now, "intent_vetoed", {"reason": "max positions reached"})
        return "rejected"
    if view is not None and drawdown_paused(state.day_pnl, view, limits):
        journal.append(now, "intent_vetoed", {"reason": "drawdown pause active"})
        return "rejected"
    if view is not None and sector_capped(view, intent.symbol, limits):
        journal.append(now, "intent_vetoed", {"reason": "sector cap reached"})
        return "rejected"
    if is_options_intent(intent) and (profile is None or not profile.options_ok):
        journal.append(now, "intent_vetoed", {"reason": "options blocked by profile"})
        return "rejected"
    if intent_notional(intent) > limits.max_trade_size:
        journal.append(now, "intent_vetoed", {"reason": "outside trade targets"})
        return "rejected"
    if state.day_pnl <= -limits.max_day_loss or state.day_pnl >= limits.day_profit_target:
        journal.append(now, "intent_vetoed", {"reason": "outside day targets"})
        return "rejected"
    journal.append(now, "intent_approved", {"signal_id": intent.signal_id, "size": intent.size})
    return "approved"


def update_position(
    position: Optional[Position], intent: OrderIntent, fill: Fill, mark_price: Optional[float] = None
) -> Position:
    signed = fill.size if intent.side == "buy" else -fill.size
    if position is None:
        average = fill.price
        net = signed
    else:
        net = position.net_size + signed
        average = (position.average_price * position.net_size + fill.price * signed) / net if net != 0 else 0.0
    mark = mark_price if mark_price is not None else fill.price
    return Position(symbol=intent.symbol, net_size=net, average_price=average, unrealized_pnl=(mark - average) * net)


def realized_pnl(position: Optional[Position], intent: OrderIntent, fill: Fill) -> float:
    if position is None or position.net_size == 0:
        return 0.0
    signed = fill.size if intent.side == "buy" else -fill.size
    if (position.net_size > 0) == (signed > 0):
        return 0.0
    closed = min(abs(position.net_size), abs(signed))
    direction = 1.0 if position.net_size > 0 else -1.0
    return (fill.price - position.average_price) * closed * direction


def update_day_pnl(
    state: RiskState,
    old_position: Optional[Position],
    new_position: Position,
    realized: float = 0.0,
    fees: float = 0.0,
) -> RiskState:
    old_unrealized = old_position.unrealized_pnl if old_position is not None else 0.0
    return replace(state, day_pnl=state.day_pnl + (new_position.unrealized_pnl - old_unrealized) + realized - fees)


def paper_fill_price(side: str, mark_price: float, bid: Optional[float], ask: Optional[float]) -> float:
    if side == "buy" and ask is not None and ask > 0:
        return ask
    if side == "sell" and bid is not None and bid > 0:
        return bid
    return mark_price


def submit_paper(
    intent: OrderIntent,
    mark_price: float,
    state: RiskState,
    position: Optional[Position],
    limits: RiskLimits,
    journal: Journal,
    now: datetime,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    slippage_bps: float = 0.0,
    fees: float = 0.0,
) -> tuple[Fill, Position, RiskState]:
    touch = paper_fill_price(intent.side, mark_price, bid, ask)
    slip = touch * slippage_bps / 10000.0
    fill_price = touch + slip if intent.side == "buy" else touch - slip
    fill = Fill(price=fill_price, size=intent.size, timestamp_utc=now, venue="paper")
    journal.append(
        now,
        "fill",
        {
            "symbol": intent.symbol,
            "side": intent.side,
            "price": fill.price,
            "size": fill.size,
            "venue": "paper",
            "mark_price": mark_price,
            "bid": bid,
            "ask": ask,
            "slippage_bps": slippage_bps,
            "fees": fees,
            "legs": [_leg_summary(leg) for leg in intent.legs],
        },
    )
    updated_position = update_position(position, intent, fill, mark_price)
    updated_state = update_day_pnl(state, position, updated_position, realized_pnl(position, intent, fill), fees)
    if updated_state.day_pnl <= -limits.max_day_loss:
        updated_state = fire_halt(updated_state)
        journal.append(now, "halt", {"day_pnl": updated_state.day_pnl})
    return fill, updated_position, updated_state
