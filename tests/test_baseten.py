from datetime import datetime, timezone

import pytest

from bot.baseten import generate_reason, predict_url, select_reason_backend
from bot.journal import Journal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

ENV = {"BASETEN_API_KEY": "key-1", "BASETEN_REASON_MODEL_ID": "model-1"}


def test_predict_url_builds_from_model_id():
    assert predict_url("abc123") == "https://model-abc123.api.baseten.co/environments/production/predict"


def test_predict_url_uses_full_url_as_is():
    assert predict_url("https://example.internal/predict") == "https://example.internal/predict"
    assert predict_url("http://127.0.0.1:8000/predict") == "http://127.0.0.1:8000/predict"


def test_select_reason_backend_names_missing_var():
    journal = Journal()
    backend, reason = select_reason_backend(journal, NOW, {})
    assert (backend, reason) == ("fallback", "missing BASETEN_API_KEY")
    backend, reason = select_reason_backend(journal, NOW, {"BASETEN_API_KEY": "key-1"})
    assert (backend, reason) == ("fallback", "missing BASETEN_REASON_MODEL_ID")
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


def test_generate_reason_empty_reason_raises():
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: {"reason": "   "})
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: {})
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: {"reason": None})


def test_generate_reason_missing_env_raises_without_calling_http():
    def exploding(*args):
        raise AssertionError("must not call http without env")

    with pytest.raises(ValueError, match="BASETEN_API_KEY"):
        generate_reason({"symbol": "SPY"}, {}, exploding)
    with pytest.raises(ValueError, match="BASETEN_REASON_MODEL_ID"):
        generate_reason({"symbol": "SPY"}, {"BASETEN_API_KEY": "key-1"}, exploding)
