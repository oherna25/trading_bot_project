from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.muse import TOOL_REGISTRY, dispatch_tool, route_http
from bot.research import route_chat
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore
from bot.tradecards import TradeCard, trade_cards_html

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


def _seeded_store(profile=None) -> SnapshotStore:
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
    if profile is not None:
        store.save_profile(profile)
    return store


def _moderate():
    return OperatorProfile(risk_tolerance="moderate", portfolio_size=50000.0,
                           knowledge="intermediate", options_ok=True)


def _aggressive():
    return OperatorProfile(risk_tolerance="aggressive", portfolio_size=50000.0,
                           knowledge="intermediate", options_ok=True)


def _tiny():
    return OperatorProfile(risk_tolerance="moderate", portfolio_size=1000.0,
                           knowledge="beginner", options_ok=True)


def test_sized_cards_print_contracts_and_cap():
    from bot.tradecards import cards_response

    reply = cards_response("trade cards", "TEST", _seeded_store(_moderate()), Journal(), NOW)
    assert "contract" in reply
    assert "at risk" in reply
    assert "within your" in reply
    assert "$500" in reply
    assert "$" in reply


def test_sized_cards_print_three_when_affordable():
    from bot.tradecards import cards_response

    reply = cards_response("trade cards", "TEST", _seeded_store(_aggressive()), Journal(), NOW)
    assert "3 contracts" in reply
    assert "at risk" in reply
    assert "within your" in reply


def test_sized_cards_shrink_over_cap_and_say_so():
    from bot.tradecards import cards_response

    reply = cards_response("trade cards", "TEST", _seeded_store(_tiny()), Journal(), NOW)
    assert "contract" in reply
    assert "at risk" in reply
    assert "cap" in reply
    assert "shrink" in reply.lower() or "shrunk" in reply.lower()
    assert "over your" in reply


def test_sized_cards_no_profile_keeps_raw_plus_hint():
    card = TradeCard(
        symbol="TEST", direction="long", spread="single", strike=100.0,
        expiry="2026-11-20", call_or_put="call", hedge_strike=None,
        confidence=0.5, snapshot_id="TEST:x", max_loss=260.0,
        breakeven=102.6, net_debit=260.0, spread_pct=8.0,
        wall_exposure=5000.0, regime="long-gamma", spot=102.0,
        flip_point=99.0, reason="raw reason", reason_source="fallback",
    )
    page = trade_cards_html([card], "TEST")
    assert "raw reason" in page
    assert "save a profile for sizing" in page


def test_cards_page_shows_sizing_with_profile():
    response = route_http("GET", "/cards?underlying=TEST", {}, _seeded_store(_moderate()),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "contract" in body
    assert "at risk" in body
    assert "$500" in body


def test_explain_position_tool_weaves_all_sources():
    from bot.holdings import save_holdings_shapes

    store = _seeded_store(_moderate())
    journal = Journal()
    save_holdings_shapes([{"symbol": "TEST", "kind": "stock", "side": "long", "qty": 100}],
                         store, journal, NOW)
    reply = dispatch_tool("explain_position", {"symbol": "TEST"}, store, journal, CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok
    assert "\n" not in reply.reply
    lowered = reply.reply.lower()
    assert "100" in reply.reply and "test" in lowered
    assert "regime" in lowered or "brief" in lowered
    assert "wall" in lowered
    assert "wheel" in lowered
    assert "debate" in lowered or "verdict" in lowered or "bull" in lowered
    assert "$" in reply.reply
    assert [e for e in journal.entries() if e.kind == "position_explained"]


def test_explain_position_chat_handles_my_symbol():
    from bot.holdings import save_holdings_shapes

    store = _seeded_store(_moderate())
    journal = Journal()
    save_holdings_shapes([{"symbol": "TEST", "kind": "stock", "side": "long", "qty": 50}],
                         store, journal, NOW)
    reply = route_chat("explain my TEST", "TEST", store, journal, CONFIG, NOW)
    assert "50" in reply
    assert "wheel" in reply.lower()
    assert [e for e in journal.entries() if e.kind == "position_explained"]


def test_explain_position_needs_position_loudly():
    store = _seeded_store(_moderate())
    reply = dispatch_tool("explain_position", {"symbol": "TEST"}, store, Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok
    assert "no position" in reply.reply.lower()
    assert "next:" in reply.reply


def test_explain_dashboard_action_reaches_tool():
    from bot.holdings import save_holdings_shapes

    store = _seeded_store(_moderate())
    journal = Journal()
    save_holdings_shapes([{"symbol": "TEST", "kind": "stock", "side": "long", "qty": 10}],
                         store, journal, NOW)
    dash = route_http("GET", "/dashboard?underlying=TEST", {}, store, journal, CONFIG, NOW)
    assert dash.status == 200
    assert "explain_position" in dash.body.decode("utf-8")
    action = route_http("GET", "/action?tool=explain_position&underlying=TEST", {}, store,
                        journal, CONFIG, NOW)
    assert action.status == 200
    assert "wheel" in action.body.decode("utf-8").lower()


def test_onboarding_start_returns_first_question():
    reply = route_chat("start", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "risk_tolerance" in reply
    assert "next:" in reply


def test_onboarding_flow_saves_profile_and_first_card():
    store = _seeded_store()
    journal = Journal()
    assert "risk_tolerance" in route_chat("start", "TEST", store, journal, CONFIG, NOW)
    step2 = route_chat("moderate", "TEST", store, journal, CONFIG, NOW)
    assert "portfolio_size" in step2 and "next:" in step2
    step3 = route_chat("50000", "TEST", store, journal, CONFIG, NOW)
    assert "knowledge" in step3 and "next:" in step3
    step4 = route_chat("intermediate", "TEST", store, journal, CONFIG, NOW)
    assert "options_ok" in step4 and "next:" in step4
    final = route_chat("yes", "TEST", store, journal, CONFIG, NOW)
    assert "profile saved" in final.lower() or "risk_tolerance=moderate" in final
    assert "trade cards" in final.lower()
    assert "contract" in final.lower() and "at risk" in final.lower()
    assert store.read_profile() is not None
    assert store.read_profile().risk_tolerance == "moderate"


def test_onboarding_stages_survive_across_turns():
    store = _seeded_store()
    journal = Journal()
    route_chat("start", "TEST", store, journal, CONFIG, NOW)
    route_chat("moderate", "TEST", store, journal, CONFIG, NOW)
    assert [e for e in journal.entries() if e.kind == "onboarding_answer"]
    other = route_chat("gex brief", "TEST", store, journal, CONFIG, NOW)
    assert "GEX brief" in other
    step = route_chat("50000", "TEST", store, journal, CONFIG, NOW)
    assert "knowledge" in step


def test_onboarding_rejects_bad_answer_loudly():
    store = _seeded_store()
    journal = Journal()
    route_chat("start", "TEST", store, journal, CONFIG, NOW)
    reply = route_chat("reckless", "TEST", store, journal, CONFIG, NOW)
    assert "risk_tolerance" in reply
    assert "next:" in reply
    assert store.read_profile() is None


def test_onboarding_tool_mirrors_chat():
    store = _seeded_store()
    journal = Journal()
    first = dispatch_tool("onboarding_step", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert first.ok and "risk_tolerance" in first.reply and "next:" in first.reply
    second = dispatch_tool("onboarding_step", {"answer": "moderate"}, store, journal, CONFIG,
                           NOW, underlying="TEST")
    assert second.ok and "portfolio_size" in second.reply


def test_onboarding_dashboard_form_posts():
    dash = route_http("GET", "/dashboard?underlying=TEST", {}, _seeded_store(_moderate()),
                      Journal(), CONFIG, NOW)
    assert dash.status == 200
    assert "onboarding_step" in dash.body.decode("utf-8")
    store = _seeded_store()
    journal = Journal()
    response = route_http("POST", "/action",
                          {"tool": "onboarding_step", "underlying": "TEST", "answer": ""},
                          store, journal, CONFIG, NOW)
    assert response.status == 200
    assert "risk_tolerance" in response.body.decode("utf-8")


def test_registries_pin_new_tools_without_new_model():
    from bot.models import MODEL_REGISTRY

    assert "explain_position" in TOOL_REGISTRY
    assert "onboarding_step" in TOOL_REGISTRY
    assert set(MODEL_REGISTRY) == {"analyst", "reviewer", "transcriber", "artist", "grounding"}
