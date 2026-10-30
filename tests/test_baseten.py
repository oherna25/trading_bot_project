from datetime import datetime, timezone

import pytest

from bot.baseten import (
    INFERENCE_CHAT_URL,
    REASON_MODEL_DEFAULT,
    generate_reason,
    predict_url,
    select_reason_backend,
)
from bot.journal import Journal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

ENV = {"BASETEN_API_KEY": "key-1", "BASETEN_REASON_MODEL_ID": "model-1"}
SLUG = "meta-models/muse-spark-open-1026"
SLUG_ENV = {"BASETEN_API_KEY": "key-1", "BASETEN_REASON_MODEL_ID": SLUG}


def test_predict_url_builds_from_model_id():
    assert predict_url("abc123") == "https://model-abc123.api.baseten.co/environments/production/predict"


def test_predict_url_routes_model_slug_to_inference_api():
    assert predict_url(SLUG) == INFERENCE_CHAT_URL
    assert INFERENCE_CHAT_URL == "https://inference.baseten.co/v1/chat/completions"


def test_predict_url_uses_full_url_as_is():
    assert predict_url("https://example.internal/predict") == "https://example.internal/predict"
    assert predict_url("http://127.0.0.1:8000/predict") == "http://127.0.0.1:8000/predict"


def test_select_reason_backend_needs_only_api_key():
    journal = Journal()
    backend, reason = select_reason_backend(journal, NOW, {})
    assert (backend, reason) == ("fallback", "missing BASETEN_API_KEY")
    backend, reason = select_reason_backend(journal, NOW, {"BASETEN_API_KEY": "key-1"})
    assert (backend, reason) == ("baseten", "")
    backend, reason = select_reason_backend(journal, NOW, {"BASETEN_REASON_MODEL_ID": "model-1"})
    assert (backend, reason) == ("fallback", "missing BASETEN_API_KEY")


def test_select_reason_backend_baseten_when_both_vars_set():
    backend, reason = select_reason_backend(Journal(), NOW, dict(ENV))
    assert (backend, reason) == ("baseten", "")


def test_generate_reason_posts_stats_with_api_key():
    seen = {}

    def stub(url, headers, payload, timeout):
        seen.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {"reason": "  Wall holds the tape.  "}

    reason, latency_ms = generate_reason({"symbol": "SPY"}, dict(ENV), stub)
    assert reason == "Wall holds the tape."
    assert seen["url"] == predict_url("model-1")
    assert seen["headers"] == {"Authorization": "Api-Key key-1", "Content-Type": "application/json"}
    assert seen["payload"] == {"stats": {"symbol": "SPY"}}
    assert seen["timeout"] == 60.0
    assert isinstance(latency_ms, float)


def test_generate_reason_slug_posts_chat_with_bearer():
    seen = {}

    def stub(url, headers, payload, timeout):
        seen.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {"choices": [{"message": {"content": "  Wall holds the tape.  "}}]}

    reason, latency_ms = generate_reason({"symbol": "SPY"}, dict(SLUG_ENV), stub)
    assert reason == "Wall holds the tape."
    assert seen["url"] == INFERENCE_CHAT_URL
    assert seen["headers"] == {"Authorization": "Bearer key-1", "Content-Type": "application/json"}
    assert seen["payload"]["model"] == SLUG
    assert seen["payload"]["messages"][0]["role"] == "system"
    assert seen["payload"]["messages"][1] == {"role": "user", "content": '{"symbol": "SPY"}'}
    assert seen["payload"]["max_tokens"] == 200
    assert seen["timeout"] == 60.0
    assert isinstance(latency_ms, float)


def test_generate_reason_defaults_to_hackathon_model():
    seen = {}

    def stub(url, headers, payload, timeout):
        seen.update(url=url, payload=payload)
        return {"choices": [{"message": {"content": "Wall holds."}}]}

    reason, _ = generate_reason({"symbol": "SPY"}, {"BASETEN_API_KEY": "key-1"}, stub)
    assert reason == "Wall holds."
    assert seen["url"] == INFERENCE_CHAT_URL
    assert seen["payload"]["model"] == REASON_MODEL_DEFAULT == SLUG


def test_generate_reason_empty_reason_raises():
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: {"reason": "   "})
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: {})
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: {"reason": None})


def test_generate_reason_slug_empty_content_raises():
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(SLUG_ENV), lambda *args: {"choices": []})
    with pytest.raises(ValueError):
        generate_reason(
            {"symbol": "SPY"}, dict(SLUG_ENV), lambda *args: {"choices": [{"message": {"content": "  "}}]}
        )
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(SLUG_ENV), lambda *args: {})
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(SLUG_ENV), lambda *args: [])


def test_generate_reason_missing_key_raises_without_calling_http():
    def exploding(*args):
        raise AssertionError("must not call http without env")

    with pytest.raises(ValueError, match="BASETEN_API_KEY"):
        generate_reason({"symbol": "SPY"}, {}, exploding)
