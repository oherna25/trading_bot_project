import json

import pytest

from bot import claude
from bot.models import MODEL_REGISTRY, run_analyst

TOOL = {
    "type": "function",
    "function": {
        "name": "get_gex_profile",
        "description": "Read the computed GEX profile for an underlying",
        "parameters": {"type": "object", "properties": {"underlying": {"type": "string"}}},
    },
}


class _Message:
    def __init__(self, body):
        self.body = body

    def to_dict(self):
        return self.body


class _FakeClient:
    """Stands in for anthropic.Anthropic: records requests, replays bodies."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []
        self.beta = self
        self.messages = self

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request)))
        return _Message(self.bodies.pop(0))


def _reply(content, stop_reason="end_turn", **extra):
    body = {
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    body.update(extra)
    return body


def test_request_moves_system_out_and_translates_tools_and_effort():
    request = claude.claude_request(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 16000,
            "reasoning_effort": "high",
            "tools": [TOOL],
            "messages": [
                {"role": "system", "content": "You are an analyst."},
                {"role": "user", "content": "Read SPY."},
            ],
        }
    )
    assert request["system"] == "You are an analyst."
    assert request["messages"] == [{"role": "user", "content": "Read SPY."}]
    assert request["tools"][0]["name"] == "get_gex_profile"
    assert request["tools"][0]["input_schema"]["type"] == "object"
    assert request["output_config"] == {"effort": "high"}
    assert "temperature" not in request and "reasoning_effort" not in request


def test_request_turns_a_data_url_into_a_base64_image_before_the_text():
    request = claude.claude_request(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is the trend?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                    ],
                }
            ],
        }
    )
    image, text = request["messages"][0]["content"]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert text == {"type": "text", "text": "What is the trend?"}


def test_request_groups_tool_results_into_one_user_message():
    request = claude.claude_request(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "a", "function": {"name": "t", "arguments": "{\"x\": 1}"}},
                        {"id": "b", "function": {"name": "t", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "a", "content": "one"},
                {"role": "tool", "tool_call_id": "b", "content": "two"},
            ],
        }
    )
    assistant, results = request["messages"][1], request["messages"][2]
    assert [block["input"] for block in assistant["content"]] == [{"x": 1}, {}]
    assert results["role"] == "user"
    assert [block["tool_use_id"] for block in results["content"]] == ["a", "b"]


def test_chat_body_exposes_text_tool_calls_and_cached_token_usage():
    body = claude.chat_body(
        _reply(
            [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Pulling the profile."},
                {"type": "tool_use", "id": "tu_1", "name": "get_gex_profile", "input": {"underlying": "SPY"}},
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
        )
    )
    message = body["choices"][0]["message"]
    assert message["content"] == "Pulling the profile."
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"underlying": "SPY"}
    assert body["usage"] == {"prompt_tokens": 100, "completion_tokens": 5}


def test_chat_body_raises_on_refusal():
    with pytest.raises(claude.ClaudeRefusal, match="cyber"):
        claude.chat_body(_reply([], stop_reason="refusal", stop_details={"category": "cyber"}))


def test_tool_round_replays_thinking_blocks_unchanged(monkeypatch):
    first = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "tu_1", "name": "get_gex_profile", "input": {"underlying": "SPY"}},
    ]
    client = _FakeClient(
        [
            _reply(first, stop_reason="tool_use"),
            _reply([{"type": "text", "text": "Short gamma.\nDIRECTION: short"}], model="claude-opus-4-8"),
        ]
    )
    monkeypatch.setattr(claude, "_client", lambda api_key, timeout: client)
    text, receipts = run_analyst(
        MODEL_REGISTRY["reviewer"], "key",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "Read SPY."}],
        [TOOL], {"get_gex_profile": lambda args: "{\"flip\": 500}"}, "Read SPY.",
    )
    assert text.startswith("Short gamma.")
    opening, followup = client.requests
    assert opening["betas"] == [claude.FALLBACK_BETA] and opening["fallbacks"] == "default"
    assert opening["output_config"] == {"effort": "high"}
    assert followup["messages"][1] == {"role": "assistant", "content": first}
    assert followup["messages"][2]["content"][0] == {
        "type": "tool_result", "tool_use_id": "tu_1", "content": "{\"flip\": 500}",
    }
    assert [receipt.model_id for receipt in receipts] == ["claude-opus-5-5", "claude-opus-4-8"]
    assert receipts[0].tokens_in == 100 and receipts[0].tokens_out == 20


def test_grounded_resumes_a_paused_turn_and_sums_usage():
    paused = [{"type": "server_tool_use", "id": "st_1", "name": "web_search", "input": {"query": "SPY"}}]
    again = [{"type": "text", "text": "No contradicting "}]
    client = _FakeClient(
        [
            _reply(paused, stop_reason="pause_turn"),
            _reply(again, stop_reason="pause_turn"),
            _reply([{"type": "text", "text": "news."}]),
        ]
    )
    body = claude.grounded(
        "key", "claude-opus-5-5", "Any news?", [dict(claude.WEB_SEARCH_TOOL)], "low", 60.0,
        client_factory=lambda api_key, timeout: client,
    )
    assert body["output"][0]["content"][0]["text"] == "No contradicting news."
    assert body["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert client.requests[0]["tools"][0]["type"] == "web_search_20260209"
    assert client.requests[2]["messages"][1:] == [
        {"role": "assistant", "content": paused},
        {"role": "assistant", "content": again},
    ]


def test_missing_sdk_fails_with_an_install_hint(monkeypatch):
    monkeypatch.setattr(claude, "sdk_available", lambda: False)
    with pytest.raises(claude.ClaudeUnavailable, match="pip install"):
        claude._client("key", 60.0)
