import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.models import MODEL_REGISTRY, prompt_hash, select_backend, spend_today
from bot.research import analyst_backend, handle_chat_query
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
FULL_ENV = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}


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


def _snapshot_parts(store):
    snapshot_id, snapshot = store.read_latest("TEST")
    profile = compute_gex(snapshot, snapshot_id)
    return snapshot_id, snapshot, profile, store.read_latest_bar("TEST")


def _canned_post(bodies):
    log = []

    def post(url, headers, payload, timeout):
        log.append(payload)
        item = bodies[min(len(log) - 1, len(bodies) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    post.log = log
    return post


def _tool_leg(underlying="TEST", name="get_gex_profile"):
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": name, "arguments": json.dumps({"underlying": underlying})},
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


def test_registry_pins_models_and_fallbacks():
    assert MODEL_REGISTRY["analyst"].model_id == "muse-spark-1.3"
    assert set(MODEL_REGISTRY) == {"analyst", "reviewer", "transcriber", "artist", "grounding"}
    assert all(spec.fallback for spec in MODEL_REGISTRY.values())


def test_select_backend_unknown_role_falls_back():
    backend, reason = select_backend("nope", Journal(), NOW, FULL_ENV)
    assert backend == "fallback"
    assert reason


def test_select_backend_missing_key_falls_back():
    backend, reason = select_backend("analyst", Journal(), NOW, {})
    assert backend == "fallback"
    assert reason == "missing META_MODEL_API_KEY"


def test_select_backend_missing_budget_falls_back():
    backend, reason = select_backend("analyst", Journal(), NOW, {"META_MODEL_API_KEY": "k"})
    assert backend == "fallback"
    assert reason == "missing MODEL_DAY_BUDGET_USD"


def test_select_backend_spent_budget_falls_back():
    journal = Journal()
    journal.append(NOW, "model_receipt", {"cost_usd": 5.0})
    backend, reason = select_backend("analyst", journal, NOW, FULL_ENV)
    assert backend == "fallback"
    assert reason == "day budget spent"


def test_select_backend_ok_when_configured():
    assert select_backend("analyst", Journal(), NOW, FULL_ENV) == ("model", "")


def test_spend_today_sums_only_today_receipts():
    journal = Journal()
    journal.append(NOW, "model_receipt", {"cost_usd": 1.5})
    journal.append(NOW, "model_receipt", {"cost_usd": 0.5})
    journal.append(NOW - timedelta(days=1), "model_receipt", {"cost_usd": 100.0})
    journal.append(NOW, "signal", {"cost_usd": 100.0})
    assert spend_today(journal, NOW) == pytest.approx(2.0)


def test_prompt_hash_is_stable_hex():
    assert prompt_hash("abc") == prompt_hash("abc")
    assert len(prompt_hash("abc")) == 64
    assert prompt_hash("abc") != prompt_hash("abd")


def test_backend_runs_tool_loop_and_journals_receipts():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_tool_leg(), _final_leg("Short gamma thesis.\nDIRECTION: short\nCONFIDENCE: 0.7")])
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.direction == "short"
    assert signal.confidence == pytest.approx(0.7)
    assert signal.thesis == "Short gamma thesis."
    assert signal.snapshot_id == snapshot_id
    receipts = [e for e in journal.entries() if e.kind == "model_receipt"]
    assert len(receipts) == 2
    assert receipts[0].payload["model_id"] == "muse-spark-1.3"
    assert receipts[0].payload["cost_usd"] == pytest.approx(100 / 1e6 * 1.25 + 20 / 1e6 * 4.25)
    assert len(receipts[0].payload["prompt_hash"]) == 64
    assert receipts[0].payload["snapshot_id"] == snapshot_id
    tool_messages = [m for m in http_post.log[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["total_exposure"] == pytest.approx(-1000.0)


def test_backend_missing_markers_use_flip_defaults():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_final_leg("A plain thesis with no markers.")])
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, Journal(), NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.direction == "flat"
    assert signal.confidence == pytest.approx(0.5)
    assert signal.thesis == "A plain thesis with no markers."


def test_backend_bad_direction_and_big_confidence_clamp():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_final_leg("Thesis.\nDIRECTION: sideways\nCONFIDENCE: 9")])
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, Journal(), NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.direction == "flat"
    assert signal.confidence == pytest.approx(1.0)


def test_backend_unknown_tool_falls_back():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_tool_leg(name="nope_tool")])
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis.startswith("analyst read on TEST")
    assert journal.entries()[-1].kind == "model_fallback"
    assert journal.entries()[-1].payload["reason"] == "ValueError"


def test_backend_empty_thesis_falls_back():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_final_leg("DIRECTION: long\nCONFIDENCE: 0.9")])
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis.startswith("analyst read on TEST")
    assert journal.entries()[-1].payload["reason"] == "ValueError"


def test_backend_missing_key_falls_back_without_network():
    def explode(*args, **kwargs):
        raise AssertionError("no http without a key")

    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env={}, http_post=explode)
    assert signal.thesis.startswith("analyst read on TEST")
    assert journal.entries()[-1].payload["reason"] == "missing META_MODEL_API_KEY"


def test_backend_http_error_falls_back_without_leaking_key():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    env = {"META_MODEL_API_KEY": "secret-key-123", "MODEL_DAY_BUDGET_USD": "5"}
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env=env, http_post=_canned_post([RuntimeError("down")]))
    assert signal.thesis.startswith("analyst read on TEST")
    assert journal.entries()[-1].payload["reason"] == "RuntimeError"
    assert "secret" not in json.dumps([e.payload for e in journal.entries()])


def test_backend_defaults_to_offline_even_with_key():
    def explode(*args, **kwargs):
        raise AssertionError("offline path must not call http")

    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             env=FULL_ENV, http_post=explode)
    assert signal.thesis.startswith("analyst read on TEST")
    assert journal.entries()[-1].payload["reason"] == "live models off"


def test_live_brief_numbers_match_offline_numbers():
    offline_reply, _ = handle_chat_query("brief", "TEST", _seeded_store(), Journal(), NOW)
    live_post = _canned_post([_tool_leg(), _final_leg("Model thesis.\nDIRECTION: short\nCONFIDENCE: 0.8")])
    live_journal = Journal()
    live_reply, live_signal = handle_chat_query("brief", "TEST", _seeded_store(), live_journal, NOW,
                                                live_models=True, env=FULL_ENV, http_post=live_post)
    for line in ("total exposure: $-104,040", "walls: 100 $-104,040"):
        assert line in offline_reply
        assert line in live_reply
    assert live_signal.thesis == "Model thesis."
    assert [e.kind for e in live_journal.entries()].count("model_receipt") == 2
    assert any(e.kind == "signal" for e in live_journal.entries())


def test_backend_empty_final_round_falls_back():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    first = _tool_leg()
    first["choices"][0]["message"]["content"] = "I will pull the profile."
    http_post = _canned_post([first, _final_leg("")])
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.thesis.startswith("analyst read on TEST")
    assert journal.entries()[-1].payload["reason"] == "ValueError"
