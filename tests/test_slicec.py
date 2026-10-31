from datetime import datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.muse import TOOL_REGISTRY, dispatch_tool, route_http
from bot.research import route_chat
from bot.shapes import Bar, ChainSnapshot, OperatorProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


@pytest.fixture(autouse=True)
def _no_reason_backend(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})


def _contract(strike=100.0, side="call", oi=1000.0, gamma=0.05):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _seeded_store(stamp=NOW, closes=(102.0,)) -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=stamp, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                _contract(100.0, "call", 1000.0, 0.05),
                _contract(100.0, "put", 2000.0, 0.03),
            ),
        )
    )
    base = stamp - timedelta(hours=len(closes))
    for n, close in enumerate(closes):
        store.save_bar(
            "TEST",
            Bar(interval="1d", timestamp_utc=base + timedelta(hours=n + 1), open=close,
                high=close, low=close, close=close, volume=1000.0),
        )
    store.save_profile(OperatorProfile(risk_tolerance="moderate", portfolio_size=50000.0,
                                       knowledge="intermediate", options_ok=True))
    return store


def test_parse_break_rule_reads_symbol_and_level():
    from bot.customalerts import parse_alert_rule

    rule = parse_alert_rule("alert me if SPY breaks 750")
    assert rule["symbol"] == "SPY"
    assert rule["kind"] == "break"
    assert rule["level"] == pytest.approx(750.0)
    assert rule["direction"] == "either"


def test_parse_break_rule_reads_direction():
    from bot.customalerts import parse_alert_rule

    assert parse_alert_rule("alert me if SPY breaks above 750")["direction"] == "above"
    assert parse_alert_rule("alert me if SPY breaks below 750")["direction"] == "below"


def test_parse_flip_rule_reads_symbol():
    from bot.customalerts import parse_alert_rule

    rule = parse_alert_rule("if RKLB crosses its flip")
    assert rule["symbol"] == "RKLB"
    assert rule["kind"] == "flip_cross"
    assert rule["level"] is None


def test_parse_rule_rejects_noise_loudly():
    from bot.customalerts import parse_alert_rule

    with pytest.raises(ValueError) as exc:
        parse_alert_rule("alert me if stocks feel funny")
    assert "next:" in str(exc.value)


def test_alert_rule_tools_set_list_and_cancel():
    store = _seeded_store()
    journal = Journal()
    made = dispatch_tool("set_alert_rule", {"text": "alert me if TEST breaks 750"},
                         store, journal, CONFIG, NOW, underlying="TEST")
    assert made.ok
    assert "rule 1" in made.reply
    assert "next:" in made.reply
    listed = dispatch_tool("list_alert_rules", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert listed.ok
    assert "TEST" in listed.reply and "750" in listed.reply
    gone = dispatch_tool("cancel_alert_rule", {"rule_id": "1"}, store, journal, CONFIG, NOW,
                         underlying="TEST")
    assert gone.ok
    assert "cancelled" in gone.reply
    listed = dispatch_tool("list_alert_rules", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert "no alert rules" in listed.reply


def test_cancel_unknown_rule_names_next_step():
    reply = dispatch_tool("cancel_alert_rule", {"rule_id": "9"}, _seeded_store(), Journal(),
                          CONFIG, NOW, underlying="TEST")
    assert reply.ok
    assert "no rule 9" in reply.reply
    assert "next:" in reply.reply


def test_chat_sets_lists_and_cancels_rules():
    store = _seeded_store()
    journal = Journal()
    assert "rule 1" in route_chat("alert me if TEST breaks 750", "TEST", store, journal, CONFIG, NOW)
    assert "750" in route_chat("list alert rules", "TEST", store, journal, CONFIG, NOW)
    assert "cancelled" in route_chat("cancel alert rule 1", "TEST", store, journal, CONFIG, NOW)
    assert "no alert rules" in route_chat("list alert rules", "TEST", store, journal, CONFIG, NOW)


def test_alert_rules_survive_a_bundle_round_trip(tmp_path):
    from bot.persist import load_bundle, save_bundle

    store = _seeded_store()
    journal = Journal()
    route_chat("alert me if TEST breaks 750", "TEST", store, journal, CONFIG, NOW)
    save_bundle(str(tmp_path), store, journal)
    fresh, _ = load_bundle(str(tmp_path))
    rules = fresh.list_alert_rules()
    assert len(rules) == 1
    assert rules[0]["symbol"] == "TEST"
    assert rules[0]["level"] == pytest.approx(750.0)


def test_break_rule_fires_once_bars_cross_the_level():
    from bot.customalerts import evaluate_alert_rules

    store = _seeded_store(stamp=NOW, closes=(100.0, 102.0))
    journal = Journal()
    route_chat("alert me if TEST breaks 101", "TEST", store, journal, CONFIG, NOW)
    alerts = evaluate_alert_rules(store, journal, NOW, "TEST")
    assert len(alerts) == 1
    assert "TEST" in alerts[0] and "101" in alerts[0] and "rule 1" in alerts[0]
    assert journal.entries()[-1].kind == "alert"


def test_break_rule_honors_direction():
    from bot.customalerts import evaluate_alert_rules

    store = _seeded_store(stamp=NOW, closes=(102.0, 100.0))
    journal = Journal()
    route_chat("alert me if TEST breaks above 101", "TEST", store, journal, CONFIG, NOW)
    assert evaluate_alert_rules(store, journal, NOW, "TEST") == []


def test_flip_rule_fires_when_spot_crosses_the_flip():
    from bot.customalerts import evaluate_alert_rules
    from bot.exposure import compute_gex

    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST", timestamp_utc=NOW, source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(
                _contract(95.0, "put", 3000.0, 0.05),
                _contract(105.0, "call", 3000.0, 0.05),
            ),
        )
    )
    snapshot_id, snapshot = store.read_latest("TEST")
    flip = compute_gex(snapshot, snapshot_id, spot=100.0).flip_point
    assert flip == pytest.approx(105.0)
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(hours=1),
                               open=104.0, high=104.0, low=104.0, close=104.0, volume=500.0))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=106.0, high=106.0, low=106.0, close=106.0, volume=500.0))
    journal = Journal()
    route_chat("alert me if TEST crosses its flip", "TEST", store, journal, CONFIG, NOW)
    alerts = evaluate_alert_rules(store, journal, NOW, "TEST")
    assert len(alerts) == 1
    assert "flip" in alerts[0] and "rule 1" in alerts[0]


def test_quiet_bars_fire_no_custom_alerts():
    from bot.customalerts import evaluate_alert_rules

    store = _seeded_store(stamp=NOW, closes=(100.0, 100.5))
    journal = Journal()
    route_chat("alert me if TEST breaks 750", "TEST", store, journal, CONFIG, NOW)
    assert evaluate_alert_rules(store, journal, NOW, "TEST") == []


def test_custom_alerts_flow_through_push_plumbing():
    from bot.alerts import push_alerts

    calls = []

    def stub(url, headers, payload, timeout):
        calls.append((url, payload))
        return {}

    store = _seeded_store(stamp=NOW, closes=(100.0, 102.0))
    journal = Journal()
    route_chat("alert me if TEST breaks 101", "TEST", store, journal, CONFIG, NOW)
    env = {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"}
    pushed = push_alerts("TEST", store, journal, NOW, env, stub)
    assert any("rule 1" in line for line in pushed)
    assert calls != []
    assert any(entry.kind == "alert_sent" and "rule 1" in str(entry.payload.get("text"))
               for entry in journal.entries())


def test_refresh_evaluates_rules_on_stored_bars():
    from bot.schedule import refresh_core

    def _boom(symbol):
        raise RuntimeError("feed down")

    store = _seeded_store(stamp=NOW, closes=(100.0, 102.0))
    journal = Journal()
    route_chat("alert me if TEST breaks 101", "TEST", store, journal, CONFIG, NOW)
    refresh_core("TEST", store, journal, NOW, context_factory=_boom)
    assert any(entry.kind == "alert" and "rule 1" in str(entry.payload.get("text"))
               for entry in journal.entries())


def test_open_brief_covers_regime_book_earnings_and_video():
    from bot.holdings import save_holdings_shapes
    from bot.morning import generate_open_brief

    store = _seeded_store(stamp=NOW - timedelta(hours=7))
    journal = Journal()
    save_holdings_shapes([{"symbol": "TEST", "kind": "stock", "side": "long", "qty": 100}],
                         store, journal, NOW)
    store.save_earnings("TEST", NOW.date().isoformat())
    text = generate_open_brief(store, journal, NOW, underlying="TEST")
    assert "open brief" in text
    assert "gamma" in text
    assert "100" in text and "flip" in text
    assert "today" in text and "TEST" in text
    assert "why:" in text
    assert "video:" in text and "scenes" in text
    assert "$" in text


def test_open_brief_empty_store_names_next_step():
    from bot.morning import generate_open_brief

    journal = Journal()
    text = generate_open_brief(SnapshotStore(), journal, NOW, underlying="TEST")
    assert "next:" in text
    assert journal.entries() == []


def test_brief_due_fires_at_925_et_on_weekdays():
    from bot.morning import brief_due

    assert brief_due(datetime(2026, 10, 27, 13, 25, tzinfo=timezone.utc), Journal()) is True
    assert brief_due(datetime(2026, 10, 27, 13, 24, tzinfo=timezone.utc), Journal()) is False
    assert brief_due(datetime(2026, 10, 24, 13, 25, tzinfo=timezone.utc), Journal()) is False


def test_brief_due_fires_once_per_day():
    from bot.morning import brief_due, generate_open_brief

    journal = Journal()
    moment = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
    assert brief_due(moment, journal) is True
    generate_open_brief(_seeded_store(), journal, moment, underlying="TEST")
    assert brief_due(moment, journal) is False


def test_open_brief_pushes_through_providers():
    from bot.morning import maybe_send_open_brief

    calls = []

    def stub(url, headers, payload, timeout):
        calls.append((url, payload))
        return {}

    text = maybe_send_open_brief(
        _seeded_store(), Journal(), NOW, {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"},
        stub, underlying="TEST",
    )
    assert text is not None and "open brief" in text
    assert calls != []


def test_session_hooks_open_brief_once_per_day(tmp_path):
    from bot.feeds import default_context
    from bot.schedule import run_session_core

    store, journal = SnapshotStore(), Journal()
    first = run_session_core("TEST", store, journal, str(tmp_path), NOW,
                             context_factory=default_context)
    assert "open brief" in first
    second = run_session_core("TEST", store, journal, str(tmp_path), NOW,
                              context_factory=default_context)
    assert "open brief" not in second


def test_chat_answers_morning_brief():
    text = route_chat("morning brief", "TEST", _seeded_store(), Journal(), CONFIG, NOW)
    assert "open brief" in text


def test_board_live_fragment_carries_badges_and_thresholds():
    store = _seeded_store(stamp=NOW - timedelta(hours=7), closes=(100.0,))
    old_bar = store.read_bars("TEST")[0]
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(hours=2),
                               open=100.0, high=101.0, low=99.0, close=100.5, volume=500.0))
    assert old_bar is not None
    response = route_http("GET", "/board/live", {}, store, Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "<table>" in body and "TEST" in body
    assert "tradier_sandbox" in body
    assert "quote" in body
    assert 'class="fresh"' in body or 'class="stale"' in body
    assert "5m" in body and "6h" in body


def test_fresh_and_stale_quotes_badge_green_and_amber():
    from bot.charts import live_board_fragment

    fresh = _seeded_store(stamp=NOW, closes=(102.0,))
    assert 'class="fresh"' in live_board_fragment(fresh, NOW)
    stale = SnapshotStore()
    stale.save(
        ChainSnapshot(
            underlying="OLD", timestamp_utc=NOW - timedelta(hours=7),
            source_name="tradier_sandbox", latency_ms=9.0,
            contracts_list=(_contract(),),
        )
    )
    stale.save_bar("OLD", Bar(interval="1d", timestamp_utc=NOW - timedelta(hours=2),
                              open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0))
    assert 'class="stale"' in live_board_fragment(stale, NOW)


def test_board_page_polls_the_live_fragment():
    response = route_http("GET", "/board", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert 'id="live-board"' in body
    assert "/board/live" in body
    assert "setInterval" in body and "5000" in body


def test_live_board_tool_matches_fragment():
    reply = dispatch_tool("get_live_board", {}, _seeded_store(), Journal(), CONFIG, NOW,
                          underlying="TEST")
    assert reply.ok
    assert "live board" in reply.reply
    assert "TEST" in reply.reply and "quote" in reply.reply


def test_dashboard_links_and_forms_cover_slice_c():
    response = route_http("GET", "/dashboard?underlying=TEST", {}, _seeded_store(),
                          Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert 'value="set_alert_rule"' in body
    assert 'value="cancel_alert_rule"' in body
    assert "tool=list_alert_rules" in body
    assert "tool=get_open_brief" in body
    assert "tool=get_live_board" in body


def test_dashboard_forms_drive_alert_rules_and_brief():
    store = _seeded_store()
    journal = Journal()
    made = route_http("POST", "/action",
                      {"tool": "set_alert_rule", "underlying": "TEST",
                       "text": "alert me if TEST breaks 750"},
                      store, journal, CONFIG, NOW)
    assert made.status == 200
    assert "rule 1" in made.body.decode("utf-8")
    listed = route_http("GET", "/action?tool=list_alert_rules&underlying=TEST", {}, store,
                        journal, CONFIG, NOW)
    assert listed.status == 200
    assert "750" in listed.body.decode("utf-8")
    denied = route_http("GET", "/action?tool=cancel_alert_rule&underlying=TEST&rule_id=1", {},
                        store, journal, CONFIG, NOW)
    assert denied.status == 400
    brief = route_http("GET", "/action?tool=get_open_brief&underlying=TEST", {}, store,
                       journal, CONFIG, NOW)
    assert brief.status == 200
    assert "open brief" in brief.body.decode("utf-8")
    live = route_http("GET", "/action?tool=get_live_board&underlying=TEST", {}, store,
                      journal, CONFIG, NOW)
    assert live.status == 200
    assert "live board" in live.body.decode("utf-8")


def test_registries_pin_new_tools_without_new_model():
    from bot.models import MODEL_REGISTRY

    for name in ("set_alert_rule", "list_alert_rules", "cancel_alert_rule",
                 "get_open_brief", "get_live_board"):
        assert name in TOOL_REGISTRY
    assert set(MODEL_REGISTRY) == {"analyst", "reviewer", "transcriber", "artist", "grounding"}
