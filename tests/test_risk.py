from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.risk import (
    build_intent,
    drawdown_paused,
    positions_capped,
    review_intent,
    sector_capped,
    submit_paper,
)
from bot.shapes import PortfolioView, RiskLimits, RiskState, Signal

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
    fill, position, updated = submit_paper(intent, 100.0, state, None, LIMITS, journal, NOW)
    assert fill.venue == "paper"
    assert fill.size == intent.size == 1000.0
    assert position.net_size == 1000.0
    assert position.average_price == 100.0
    assert position.unrealized_pnl == 0.0
    assert updated.day_pnl == 0.0
    assert not updated.halt_active


def test_day_loss_breach_halts_and_blocks_further_intents():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW)
    _, _, updated = submit_paper(intent, 80.0, state, None, LIMITS, journal, NOW, ask=100.0)
    assert updated.day_pnl == -20000.0
    assert updated.halt_active
    assert journal.entries()[-1].kind == "halt"
    next_intent = build_intent(_signal(), "sig-2", updated, journal, NOW)
    assert review_intent(next_intent, updated, LIMITS, journal, NOW) == "rejected"


def test_flat_signal_produces_no_intent():
    journal = Journal()
    flat = Signal(direction="flat", thesis="no read", confidence=0.1, snapshot_id="TEST:snap-1")
    assert build_intent(flat, "sig-1", _state(), journal, NOW) is None
    assert journal.entries()[-1].kind == "intent_skipped"


def test_options_sizing_uses_max_loss_per_contract():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, max_loss_per_contract=250.0)
    assert intent.risk_numbers["contracts"] == 4.0
    assert intent.size == 1000.0
    small = build_intent(_signal(), "sig-2", _state(), journal, NOW, max_loss_per_contract=300.0)
    assert small.risk_numbers["contracts"] == 3.0
    assert small.size == 900.0


def test_paper_fill_uses_touch_plus_slippage_and_fees():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW)
    fill, position, updated = submit_paper(
        intent, 100.0, state, None, LIMITS, journal, NOW, bid=99.9, ask=100.1,
        slippage_bps=10.0, fees=5.0,
    )
    assert fill.price == pytest.approx(100.1 * 1.001)
    assert position.average_price == pytest.approx(100.1 * 1.001)
    assert position.unrealized_pnl == pytest.approx((100.0 - 100.1 * 1.001) * 1000.0)
    assert updated.day_pnl == pytest.approx((100.0 - 100.1 * 1.001) * 1000.0 - 5.0)
    assert not updated.halt_active


def test_closing_fill_books_realized_pnl():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW)
    _, position, updated = submit_paper(intent, 100.0, state, None, LIMITS, journal, NOW)
    assert updated.day_pnl == 0.0
    sell = replace(intent, side="sell")
    _, closed, final = submit_paper(sell, 110.0, updated, position, LIMITS, journal, NOW)
    assert closed.net_size == 0.0
    assert final.day_pnl == 10000.0


def test_zero_caps_with_view_preserve_approval():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    assert review_intent(intent, _state(), LIMITS, journal, NOW, view=PortfolioView()) == "approved"


def test_positions_cap_vetoes_at_cap():
    limits = replace(LIMITS, max_positions=2)
    assert positions_capped(PortfolioView(open_positions=2), limits) is True
    assert positions_capped(PortfolioView(open_positions=1), limits) is False
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    view = PortfolioView(open_positions=2, open_symbols=("AAPL", "JPM"))
    assert review_intent(intent, _state(), limits, journal, NOW, view=view) == "rejected"
    assert journal.entries()[-1].payload == {"reason": "max positions reached"}


def test_drawdown_pause_math():
    limits = replace(LIMITS, drawdown_pause_pct=0.10)
    view = PortfolioView(peak_day_pnl=1000.0)
    assert drawdown_paused(800.0, view, limits) is True
    assert drawdown_paused(950.0, view, limits) is False
    assert drawdown_paused(-5000.0, PortfolioView(), limits) is False
    assert drawdown_paused(-5000.0, PortfolioView(peak_day_pnl=0.0), limits) is False
    assert drawdown_paused(-5000.0, PortfolioView(peak_day_pnl=-100.0), limits) is False
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(day_pnl=800.0), journal, NOW)
    assert review_intent(intent, _state(day_pnl=800.0), limits, journal, NOW, view=view) == "rejected"
    assert journal.entries()[-1].payload == {"reason": "drawdown pause active"}


def test_sector_cap_vetoes_known_sector_only():
    limits = replace(LIMITS, max_per_sector=1)
    view = PortfolioView(open_positions=1, open_symbols=("AAPL",))
    assert sector_capped(view, "MSFT", limits) is True
    assert sector_capped(view, "JPM", limits) is False
    assert sector_capped(view, "ZZZZ", limits) is False
    journal = Journal()
    tech = Signal(direction="long", thesis="t", confidence=0.6, snapshot_id="MSFT:snap-1")
    intent = build_intent(tech, "sig-1", _state(), journal, NOW)
    assert review_intent(intent, _state(), limits, journal, NOW, view=view) == "rejected"
    assert journal.entries()[-1].payload == {"reason": "sector cap reached"}
    unknown = Signal(direction="long", thesis="t", confidence=0.6, snapshot_id="ZZZZ:snap-1")
    intent = build_intent(unknown, "sig-2", _state(), journal, NOW)
    assert review_intent(intent, _state(), limits, journal, NOW, view=view) == "approved"


def test_day_pnl_accumulates_across_fills():
    journal = Journal()
    state = _state()
    intent = build_intent(_signal(), "sig-1", state, journal, NOW)
    _, position, updated = submit_paper(intent, 100.0, state, None, LIMITS, journal, NOW)
    assert updated.day_pnl == 0.0
    _, position2, updated2 = submit_paper(intent, 101.0, updated, position, LIMITS, journal, NOW)
    assert position2.unrealized_pnl == 1000.0
    assert updated2.day_pnl == 1000.0
