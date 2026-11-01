from datetime import datetime, timezone

import pytest

import bot.autonomy as autonomy
from bot.autonomy import enable_live
from bot.journal import Journal
from bot.live_trade import (
    enable_live_ui,
    kill_live_ui,
    maybe_auto_live,
    reset_live_memory,
    take_live_trade,
)
from bot.muse import FormData, route_http
from bot.shapes import (
    Bar,
    ChainSnapshot,
    DashboardSettings,
    OperatorConfig,
    OperatorProfile,
    OptionContract,
    PaperEvidence,
    RiskState,
    dashboard_settings_from,
)
from bot.store import SnapshotStore
from bot.venue import LiveAck

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0)


class StubVenue:
    def __init__(self) -> None:
        self.calls: list = []

    def place_order(self, intent, reference_price):
        self.calls.append((intent, reference_price))
        return LiveAck(order_id="stub-1", status="filled", fill_price=reference_price, quantity=1)


def _seeded_store(threshold: float = 0.50, auto: bool = False) -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="call",
                               open_interest=1000.0, gamma=0.05, price=2.50,
                               bid=2.40, ask=2.60, iv=0.30),
                OptionContract(strike=100.0, expiry="2026-11-20", call_or_put="put",
                               open_interest=2000.0, gamma=0.03, price=3.00,
                               bid=2.90, ask=3.10, iv=0.32),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW, open=100.0, high=103.0,
                               low=99.0, close=102.0, volume=1200.0))
    store.save_profile(OperatorProfile(risk_tolerance="moderate", portfolio_size=50000.0,
                                       knowledge="intermediate", options_ok=True))
    store.save_settings(DashboardSettings(live_auto_send=auto, live_min_confidence=threshold))
    return store


def _state(**overrides) -> RiskState:
    base = {"account_size": 50000.0, "risk_per_trade": 0.01, "day_pnl": 0.0,
            "halt_active": False, "kill_active": False}
    base.update(overrides)
    return RiskState(**base)


def _ready_evidence() -> PaperEvidence:
    return PaperEvidence(clean_days=12, clean_days_target=10, max_drawdown=100.0,
                         drawdown_bound=500.0, halt_drills_passed=True)


def _checklist() -> dict[str, bool]:
    return {"paper_evidence_reviewed": True, "risk_limits_set": True, "kill_switch_tested": True}


@pytest.fixture(autouse=True)
def _clean_live():
    autonomy.live_enabled = False
    reset_live_memory()
    yield
    autonomy.live_enabled = False
    reset_live_memory()


def test_settings_new_fields_round_trip_and_validate():
    settings = dashboard_settings_from({"live_auto_send": "1", "live_min_confidence": "0.85"})
    assert settings.live_auto_send is True
    assert settings.live_min_confidence == pytest.approx(0.85)
    with pytest.raises(ValueError):
        dashboard_settings_from({"live_min_confidence": 2.0})
    with pytest.raises(ValueError):
        dashboard_settings_from({"live_min_confidence": "high"})


def test_take_live_below_threshold_skips_without_broker():
    store = _seeded_store(threshold=0.99)
    venue = StubVenue()
    text = take_live_trade(0, "TEST", store, Journal(), CONFIG, NOW, venue=venue)
    assert "skipped" in text and "threshold" in text
    assert venue.calls == []


def test_take_live_disarmed_never_reaches_broker():
    store = _seeded_store(threshold=0.10)
    venue = StubVenue()
    with pytest.raises(ValueError, match="rejected"):
        take_live_trade(0, "TEST", store, Journal(), CONFIG, NOW, venue=venue)
    assert venue.calls == []


def test_take_live_armed_fills_through_review_and_journals():
    store = _seeded_store(threshold=0.10)
    journal = Journal()
    assert enable_live("op-1", _state(), journal, NOW, _ready_evidence(), _checklist()) is True
    venue = StubVenue()
    text = take_live_trade(0, "TEST", store, journal, CONFIG, NOW, venue=venue)
    assert "filled" in text
    assert len(venue.calls) == 1
    assert any(e.kind == "live_fill" for e in journal.entries())


def test_kill_switch_blocks_aftermath():
    store = _seeded_store(threshold=0.10)
    journal = Journal()
    enable_live("op-1", _state(), journal, NOW, _ready_evidence(), _checklist())
    kill_live_ui(store, journal, NOW)
    venue = StubVenue()
    with pytest.raises(ValueError, match="rejected"):
        take_live_trade(0, "TEST", store, journal, CONFIG, NOW, venue=venue)
    assert venue.calls == []


def test_auto_hook_default_off_makes_no_broker_contact(monkeypatch):
    from bot.tradecards import query_trade_cards

    monkeypatch.setattr(
        "bot.venue.TradierVenue.from_env",
        classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("no broker"))),
    )
    store = _seeded_store(threshold=0.10, auto=False)
    journal = Journal()
    cards = query_trade_cards("trade ideas", "TEST", store, journal, NOW)
    assert cards
    assert not [e for e in journal.entries() if e.kind in ("live_fill", "live_auto", "live_blocked")]


def test_auto_sends_only_qualifying_cards_when_armed():
    store = _seeded_store(threshold=0.10, auto=True)
    journal = Journal()
    enable_live("op-1", _state(), journal, NOW, _ready_evidence(), _checklist())
    from bot.tradecards import query_trade_cards

    cards = query_trade_cards("trade ideas", "TEST", store, journal, NOW)
    assert cards
    venue = StubVenue()
    summary = maybe_auto_live(cards, store, journal, CONFIG, NOW, venue=venue)
    assert "fill" in summary
    assert len(venue.calls) >= 1


def test_live_page_toggle_and_gated_enable():
    store = _seeded_store()
    journal = Journal()
    page = route_http("GET", "/live", {}, store, journal, CONFIG, NOW)
    assert page.status == 200
    assert "live ordering" in page.body.decode("utf-8")
    bad = route_http("POST", "/live", FormData({"action": "enable"}), store, journal, CONFIG, NOW)
    assert bad.status == 400
    killed = route_http("POST", "/live", FormData({"action": "kill"}), store, journal, CONFIG, NOW)
    assert killed.status == 200
    assert autonomy.live_enabled is False


def test_enable_ui_needs_evidence():
    store = _seeded_store()
    with pytest.raises(ValueError, match="blocked"):
        enable_live_ui("op-1", _checklist(), store, Journal(), CONFIG, NOW)
