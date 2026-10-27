from datetime import datetime, timezone

import pytest

import bot.autonomy as autonomy
from bot.autonomy import LiveDisabledError, check_promotion, enable_live, kill_switch, reset_autonomy, submit_live
from bot.journal import Journal
from bot.risk import review_intent
from bot.shapes import OrderIntent, PaperEvidence, RiskLimits, RiskState

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)


@pytest.fixture(autouse=True)
def _live_off():
    autonomy.live_enabled = False
    yield
    autonomy.live_enabled = False


def _state(**overrides) -> RiskState:
    base = {"account_size": 100000.0, "risk_per_trade": 0.01, "day_pnl": 0.0,
            "halt_active": False, "kill_active": False}
    base.update(overrides)
    return RiskState(**base)


def _intent() -> OrderIntent:
    return OrderIntent(symbol="TEST", side="buy", size=1000.0,
                       risk_numbers={"size": 1000.0}, signal_id="sig-1")


def test_live_adapter_ships_disabled():
    with pytest.raises(LiveDisabledError):
        submit_live(_intent())


def test_promotion_stays_paper_until_all_evidence_passes():
    short_run = PaperEvidence(clean_days=3, clean_days_target=10, max_drawdown=100.0,
                              drawdown_bound=500.0, halt_drills_passed=True)
    assert check_promotion(short_run) == "stay_paper"
    deep_drawdown = PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=800.0,
                                  drawdown_bound=500.0, halt_drills_passed=True)
    assert check_promotion(deep_drawdown) == "stay_paper"
    no_drills = PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=100.0,
                              drawdown_bound=500.0, halt_drills_passed=False)
    assert check_promotion(no_drills) == "stay_paper"
    ready = PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=100.0,
                          drawdown_bound=500.0, halt_drills_passed=True)
    assert check_promotion(ready) == "ready_for_checklist"


def test_enable_live_needs_signature_and_clean_state():
    journal = Journal()
    assert enable_live("", _state(), journal, NOW) is False
    assert enable_live("op-1", _state(kill_active=True), journal, NOW) is False
    with pytest.raises(LiveDisabledError):
        submit_live(_intent())


def test_enable_live_with_signature_arms_adapter():
    journal = Journal()
    assert enable_live("op-1", _state(), journal, NOW) is True
    assert journal.entries()[-1].kind == "live_enabled"
    with pytest.raises(NotImplementedError):
        submit_live(_intent())


def test_kill_switch_halts_and_needs_operator_reset():
    journal = Journal()
    enable_live("op-1", _state(), journal, NOW)
    halted = kill_switch(_state(), journal, NOW)
    assert halted.kill_active
    assert halted.halt_active
    assert journal.entries()[-1].kind == "kill_fired"
    with pytest.raises(LiveDisabledError):
        submit_live(_intent())
    assert review_intent(_intent(), halted, LIMITS, journal, NOW) == "rejected"
    cleared = reset_autonomy(halted, journal, NOW)
    assert not cleared.kill_active
    assert not cleared.halt_active
    assert review_intent(_intent(), cleared, LIMITS, journal, NOW) == "approved"
