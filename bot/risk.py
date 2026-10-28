from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from bot.journal import Journal
from bot.shapes import Fill, OperatorProfile, OrderIntent, OrderLeg, Position, RiskLimits, RiskState, Signal


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
) -> OrderIntent:
    size = state.account_size * state.risk_per_trade
    leg_tuple = tuple(legs)
    total = sum(leg.size for leg in leg_tuple) if leg_tuple else size
    risk_numbers = {
        "account_size": state.account_size,
        "risk_per_trade": state.risk_per_trade,
        "size": total,
    }
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


def update_position(position: Optional[Position], intent: OrderIntent, fill: Fill) -> Position:
    signed = fill.size if intent.side == "buy" else -fill.size
    if position is None:
        return Position(symbol=intent.symbol, net_size=signed, average_price=fill.price, unrealized_pnl=0.0)
    net = position.net_size + signed
    average = (position.average_price * position.net_size + fill.price * signed) / net if net != 0 else 0.0
    return Position(symbol=intent.symbol, net_size=net, average_price=average, unrealized_pnl=0.0)


def update_day_pnl(state: RiskState, position: Position, mark_price: float) -> RiskState:
    return replace(state, day_pnl=(mark_price - position.average_price) * position.net_size)


def submit_paper(
    intent: OrderIntent,
    fill_price: float,
    mark_price: float,
    state: RiskState,
    position: Optional[Position],
    limits: RiskLimits,
    journal: Journal,
    now: datetime,
) -> tuple[Fill, Position, RiskState]:
    fill = Fill(price=fill_price, size=intent.size, timestamp_utc=now, venue="paper")
    journal.append(
        now,
        "fill",
        {
            "price": fill.price,
            "size": fill.size,
            "venue": "paper",
            "legs": [_leg_summary(leg) for leg in intent.legs],
        },
    )
    updated_position = update_position(position, intent, fill)
    updated_state = update_day_pnl(state, updated_position, mark_price)
    if updated_state.day_pnl <= -limits.max_day_loss:
        updated_state = fire_halt(updated_state)
        journal.append(now, "halt", {"day_pnl": updated_state.day_pnl})
    return fill, updated_position, updated_state
