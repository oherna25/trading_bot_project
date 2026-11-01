import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.models import (
    MODEL_REGISTRY,
    chat_api_key,
    chat_base_url,
    chat_spec,
    prompt_hash,
    read_user_model,
    select_backend,
    spend_today,
)
from bot.research import analyst_backend, handle_chat_query
from bot.shapes import Bar, ChainSnapshot, OptionContract
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
FULL_ENV = {"META_MODEL_API_KEY": "test-key", "MODEL_DAY_BUDGET_USD": "5"}
USER_ENV = {"USER_MODEL_API_KEY": "user-secret-1", "USER_MODEL_BASE_URL": "https://llm.example/v1",
            "USER_MODEL_ID": "anything-llm"}


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


def test_select_backend_user_key_needs_no_budget():
    assert select_backend("analyst", Journal(), NOW, dict(USER_ENV)) == ("user", "")


def test_select_backend_user_key_wins_over_builtin():
    assert select_backend("analyst", Journal(), NOW, {**FULL_ENV, **USER_ENV}) == ("user", "")
    assert select_backend("reviewer", Journal(), NOW, {**FULL_ENV, **USER_ENV}) == ("user", "")


def test_select_backend_user_key_ignored_for_provider_roles():
    backend, reason = select_backend("grounding", Journal(), NOW, dict(USER_ENV))
    assert backend == "fallback"
    assert reason == "missing META_MODEL_API_KEY"


def test_select_backend_invalid_user_url_fails_loud():
    env = dict(USER_ENV, USER_MODEL_BASE_URL="notaurl")
    backend, reason = select_backend("analyst", Journal(), NOW, env)
    assert backend == "fallback"
    assert "USER_MODEL_BASE_URL" in reason


def test_runtime_user_model_wins_over_env():
    from bot.models import chat_api_key, read_runtime_user_model, select_backend, set_runtime_user_model
    from bot.shapes import UserModelConfig

    env = {"USER_MODEL_API_KEY": "env-key", "MODEL_DAY_BUDGET_USD": "5"}
    set_runtime_user_model(UserModelConfig(api_key="sess-key",
                                           base_url="https://llm.example/v1", model_id="sess-model"))
    try:
        assert select_backend("analyst", Journal(), NOW, env) == ("user", "")
        assert chat_api_key("analyst", env) == "sess-key"
        assert read_runtime_user_model() is not None
    finally:
        set_runtime_user_model(None)


def test_clear_runtime_falls_back_to_env():
    from bot.models import chat_api_key, set_runtime_user_model
    from bot.shapes import UserModelConfig

    env = {"USER_MODEL_API_KEY": "env-key", "MODEL_DAY_BUDGET_USD": "5"}
    set_runtime_user_model(UserModelConfig(api_key="sess-key",
                                           base_url="https://llm.example/v1", model_id="sess-model"))
    set_runtime_user_model(None)
    assert chat_api_key("analyst", env) == "env-key"


def test_read_user_model_defaults():
    config, error = read_user_model({"USER_MODEL_API_KEY": " k "})
    assert error == ""
    assert config is not None
    assert config.api_key == "k"
    assert config.base_url == "https://api.openai.com/v1"
    assert config.model_id == "gpt-4o-mini"


def test_read_user_model_absent_is_not_an_error():
    assert read_user_model({}) == (None, "")


def test_chat_spec_uses_user_model_with_zero_price():
    spec = chat_spec("analyst", dict(USER_ENV))
    assert spec.model_id == "anything-llm"
    assert spec.endpoint == "/chat/completions"
    assert spec.price_in == 0.0 and spec.price_out == 0.0
    assert chat_api_key("analyst", dict(USER_ENV)) == "user-secret-1"
    assert chat_base_url("analyst", dict(USER_ENV)) == "https://llm.example/v1"
    assert chat_spec("analyst", dict(FULL_ENV)).model_id == "muse-spark-1.3"
    assert chat_base_url("analyst", dict(FULL_ENV)) == "https://api.meta.ai/v1"


def test_analyst_backend_user_key_hits_user_endpoint():
    seen = {}

    def post(url, headers, payload, timeout):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        seen["model"] = payload["model"]
        return {
            "choices": [{"message": {"content": "User model thesis.\nDIRECTION: long\nCONFIDENCE: 0.6"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }

    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    journal = Journal()
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, journal, NOW,
                             live_models=True, env=dict(USER_ENV), http_post=post)
    assert signal.thesis == "User model thesis."
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer user-secret-1"
    assert seen["model"] == "anything-llm"
    receipts = [e for e in journal.entries() if e.kind == "model_receipt"]
    assert len(receipts) == 1
    assert receipts[0].payload["model_id"] == "anything-llm"
    assert receipts[0].payload["cost_usd"] == 0.0
    assert "user-secret-1" not in json.dumps([e.payload for e in journal.entries()])
    tiers = [e for e in journal.entries() if e.kind == "tier_selected"]
    assert tiers and tiers[0].payload["tier"] == "user"


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
    assert json.loads(tool_messages[0]["content"])["total_exposure"] == pytest.approx(-104040.0)


def test_backend_missing_markers_use_regime_fallback():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_final_leg("A plain thesis with no markers.")])
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, Journal(), NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.direction == "short"
    assert signal.confidence == pytest.approx(0.5)
    assert signal.thesis == "A plain thesis with no markers."


def test_backend_bad_direction_and_big_confidence_clamp():
    store = _seeded_store()
    snapshot_id, snapshot, profile, bar = _snapshot_parts(store)
    http_post = _canned_post([_final_leg("Thesis.\nDIRECTION: sideways\nCONFIDENCE: 9")])
    signal = analyst_backend("brief", snapshot_id, snapshot, profile, bar, Journal(), NOW,
                             live_models=True, env=FULL_ENV, http_post=http_post)
    assert signal.direction == "short"
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
    for line in ("total exposure: -$104K", "walls: 100 -$104K"):
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
