import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.muse import MuseChatRequest, dispatch_tool, handle_chat, llm_route, parse_chat_payload
from bot.shapes import Bar, ChainSnapshot, OperatorConfig, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0)
ENV = {"META_MODEL_API_KEY": "key-1"}


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
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=100.0, high=103.0, low=99.0, close=102.0, volume=1200.0))
    return store


def _stub(content):
    def post(url, headers, payload, timeout):
        assert payload["model"] == "muse-spark-1.3"
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    return post


def test_llm_route_parses_tool_json():
    journal = Journal()
    name, args, symbol = llm_route(
        "show me trade cards", "TEST", journal, NOW, dict(ENV),
        _stub('{"tool": "get_trade_cards", "arguments": {"query": "monthly"}, "underlying": "test"}'),
    )
    assert (name, args, symbol) == ("get_trade_cards", {"query": "monthly"}, "TEST")
    assert journal.entries()[-1].kind == "model_receipt"


def test_llm_route_parses_fenced_json_and_defaults_underlying():
    name, args, symbol = llm_route(
        "pnl?", "TEST", Journal(), NOW, dict(ENV),
        _stub('```json\n{"tool": "paper_pnl", "arguments": {}}\n```'),
    )
    assert (name, args, symbol) == ("paper_pnl", {}, "TEST")


def test_llm_route_rejects_garbage_unknown_tool_and_missing_key():
    with pytest.raises(ValueError, match="no JSON"):
        llm_route("hi", "TEST", Journal(), NOW, dict(ENV), _stub("just prose"))
    with pytest.raises(ValueError, match="unknown tool"):
        llm_route("hi", "TEST", Journal(), NOW, dict(ENV), _stub('{"tool": "nope"}'))

    def exploding(*args):
        raise AssertionError("must not call http without a key")

    with pytest.raises(ValueError, match="META_MODEL_API_KEY"):
        llm_route("hi", "TEST", Journal(), NOW, {}, exploding)


def test_handle_chat_llm_router_dispatches_tool(monkeypatch):
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("bot.metareason._read_dotenv", lambda *args, **kwargs: {})
    journal = Journal()
    request = MuseChatRequest(text="anything at all", underlying="TEST", llm_router=True)
    reply = handle_chat(
        request, _seeded_store(), journal, CONFIG, NOW, dict(ENV),
        _stub('{"tool": "get_trade_cards", "arguments": {"query": "trade cards"}}'),
    )
    assert reply.ok is True
    assert reply.reply.startswith("trade cards")
    assert any(e.kind == "llm_routed" for e in journal.entries())


def test_handle_chat_llm_router_failure_falls_back_to_regex():
    def failing(*args):
        raise RuntimeError("down")

    journal = Journal()
    request = MuseChatRequest(text="gex brief", underlying="TEST", llm_router=True)
    reply = handle_chat(request, _seeded_store(), journal, CONFIG, NOW, dict(ENV), failing)
    assert reply.ok is True
    assert "total exposure:" in reply.reply
    assert any(e.kind == "llm_route_failed" for e in journal.entries())


def test_parse_chat_payload_carries_llm_router_flag():
    assert parse_chat_payload({"text": "hi", "llm_router": True}).llm_router is True
    assert parse_chat_payload({"text": "hi"}).llm_router is False


def test_questionnaire_keyword_returns_questionnaire():
    reply = dispatch_tool("get_gex_brief", {}, _seeded_store(), Journal(), CONFIG, NOW, underlying="TEST")
    assert reply.ok is True
    from bot.research import route_chat

    assert "risk tolerance" in route_chat("questionnaire", "TEST", _seeded_store(), Journal(), CONFIG, NOW).lower()
