import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.cards import render_brief_card
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.research import handle_chat_query
from bot.shapes import Bar, ChainSnapshot, OptionContract, Signal
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
FULL_ENV = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


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


def _parts(store):
    snapshot_id, snapshot = store.read_latest("TEST")
    profile = compute_gex(snapshot, snapshot_id)
    signal = Signal(direction="short", thesis="t", confidence=0.7, snapshot_id=snapshot_id, trigger="flip 100")
    return snapshot_id, snapshot, profile, signal


def _strict_post(bodies):
    log = []

    def post(url, headers, payload, timeout):
        log.append(payload)
        if len(log) > len(bodies):
            raise AssertionError("unexpected extra http call")
        item = bodies[len(log) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    post.log = log
    return post


def _tool_leg():
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "get_gex_profile", "arguments": json.dumps({"underlying": "TEST"})},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _final_leg(content):
    return {
        "choices": [{"message": {"content": content, "tool_calls": []}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
    }


def test_render_card_returns_art_with_engine_numbers():
    store = _seeded_store()
    snapshot_id, snapshot, profile, signal = _parts(store)
    http_post = _strict_post([{"data": [{"b64_json": PNG_B64}]}])
    journal = Journal()
    card = render_brief_card(signal, snapshot, profile, journal, NOW, snapshot_id, FULL_ENV, http_post)
    assert card["art"] == base64.b64decode(PNG_B64)
    assert card["regime"] == "short-gamma"
    assert card["numbers"]["total_exposure"] == pytest.approx(profile.total_exposure)
    assert card["numbers"]["flip_point"] == profile.flip_point
    assert card["numbers"]["direction"] == "short"
    prompt = http_post.log[0]["prompt"]
    assert "short-gamma" in prompt
    assert "no text" in prompt
    receipts = [e for e in journal.entries() if e.kind == "model_receipt"]
    assert len(receipts) == 1
    assert receipts[0].payload["role_name"] == "artist"
    assert receipts[0].payload["cost_usd"] == pytest.approx(0.01)
    brief_card = next(e for e in journal.entries() if e.kind == "brief_card")
    assert brief_card.payload["numbers"]["total_exposure"] == pytest.approx(-1000.0)
    assert brief_card.payload["snapshot_id"] == snapshot_id


def test_render_card_accepts_url_form():
    store = _seeded_store()
    snapshot_id, snapshot, profile, signal = _parts(store)
    card = render_brief_card(signal, snapshot, profile, Journal(), NOW, snapshot_id, FULL_ENV,
                             _strict_post([{"data": [{"url": "https://img.example/card.png"}]}]))
    assert card["art"] is None
    assert card["url"] == "https://img.example/card.png"


def test_render_card_empty_result_skips():
    store = _seeded_store()
    snapshot_id, snapshot, profile, signal = _parts(store)
    journal = Journal()
    assert render_brief_card(signal, snapshot, profile, journal, NOW, snapshot_id, FULL_ENV,
                             _strict_post([{"data": []}])) is None
    assert journal.entries()[-1].kind == "card_skipped"


def test_render_card_missing_key_skips_without_network():
    def explode(*args, **kwargs):
        raise AssertionError("no key means no http")

    store = _seeded_store()
    snapshot_id, snapshot, profile, signal = _parts(store)
    journal = Journal()
    assert render_brief_card(signal, snapshot, profile, journal, NOW, snapshot_id, {}, explode) is None
    assert journal.entries()[-1].payload["reason"] == "missing META_MODEL_API_KEY"


def test_render_card_http_error_skips():
    store = _seeded_store()
    snapshot_id, snapshot, profile, signal = _parts(store)
    journal = Journal()
    assert render_brief_card(signal, snapshot, profile, journal, NOW, snapshot_id, FULL_ENV,
                             _strict_post([RuntimeError("down")])) is None
    assert journal.entries()[-1].payload["reason"] == "RuntimeError"


def test_chat_with_cards_appends_card_line():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"),
                              {"data": [{"b64_json": PNG_B64}]}])
    journal = Journal()
    reply, _ = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                 live_models=True, cards=True, env=FULL_ENV, http_post=http_post)
    assert "total exposure: $-104,040" in reply
    assert "\ncard: TEST:" in reply
    assert "short-gamma" in reply
    assert any(e.kind == "brief_card" for e in journal.entries())


def test_chat_without_cards_has_no_card_line():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8")])
    journal = Journal()
    reply, _ = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                 live_models=True, env=FULL_ENV, http_post=http_post)
    assert "\ncard: " not in reply
    assert not any(e.kind == "brief_card" for e in journal.entries())
