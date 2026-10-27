from __future__ import annotations

from datetime import datetime

from bot.journal import Journal
from bot.risk import fire_kill, operator_reset
from bot.shapes import OrderIntent, PaperEvidence, RiskState

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


def submit_live(intent: OrderIntent) -> None:
    if not live_enabled:
        raise LiveDisabledError("live adapter is disabled")
    raise NotImplementedError("live venue lands in Phase 4")
