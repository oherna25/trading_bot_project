from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bot.journal import Journal
from bot.risk import fire_halt, fire_kill, operator_reset, update_day_pnl, update_position
from bot.shapes import Fill, OrderIntent, PaperEvidence, Position, RiskLimits, RiskState
from bot.venue import TradierVenue

live_enabled = False


class LiveDisabledError(Exception):
    pass


def check_promotion(evidence: PaperEvidence) -> str:
    if evidence.clean_days < evidence.clean_days_target:
        return "stay_paper"
    if evidence.max_drawdown > evidence.drawdown_bound:
        return "stay_paper"
    if not evidence.halt_drills_passed:
        return "stay_paper"
    return "ready_for_checklist"


def enable_live(signature: str, state: RiskState, journal: Journal, now: datetime) -> bool:
    global live_enabled
    if not signature or state.kill_active:
        return False
    live_enabled = True
    journal.append(now, "live_enabled", {"signature": signature})
    return True


def kill_switch(state: RiskState, journal: Journal, now: datetime) -> RiskState:
    global live_enabled
    live_enabled = False
    journal.append(now, "kill_fired", {})
    return fire_kill(state)


def reset_autonomy(state: RiskState, journal: Journal, now: datetime) -> RiskState:
    journal.append(now, "operator_reset", {})
    return operator_reset(state)


def submit_live(
    intent: OrderIntent,
    state: Optional[RiskState] = None,
    position: Optional[Position] = None,
    limits: Optional[RiskLimits] = None,
    journal: Optional[Journal] = None,
    now: Optional[datetime] = None,
    venue: Optional[TradierVenue] = None,
    reference_price: float = 0.0,
) -> tuple[Fill, Optional[Position], Optional[RiskState]]:
    if not live_enabled:
        raise LiveDisabledError("live adapter is disabled")
    if state is not None and (state.halt_active or state.kill_active):
        if journal is not None and now is not None:
            journal.append(now, "live_blocked", {"reason": "halt active" if state.halt_active else "kill active"})
        raise LiveDisabledError("live ordering is halted")
    active_venue = venue or TradierVenue.from_env()
    ack = active_venue.place_order(intent, reference_price)
    moment = now or datetime.now(timezone.utc)
    fill = Fill(price=ack.fill_price or 0.0, size=intent.size, timestamp_utc=moment, venue="live")
    if journal is not None:
        journal.append(
            moment,
            "live_fill",
            {"order_id": ack.order_id, "price": fill.price, "size": fill.size, "venue": "live"},
        )
    if state is None or limits is None:
        return fill, position, state
    updated_position = update_position(position, intent, fill)
    updated_state = update_day_pnl(state, updated_position, fill.price)
    if updated_state.day_pnl <= -limits.max_day_loss:
        updated_state = fire_halt(updated_state)
        if journal is not None:
            journal.append(moment, "halt", {"day_pnl": updated_state.day_pnl})
    return fill, updated_position, updated_state
