import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.muse import (
    Harness,
    dispatch_tool,
    handle_chat,
    openapi_spec,
    parse_chat_payload,
    route_http,
    TOOL_REGISTRY,
)
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OperatorProfile, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="call", open_interest=1000.0,
                    gamma=0.05, price=2.50, bid=2.40, ask=2.60, iv=0.30,
                ),
                OptionContract(
                    strike=100.0, expiry="2026-11-20", call_or_put="put", open_interest=2000.0,
                    gamma=0.03, price=3.00, bid=2.90, ask=3.10, iv=0.32,
                ),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW - timedelta(days=1),
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    store.save_profile(OperatorProfile(
        risk_tolerance="moderate", portfolio_size=50000.0, knowledge="intermediate", options_ok=True
    ))
    return store


def test_chat_brief_carries_profile_numbers():
    store = _seeded_store()
    request = parse_chat_payload({"text": "give me the gex brief", "underlying": "TEST"})
    reply = handle_chat(request, store, Journal(), CONFIG, NOW)
    assert reply.ok is True
    assert reply.error == ""
    assert "total exposure: $-104,040" in reply.reply
    assert "strike 100.0:" not in reply.reply
    assert "direction: short confidence: 0.50 (regime fallback)" in reply.reply


def test_trade_cards_text_returns_cards_with_reasons(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    store = _seeded_store()
    request = parse_chat_payload({"text": "show me trade cards", "underlying": "TEST"})
    reply = handle_chat(request, store, Journal(), CONFIG, NOW)
    assert reply.ok is True
    assert reply.reply.startswith("trade cards (2):")
    assert "short via" in reply.reply


def test_blank_text_payload_raises():
    with pytest.raises(ValueError):
        parse_chat_payload({"text": "   ", "underlying": "TEST"})
    with pytest.raises(ValueError):
        parse_chat_payload({"underlying": "TEST"})


def test_missing_snapshot_yields_error_naming_underlying():
    request = parse_chat_payload({"text": "gex brief", "underlying": "MISSING"})
    reply = handle_chat(request, SnapshotStore(), Journal(), CONFIG, NOW)
    assert reply.ok is False
    assert reply.reply == ""
    assert "MISSING" in reply.error


def test_unknown_tool_names_tool():
    reply = dispatch_tool("nope_missing", {}, _seeded_store(), Journal(), CONFIG, NOW, underlying="TEST")
    assert reply.ok is False
    assert "nope_missing" in reply.error


def test_registry_tools_answer(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    assert set(TOOL_REGISTRY) == {
        "get_gex_brief", "get_trade_cards", "get_trade_ideas", "get_wheel_ideas",
        "get_vex_brief", "get_dex_brief", "journal_tail", "paper_pnl", "run_session", "get_iv_rank", "run_replay",
        "backfill_iv", "snapshot_diff", "shock_test", "set_holdings", "debate_card",
        "get_profile", "get_dashboard", "get_alerts", "get_holdings",
    }
    store = _seeded_store()
    journal = Journal()
    brief = dispatch_tool("get_gex_brief", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert brief.ok is True
    assert "total exposure: $-104,040" in brief.reply
    cards = dispatch_tool("get_trade_cards", {"query": "monthly trade cards"}, store, journal, CONFIG, NOW, underlying="TEST")
    assert cards.ok is True
    assert cards.reply.startswith("trade cards")
    assert "short via" in cards.reply
    ideas = dispatch_tool("get_trade_ideas", {"query": "weekly trade ideas"}, store, journal, CONFIG, NOW, underlying="TEST")
    assert ideas.ok is True
    assert "trade ideas" in ideas.reply
    wheel = dispatch_tool("get_wheel_ideas", {"query": "wheel ideas"}, store, journal, CONFIG, NOW, underlying="TEST")
    assert wheel.ok is True
    assert wheel.reply.startswith("wheel ideas")
    vex = dispatch_tool("get_vex_brief", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert vex.ok is True
    assert "VEX brief" in vex.reply
    dex = dispatch_tool("get_dex_brief", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert dex.ok is True
    assert "DEX brief" in dex.reply
    tail = dispatch_tool("journal_tail", {"count": 2}, store, journal, CONFIG, NOW, underlying="TEST")
    assert tail.ok is True
    assert "journal tail" in tail.reply
    pnl = dispatch_tool("paper_pnl", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert pnl.ok is True
    assert "paper pnl" in pnl.reply
    backfill = dispatch_tool("backfill_iv", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert backfill.ok is True
    assert backfill.reply.startswith("backfilled IV")
    diff = dispatch_tool("snapshot_diff", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert diff.ok is True
    assert "diff TEST" in diff.reply
    shock = dispatch_tool("shock_test", {"shock_pct": -2.0}, store, journal, CONFIG, NOW, underlying="TEST")
    assert shock.ok is True
    assert "TEST shock -2.0%" in shock.reply
    holdings = dispatch_tool("set_holdings", {"holdings": []}, store, journal, CONFIG, NOW, underlying="TEST")
    assert holdings.ok is True
    assert holdings.reply == "holdings cleared"
    debate = dispatch_tool("debate_card", {"index": 0}, store, journal, CONFIG, NOW, underlying="TEST")
    assert debate.ok is True
    assert "BULL [rules]:" in debate.reply
    empty_profile = dispatch_tool("get_profile", {}, SnapshotStore(), journal, CONFIG, NOW, underlying="TEST")
    assert empty_profile.ok is True
    assert "no profile is saved" in empty_profile.reply
    dashboard = dispatch_tool("get_dashboard", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert dashboard.ok is True
    assert "dashboard" in dashboard.reply
    assert "TEST:" in dashboard.reply
    alerts = dispatch_tool("get_alerts", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert alerts.ok is True
    assert "alerts: none" in alerts.reply
    holdings_read = dispatch_tool("get_holdings", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert holdings_read.ok is True
    assert holdings_read.reply.startswith("holdings: none saved")


def test_openapi_spec_lists_both_paths():
    spec = openapi_spec()
    assert spec["openapi"] == "3.0.0"
    assert spec["info"]["title"]
    assert "/chat" in spec["paths"]
    assert "/mcp" in spec["paths"]


def _json_body(response):
    assert response.content_type == "application/json"
    return json.loads(response.body.decode("utf-8"))


def test_route_chat_round_trip_carries_profile_numbers():
    response = route_http(
        "POST", "/chat",
        {"text": "give me the gex brief", "underlying": "TEST"},
        _seeded_store(), Journal(), CONFIG, NOW,
    )
    assert response.status == 200
    body = _json_body(response)
    assert body["ok"] is True
    assert "total exposure: $-104,040" in body["reply"]


def test_route_chat_rejects_non_dict_and_blank_payloads():
    response = route_http("POST", "/chat", ["not", "a", "dict"], _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 400
    assert _json_body(response)["ok"] is False
    response = route_http("POST", "/chat", {"text": "   ", "underlying": "TEST"}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 400
    assert _json_body(response)["ok"] is False


def test_route_mcp_tools_list_names_registry():
    response = route_http("POST", "/mcp", {"method": "tools/list"}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    body = _json_body(response)
    assert {tool["name"] for tool in body["tools"]} == {
        "get_gex_brief", "get_trade_cards", "get_trade_ideas", "get_wheel_ideas",
        "get_vex_brief", "get_dex_brief", "journal_tail", "paper_pnl", "run_session", "get_iv_rank", "run_replay",
        "backfill_iv", "snapshot_diff", "shock_test", "set_holdings", "debate_card",
        "get_profile", "get_dashboard", "get_alerts", "get_holdings",
    }


def test_route_mcp_tools_call_trade_cards(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    payload = {"method": "tools/call", "params": {"name": "get_trade_cards", "arguments": {"query": "trade cards"}, "underlying": "TEST"}}
    response = route_http("POST", "/mcp", payload, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    body = _json_body(response)
    assert body["ok"] is True
    assert body["reply"].startswith("trade cards")
    assert "short via" in body["reply"]


def test_route_mcp_unknown_method_names_it():
    response = route_http("POST", "/mcp", {"method": "tools/nope"}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 400
    assert "tools/nope" in _json_body(response)["error"]


def test_route_http_enforces_bearer_token_when_set():
    store = _seeded_store()
    journal = Journal()
    denied = route_http(
        "POST", "/chat", {"text": "gex brief", "underlying": "TEST"},
        store, journal, CONFIG, NOW, auth_token="secret",
    )
    assert denied.status == 401
    wrong = route_http(
        "POST", "/chat", {"text": "gex brief", "underlying": "TEST"},
        store, journal, CONFIG, NOW, auth_token="secret", authorization="Bearer nope",
    )
    assert wrong.status == 401
    allowed = route_http(
        "POST", "/chat", {"text": "gex brief", "underlying": "TEST"},
        store, journal, CONFIG, NOW, auth_token="secret", authorization="Bearer secret",
    )
    assert allowed.status == 200
    assert _json_body(allowed)["ok"] is True


def test_health_endpoints_stay_open_behind_auth():
    store = _seeded_store()
    journal = Journal()
    for path in ("/healthz", "/health", "/openapi.json"):
        response = route_http("GET", path, {}, store, journal, CONFIG, NOW, auth_token="secret")
        assert response.status == 200


def test_health_reports_last_ingest_and_feed():
    store = _seeded_store()
    journal = Journal()
    journal.append(NOW - timedelta(minutes=5), "snapshot_ingested",
                   {"snapshot_id": "TEST:snap-1", "source_name": "marketdata_eod"})
    journal.append(NOW - timedelta(minutes=4), "bars_ingested",
                   {"source_name": "alpaca", "underlying": "TEST", "count": 2})
    response = route_http("GET", "/health", {}, store, journal, CONFIG, NOW)
    assert response.status == 200
    body = _json_body(response)
    assert body["ok"] is True
    assert body["feed_used"] == "alpaca"
    assert body["last_ingest_utc"] == (NOW - timedelta(minutes=4)).isoformat()
    assert body["snapshot_age_seconds"] == pytest.approx(240.0)
    assert body["quote_age_seconds"] == {"TEST": pytest.approx(0.0)}
    assert body["underlyings"] == ["TEST"]


def test_iv_rank_tool_reports_histories():
    store = _seeded_store()
    for day in range(12):
        store.save_iv("TEST", f"2026-09-{day + 1:02d}", 0.0)
    for day in range(12):
        store.save_iv("TEST", f"2026-10-{day + 1:02d}", 100.0)
    store.save_iv("TEST", "2026-10-27", 50.0)
    reply = dispatch_tool("get_iv_rank", {}, store, Journal(), CONFIG, NOW, underlying="TEST")
    assert reply.ok is True
    assert reply.reply == "iv ranks:\nTEST: 50.0 (25 points)"


def test_iv_rank_tool_leads_with_requested_underlying():
    store = SnapshotStore()
    for symbol in ("AAA", "ZZZ"):
        store.save(
            ChainSnapshot(
                underlying=symbol, timestamp_utc=NOW, source_name="seed", latency_ms=1.0,
                contracts_list=(),
            )
        )
    for day in range(25):
        store.save_iv("AAA", f"2026-10-{day + 1:02d}", 10.0)
        store.save_iv("ZZZ", f"2026-10-{day + 1:02d}", 90.0)
    reply = dispatch_tool("get_iv_rank", {}, store, Journal(), CONFIG, NOW, underlying="ZZZ")
    assert reply.reply.splitlines()[1].startswith("ZZZ:")
    reply2 = dispatch_tool("get_iv_rank", {}, store, Journal(), CONFIG, NOW, underlying="AAA")
    assert reply2.reply.splitlines()[1].startswith("AAA:")


def test_iv_rank_tool_empty_store():
    reply = dispatch_tool("get_iv_rank", {}, SnapshotStore(), Journal(), CONFIG, NOW)
    assert reply.reply == "iv ranks: none stored"


def test_route_unknown_path_returns_404():
    response = route_http("GET", "/missing", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 404
    response = route_http("POST", "/missing", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 404


def test_harness_routes_text_voice_cards_push(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    harness = Harness()
    harness.post("give me the gex brief", kind="text")
    harness.post("give me the gex brief", kind="voice_transcript")
    harness.post("show me trade cards", kind="text")
    harness.run_once()
    harness.push_scheduled()
    assert len(harness.outbox) == 4
    assert [entry["kind"] for entry in harness.outbox] == ["text", "text", "text", "push"]
    assert "total exposure:" in harness.outbox[0]["body"]
    assert "total exposure:" in harness.outbox[1]["body"]
    assert harness.outbox[2]["body"].startswith("trade cards")
    assert "total exposure:" in harness.outbox[3]["body"]
