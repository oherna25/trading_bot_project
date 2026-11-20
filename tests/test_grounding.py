import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.journal import Journal
from bot.models import select_backend
from bot.research import handle_chat_query
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
FULL_ENV = {"ANTHROPIC_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}


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


def _responses_body(text):
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }


def test_grounding_appends_note_and_receipt():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"),
                              _responses_body("No contradiction found.")])
    journal = Journal()
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                  live_models=True, grounding=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis.\nGrounded note: No contradiction found."
    grounding = [e for e in journal.entries() if e.kind == "model_receipt" and e.payload["role_name"] == "grounding"]
    assert len(grounding) == 1
    assert grounding[0].payload["cost_usd"] == pytest.approx(50 / 1e6 * 4.00 + 10 / 1e6 * 20.00)


def test_grounding_uses_final_message_text():
    body = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "I will search now."}]},
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "No contradiction found."}]},
        ],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"), body])
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), Journal(), NOW,
                                  live_models=True, grounding=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis.\nGrounded note: No contradiction found."


def test_grounding_failure_skips_silently():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8"),
                              RuntimeError("search down")])
    journal = Journal()
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                  live_models=True, grounding=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis."
    skipped = [e for e in journal.entries() if e.kind == "grounding_skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["reason"] == "RuntimeError"


def test_grounding_off_makes_no_extra_call():
    http_post = _strict_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8")])
    journal = Journal()
    _, signal = handle_chat_query("brief", "TEST", _seeded_store(), journal, NOW,
                                  live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis == "Model thesis."
    assert not any(e.kind == "grounding_skipped" for e in journal.entries())


def test_grounding_role_selects_model():
    assert select_backend("grounding", Journal(), NOW, FULL_ENV) == ("model", "")
