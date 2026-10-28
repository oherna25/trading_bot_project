from datetime import datetime, timezone

from bot.journal import Journal
from bot.research import ideas_response, route_chat
from bot.risk import build_intent, even_legs, review_intent
from bot.shapes import OperatorConfig, OperatorProfile, RiskLimits, RiskState, Signal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    dashboard_base_url="https://ops.example.internal",
    dashboard_link_expiry_seconds=3600,
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)

LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)

OPT_IN = "profile: risk_tolerance=moderate, portfolio_size=50000, knowledge=intermediate, options_ok=yes"
OPT_OUT = "profile: risk_tolerance=moderate, portfolio_size=50000, knowledge=intermediate, options_ok=no"


def _state() -> RiskState:
    return RiskState(account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0,
                     halt_active=False, kill_active=False)


def _signal() -> Signal:
    return Signal(direction="long", thesis="flip holds", confidence=0.6, snapshot_id="TEST:snap-1")


def _spread():
    return even_legs(
        1000.0,
        [
            ("TEST", "buy", 100.0, "2026-11-20", "call"),
            ("TEST", "sell", 105.0, "2026-11-20", "call"),
        ],
    )


def test_route_chat_saves_profile_to_store():
    from bot.store import SnapshotStore

    store = SnapshotStore()
    route_chat(OPT_IN, "TEST", store, Journal(), CONFIG, NOW)
    assert store.read_profile() == OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    )


def test_options_intent_vetoed_without_saved_profile():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, _spread())
    assert review_intent(intent, _state(), LIMITS, journal, NOW) == "rejected"
    assert journal.entries()[-1].kind == "intent_vetoed"
    assert journal.entries()[-1].payload["reason"] == "options blocked by profile"


def test_options_intent_vetoed_when_profile_opts_out():
    from bot.store import SnapshotStore

    store = SnapshotStore()
    route_chat(OPT_OUT, "TEST", store, Journal(), CONFIG, NOW)
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, _spread())
    assert review_intent(intent, _state(), LIMITS, journal, NOW, store.read_profile()) == "rejected"
    assert journal.entries()[-1].payload["reason"] == "options blocked by profile"


def test_options_intent_approved_when_profile_opts_in():
    from bot.store import SnapshotStore

    store = SnapshotStore()
    route_chat(OPT_IN, "TEST", store, Journal(), CONFIG, NOW)
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW, _spread())
    assert review_intent(intent, _state(), LIMITS, journal, NOW, store.read_profile()) == "approved"


def test_stock_intent_ignores_missing_profile():
    journal = Journal()
    intent = build_intent(_signal(), "sig-1", _state(), journal, NOW)
    assert review_intent(intent, _state(), LIMITS, journal, NOW) == "approved"


def test_ideas_blocked_when_profile_opts_out():
    from bot.store import SnapshotStore

    store = SnapshotStore()
    journal = Journal()
    route_chat(OPT_OUT, "TEST", store, journal, CONFIG, NOW)
    reply = ideas_response("trade ideas", "TEST", store, journal, NOW)
    assert reply == "options ideas are disabled by your saved profile"
    assert journal.entries()[-1].kind == "ideas_blocked"


def test_ideas_flow_without_profile():
    from bot.feeds import default_context, ingest_bars, ingest_chain
    from bot.store import SnapshotStore

    store, journal = SnapshotStore(), Journal()
    ctx = default_context("TEST")
    ingest_chain("tradier_sandbox", store, journal, ctx)
    ingest_bars("alpaca", store, journal, ctx)
    reply = ideas_response("trade ideas", "TEST", store, journal, NOW)
    assert reply.startswith("trade ideas")
