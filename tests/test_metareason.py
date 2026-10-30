from datetime import datetime, timezone

import pytest

from bot.metareason import generate_reason, select_reason_backend
from bot.journal import Journal

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

ENV = {"META_MODEL_API_KEY": "key-1"}


def _chat_body(content):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def test_select_reason_backend_names_missing_var():
    backend, reason = select_reason_backend(Journal(), NOW, {})
    assert (backend, reason) == ("fallback", "missing META_MODEL_API_KEY")


def test_select_reason_backend_meta_when_key_set():
    backend, reason = select_reason_backend(Journal(), NOW, dict(ENV))
    assert (backend, reason) == ("meta", "")


def test_generate_reason_posts_chat_with_api_key():
    seen = {}

    def stub(url, headers, payload, timeout):
        seen.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return _chat_body("  Wall holds the tape.  ")

    reason, latency_ms = generate_reason({"symbol": "SPY"}, dict(ENV), stub)
    assert reason == "Wall holds the tape."
    assert seen["url"] == "https://api.meta.ai/v1/chat/completions"
    assert seen["headers"] == {"Authorization": "Bearer key-1", "Content-Type": "application/json"}
    assert seen["payload"]["model"] == "muse-spark-1.3"
    assert seen["payload"]["messages"][0]["role"] == "system"
    assert seen["payload"]["messages"][1] == {"role": "user", "content": '{"symbol": "SPY"}'}
    assert seen["timeout"] == 60.0
    assert isinstance(latency_ms, float)


def test_generate_reason_empty_reason_raises():
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: _chat_body("   "))
    with pytest.raises(ValueError):
        generate_reason({"symbol": "SPY"}, dict(ENV), lambda *args: _chat_body(""))


def test_generate_reason_missing_env_raises_without_calling_http():
    def exploding(*args):
        raise AssertionError("must not call http without env")

    with pytest.raises(ValueError, match="META_MODEL_API_KEY"):
        generate_reason({"symbol": "SPY"}, {}, exploding)
