from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.risk import (
    build_intent,
    even_legs,
    intent_notional,
    is_options_intent,
    review_intent,
    submit_paper,
)
from bot.shapes import OrderLeg, RiskLimits, RiskState, Signal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)


def _state(**overrides) -> RiskState:
    base = {"account_size": 100000.0, "risk_per_trade": 0.01, "day_pnl": 0.0,
            "halt_active": False, "kill_active": False}
    base.update(overrides)
    return RiskState(**base)


def _signal() -> Signal:
    return Signal(direction="long", thesis="flip holds as support", confidence=0.6, snapshot_id="TEST:snap-1")


def _spread() -> tuple[OrderLeg, ...]:
    return even_legs(
        1000.0,
        [
            ("TEST", "buy", 100.0, "2026-11-20", "call"),
            ("TEST", "sell", 105.0, "2026-11-20", "call"),
        ],
    )


def test_even_legs_splits_size_evenly():
    legs = _spread()
    assert len(legs) == 2
    assert legs[0].size == pytest.approx(500.0)
    assert legs[1].size == pytest.approx(500.0)
    assert legs[0].strike == 100.0
    assert legs[1].side == "sell"


def test_even_legs_rejects_empty_specs():
    with pytest.raises(ValueError, match="at least one leg"):
        even_legs(1000.0, [])


def test_build_intent_with_legs_sums_notional():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, _spread())
    assert intent.size == pytest.approx(1000.0)
    assert intent_notional(intent) == pytest.approx(1000.0)
    assert intent.risk_numbers["legs"] == pytest.approx(2.0)
    assert len(intent.legs) == 2
    proposed = journal.entries()[-1]
    assert proposed.kind == "intent_proposed"
    assert len(proposed.payload["legs"]) == 2


def test_single_leg_intent_has_empty_legs():
    intent = build_intent(_signal(), "sig-1", _state(), Journal(), NOW)
    assert intent.legs == ()
    assert intent_notional(intent) == pytest.approx(1000.0)
    assert not is_options_intent(intent)


def test_stock_basket_is_not_options_intent():
    legs = even_legs(1000.0, [("AAA", "buy", None, "", ""), ("BBB", "buy", None, "", "")])
    intent = build_intent(_signal(), "sig-1", _state(), Journal(), NOW, legs)
    assert not is_options_intent(intent)
    assert review_intent(intent, _state(), LIMITS, Journal(), NOW) == "approved"


def test_spread_is_options_intent_and_passes_review():
    from bot.shapes import OperatorProfile

    profile = OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    )
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, _spread())
    assert is_options_intent(intent)
    assert review_intent(intent, _state(), LIMITS, journal, NOW, profile) == "approved"


def test_review_rejects_combo_above_trade_limit():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, _spread())
    tight = replace(LIMITS, max_trade_size=100.0)
    assert review_intent(intent, _state(), tight, journal, NOW) == "rejected"
    assert journal.entries()[-1].kind == "intent_vetoed"


@pytest.mark.parametrize(
    "leg",
    [
        OrderLeg(symbol="", side="buy", size=10.0),
        OrderLeg(symbol="TEST", side="hold", size=10.0),
        OrderLeg(symbol="TEST", side="buy", size=0.0),
        OrderLeg(symbol="TEST", side="buy", size=10.0, strike=100.0, expiry="2026-11-20", call_or_put="maybe"),
        OrderLeg(symbol="TEST", side="buy", size=10.0, expiry="2026-11-20", call_or_put="call"),
        OrderLeg(symbol="TEST", side="buy", size=10.0, strike=100.0, call_or_put="call"),
        OrderLeg(symbol="TEST", side="buy", size=10.0, strike=100.0, expiry="next friday", call_or_put="call"),
        OrderLeg(symbol="TEST", side="buy", size=10.0, strike=100.0),
    ],
)
def test_review_rejects_bad_leg_schema(leg):
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, (leg,))
    assert review_intent(intent, _state(), LIMITS, journal, NOW) == "rejected"
    assert journal.entries()[-1].kind == "intent_vetoed"


def test_paper_fill_journals_combo_legs():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW, _spread())
    fill, _, _ = submit_paper(intent, 2.5, state, None, LIMITS, journal, NOW)
    assert fill.venue == "paper"
    assert fill.size == pytest.approx(1000.0)
    entry = next(e for e in journal.entries() if e.kind == "fill")
    assert len(entry.payload["legs"]) == 2
    assert entry.payload["legs"][0]["strike"] == 100.0
