from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional

from bot.journal import Journal
from bot.shapes import Fill, OrderIntent, Position, RiskLimits, RiskState, Signal


def fire_halt(state: RiskState) -> RiskState:
    return replace(state, halt_active=True)


def fire_kill(state: RiskState) -> RiskState:
    return replace(state, halt_active=True, kill_active=True)


def operator_reset(state: RiskState) -> RiskState:
    return replace(state, halt_active=False, kill_active=False)


def build_intent(signal: Signal, signal_id: str, state: RiskState, journal: Journal, now: datetime) -> OrderIntent:
    size = state.account_size * state.risk_per_trade
    intent = OrderIntent(
        symbol=signal.snapshot_id.split(":")[0],
        side="buy" if signal.direction == "long" else "sell",
        size=size,
        risk_numbers={"account_size": state.account_size, "risk_per_trade": state.risk_per_trade, "size": size},
        signal_id=signal_id,
    )
    journal.append(now, "intent_proposed", {"signal_id": signal_id, "size": size})
    return intent


def _schema_error(intent: OrderIntent) -> Optional[str]:
    if not intent.symbol or intent.side not in ("buy", "sell"):
        return "bad symbol or side"
    if intent.size <= 0:
        return "non-positive size"
    if not intent.signal_id:
        return "missing signal id"
    return None


def review_intent(intent: OrderIntent, state: RiskState, limits: RiskLimits, journal: Journal, now: datetime) -> str:
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
    if intent.size > limits.max_trade_size:
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
    journal.append(now, "fill", {"price": fill.price, "size": fill.size, "venue": "paper"})
    updated_position = update_position(position, intent, fill)
    updated_state = update_day_pnl(state, updated_position, mark_price)
    if updated_state.day_pnl <= -limits.max_day_loss:
        updated_state = fire_halt(updated_state)
        journal.append(now, "halt", {"day_pnl": updated_state.day_pnl})
    return fill, updated_position, updated_state
