import json
from datetime import datetime, timezone

from bot.journal import Journal
from bot.research import ideas_response, route_chat
from bot.risk import build_intent, even_legs, review_intent
from bot.shapes import OperatorConfig, OperatorProfile, RiskLimits, RiskState, Signal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
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


PROFILE_REQUIRED = "trade cards need a saved operator profile; reply profile for the questionnaire"


def _snapshotted_store():
    from bot.feeds import default_context, ingest_bars, ingest_chain
    from bot.store import SnapshotStore

    store, journal = SnapshotStore(), Journal()
    ctx = default_context("TEST")
    ingest_chain("tradier_sandbox", store, journal, ctx)
    ingest_bars("alpaca", store, journal, ctx)
    return store


def test_trade_cards_blocked_without_profile():
    from bot.tradecards import query_trade_cards

    journal = Journal()
    assert query_trade_cards("trade cards", "TEST", _snapshotted_store(), journal, NOW) is None
    assert journal.entries()[-1].kind == "ideas_blocked"
    assert journal.entries()[-1].payload["reason"] == "profile required"


def test_cards_response_requires_profile():
    from bot.tradecards import cards_response

    reply = cards_response("trade cards", "TEST", _snapshotted_store(), Journal(), NOW)
    assert reply == PROFILE_REQUIRED


def test_cards_still_blocked_when_profile_opts_out():
    from bot.tradecards import cards_response

    store = _snapshotted_store()
    journal = Journal()
    route_chat(OPT_OUT, "TEST", store, journal, CONFIG, NOW)
    assert cards_response("trade cards", "TEST", store, journal, NOW) == (
        "options ideas are disabled by your saved profile"
    )


def test_trade_cards_flow_with_profile():
    from bot.tradecards import cards_response

    store = _snapshotted_store()
    journal = Journal()
    route_chat(OPT_IN, "TEST", store, journal, CONFIG, NOW)
    reply = cards_response("trade cards", "TEST", store, journal, NOW)
    assert reply.startswith("trade cards")
    assert journal.entries()[-1].kind == "ideas"


def test_card_routes_require_profile():
    from bot.muse import route_http
    from bot.shapes import default_operator_config

    config = default_operator_config()
    page = route_http("GET", "/cards?underlying=TEST", {}, _snapshotted_store(), Journal(), config, NOW)
    assert page.status == 403
    assert PROFILE_REQUIRED in page.body.decode("utf-8")
    doc = route_http("GET", "/cards.json?underlying=TEST", {}, _snapshotted_store(), Journal(), config, NOW)
    assert doc.status == 403
    assert PROFILE_REQUIRED in json.loads(doc.body)["error"]
    card = route_http("GET", "/card?underlying=TEST&index=0", {}, _snapshotted_store(), Journal(), config, NOW)
    assert card.status == 403
    assert PROFILE_REQUIRED in card.body.decode("utf-8")


def test_shock_debate_demo_dashboard_require_profile():
    from bot.dashboard import dashboard_html
    from bot.debate import card_debate
    from bot.demo import demo_html
    from bot.shock import shock_text

    assert PROFILE_REQUIRED in shock_text("TEST", _snapshotted_store(), Journal(), NOW, -2.0)
    assert card_debate("TEST", _snapshotted_store(), Journal(), NOW) == PROFILE_REQUIRED
    assert PROFILE_REQUIRED in demo_html("TEST", _snapshotted_store(), Journal(), NOW)
    assert PROFILE_REQUIRED in dashboard_html(_snapshotted_store(), Journal(), NOW, "TEST")
