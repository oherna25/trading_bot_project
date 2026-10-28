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
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    dashboard_base_url="https://ops.example.internal",
    dashboard_link_expiry_seconds=3600,
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
    return store


def test_chat_brief_carries_profile_numbers():
    store = _seeded_store()
    request = parse_chat_payload({"text": "give me the gex brief", "underlying": "TEST"})
    reply = handle_chat(request, store, Journal(), CONFIG, NOW)
    assert reply.ok is True
    assert reply.error == ""
    assert "total exposure: -1000.0" in reply.reply
    assert "strike 100.0: -1000.0" in reply.reply
    assert "direction: flat" in reply.reply


def test_dashboard_text_returns_pinned_link():
    store = _seeded_store()
    request = parse_chat_payload({"text": "show me the dashboard", "underlying": "TEST"})
    reply = handle_chat(request, store, Journal(), CONFIG, NOW)
    assert reply.ok is True
    assert reply.reply.startswith("dashboard for snapshot TEST:")
    assert "https://ops.example.internal/dashboard/TEST:" in reply.reply


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


def test_registry_tools_answer():
    assert set(TOOL_REGISTRY) == {"get_gex_brief", "get_dashboard_link", "get_trade_ideas"}
    store = _seeded_store()
    journal = Journal()
    brief = dispatch_tool("get_gex_brief", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert brief.ok is True
    assert "total exposure: -1000.0" in brief.reply
    link = dispatch_tool("get_dashboard_link", {}, store, journal, CONFIG, NOW, underlying="TEST")
    assert link.ok is True
    assert link.reply.startswith("dashboard for snapshot TEST:")
    assert "https://ops.example.internal/dashboard/TEST:" in link.reply
    ideas = dispatch_tool("get_trade_ideas", {"query": "weekly trade ideas"}, store, journal, CONFIG, NOW, underlying="TEST")
    assert ideas.ok is True
    assert "trade ideas" in ideas.reply


def test_openapi_spec_lists_both_paths():
    spec = openapi_spec()
    assert spec["openapi"] == "3.0.0"
    assert spec["info"]["title"]
    assert "/chat" in spec["paths"]
    assert "/mcp" in spec["paths"]


def test_route_chat_round_trip_carries_profile_numbers():
    status, body = route_http(
        "POST", "/chat",
        {"text": "give me the gex brief", "underlying": "TEST"},
        _seeded_store(), Journal(), CONFIG, NOW,
    )
    assert status == 200
    assert body["ok"] is True
    assert "total exposure: -1000.0" in body["reply"]


def test_route_chat_rejects_non_dict_and_blank_payloads():
    status, body = route_http("POST", "/chat", ["not", "a", "dict"], _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 400
    assert body["ok"] is False
    status, body = route_http("POST", "/chat", {"text": "   ", "underlying": "TEST"}, _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 400
    assert body["ok"] is False


def test_route_mcp_tools_list_names_registry():
    status, body = route_http("POST", "/mcp", {"method": "tools/list"}, _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 200
    assert {tool["name"] for tool in body["tools"]} == {"get_gex_brief", "get_dashboard_link", "get_trade_ideas"}


def test_route_mcp_tools_call_dashboard_link_pinned():
    payload = {"method": "tools/call", "params": {"name": "get_dashboard_link", "arguments": {}, "underlying": "TEST"}}
    status, body = route_http("POST", "/mcp", payload, _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 200
    assert body["ok"] is True
    assert body["reply"].startswith("dashboard for snapshot TEST:")
    assert "https://ops.example.internal/dashboard/TEST:" in body["reply"]


def test_route_mcp_unknown_method_names_it():
    status, body = route_http("POST", "/mcp", {"method": "tools/nope"}, _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 400
    assert "tools/nope" in body["error"]


def test_route_unknown_path_returns_404():
    status, _ = route_http("GET", "/missing", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 404
    status, _ = route_http("POST", "/missing", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert status == 404


def test_harness_routes_text_voice_dashboard_push():
    harness = Harness()
    harness.post("give me the gex brief", kind="text")
    harness.post("give me the gex brief", kind="voice_transcript")
    harness.post("show me the dashboard", kind="text")
    harness.run_once()
    harness.push_scheduled()
    assert len(harness.outbox) == 4
    assert [entry["kind"] for entry in harness.outbox] == ["text", "text", "link", "push"]
    assert "total exposure:" in harness.outbox[0]["body"]
    assert "total exposure:" in harness.outbox[1]["body"]
    assert harness.outbox[2]["body"].startswith("dashboard for snapshot")
    assert "total exposure:" in harness.outbox[3]["body"]
