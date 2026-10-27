from dataclasses import replace
from datetime import datetime, timezone

from bot.journal import Journal
from bot.risk import build_intent, review_intent, submit_paper
from bot.shapes import RiskLimits, RiskState, Signal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)


def _state(**overrides) -> RiskState:
    base = {"account_size": 100000.0, "risk_per_trade": 0.01, "day_pnl": 0.0,
            "halt_active": False, "kill_active": False}
    base.update(overrides)
    return RiskState(**base)


def _signal() -> Signal:
    return Signal(direction="long", thesis="flip holds as support", confidence=0.6, snapshot_id="TEST:snap-1")


def test_build_intent_sizes_from_account_and_risk():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    assert intent.size == 1000.0
    assert intent.symbol == "TEST"
    assert intent.side == "buy"
    assert intent.risk_numbers == {"account_size": 100000.0, "risk_per_trade": 0.01, "size": 1000.0}
    assert journal.entries()[-1].kind == "intent_proposed"


def test_review_approves_clean_intent():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    assert review_intent(intent, _state(), LIMITS, journal, NOW) == "approved"
    assert journal.entries()[-1].kind == "intent_approved"


def test_review_rejects_bad_schema_with_veto():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    bad = replace(intent, size=0.0)
    assert review_intent(bad, _state(), LIMITS, journal, NOW) == "rejected"
    assert journal.entries()[-1].kind == "intent_vetoed"


def test_review_blocks_intent_while_halted():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    assert review_intent(intent, _state(halt_active=True), LIMITS, journal, NOW) == "rejected"
    assert journal.entries()[-1].kind == "intent_blocked"


def test_paper_fill_reconciles_with_intent():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW)
    fill, position, updated = submit_paper(intent, 100.0, 100.0, state, None, LIMITS, journal, NOW)
    assert fill.venue == "paper"
    assert fill.size == intent.size == 1000.0
    assert position.net_size == 1000.0
    assert position.average_price == 100.0
    assert updated.day_pnl == 0.0
    assert not updated.halt_active


def test_day_loss_breach_halts_and_blocks_further_intents():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW)
    _, _, updated = submit_paper(intent, 100.0, 80.0, state, None, LIMITS, journal, NOW)
    assert updated.day_pnl == -20000.0
    assert updated.halt_active
    assert journal.entries()[-1].kind == "halt"
    next_intent = build_intent(_signal(), "sig-2", updated, journal, NOW)
    assert review_intent(next_intent, updated, LIMITS, journal, NOW) == "rejected"
