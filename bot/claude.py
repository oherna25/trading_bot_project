"""Claude backend: the Anthropic SDK behind the chat-shaped seam callers use.

Callers build one provider-neutral chat payload (system/user/assistant/tool
messages, function tools). This module translates it to a Messages API
request, sends it through the official SDK, and translates the reply back, so
`run_analyst` drives Claude and OpenAI-compatible backends with one loop.

The SDK is an optional extra and is imported lazily. The key is always passed
explicitly: a bare client could pick up a local login profile and reach the
network from a run that was meant to stay offline.
"""
from __future__ import annotations

import importlib.util
import json
from typing import Any, Callable, Mapping

API_KEY_ENV = "ANTHROPIC_API_KEY"
BASE_URL = "https://api.anthropic.com/v1"
MODEL_ID = "claude-opus-5-5"
# Thinking is always on for this model and shares the output budget.
MAX_TOKENS = 16000
MIN_TIMEOUT_SECONDS = 180.0
# One retry keeps a slow request from holding a chat reply for many minutes.
MAX_RETRIES = 1
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
MAX_PAUSE_RESUMES = 3
SDK_MISSING = "anthropic SDK not installed (pip install '.[claude]')"
# Raw content blocks ride along on the assistant message so a tool round can
# replay them unchanged; thinking blocks are only valid when echoed verbatim.
RAW_CONTENT_KEY = "claude_content"

ClientFactory = Callable[[str, float], Any]


class ClaudeUnavailable(RuntimeError):
    """The SDK extra is not installed."""


class ClaudeRefusal(ValueError):
    """A safety classifier declined the request, including the fallback."""


def sdk_available() -> bool:
    return importlib.util.find_spec("anthropic") is not None


def _client(api_key: str, timeout: float) -> Any:
    if not sdk_available():
        raise ClaudeUnavailable(SDK_MISSING)
    import anthropic

    return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=MAX_RETRIES)


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:") and ";base64," in url:
        head, data = url.split(";base64,", 1)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": head[len("data:"):], "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _user_content(content: Any) -> Any:
    if not isinstance(content, list):
        return str(content or "")
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "image_url":
            image = part.get("image_url") or {}
            url = image.get("url", "") if isinstance(image, Mapping) else str(image)
            blocks.append(_image_block(url))
        elif part.get("type") == "text":
            blocks.append({"type": "text", "text": str(part.get("text", ""))})
    # Images go before the question that refers to them.
    return sorted(blocks, key=lambda block: block["type"] != "image")


def _assistant_content(message: Mapping[str, Any]) -> list[Any]:
    raw = message.get(RAW_CONTENT_KEY)
    if raw is not None:
        return list(raw)
    blocks: list[Any] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": str(message["content"])})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        blocks.append(
            {"type": "tool_use", "id": call.get("id"), "name": function.get("name", ""), "input": arguments}
        )
    return blocks


def _is_tool_results(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and all(isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content)
    )


def claude_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a chat-shaped payload into Messages API keyword arguments."""
    system: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        role = message.get("role")
        if role == "system":
            system.append(str(message.get("content") or ""))
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": str(message.get("content") or ""),
            }
            # Every result for one assistant turn belongs in one user message.
            if messages and _is_tool_results(messages[-1]):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": _assistant_content(message)})
        else:
            messages.append({"role": "user", "content": _user_content(message.get("content"))})
    request: dict[str, Any] = {
        "model": payload["model"],
        "max_tokens": int(payload.get("max_tokens") or MAX_TOKENS),
        "messages": messages,
    }
    if system:
        request["system"] = "\n\n".join(part for part in system if part)
    tools = [
        {
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
        }
        for function in (tool.get("function") or {} for tool in payload.get("tools") or [])
    ]
    if tools:
        request["tools"] = tools
    effort = payload.get("reasoning_effort")
    if effort:
        request["output_config"] = {"effort": effort}
    return request


def _usage(message: Mapping[str, Any]) -> tuple[int, int]:
    usage = message.get("usage") or {}
    tokens_in = sum(
        int(usage.get(name) or 0)
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return tokens_in, int(usage.get("output_tokens") or 0)


def _raise_on_refusal(message: Mapping[str, Any]) -> None:
    if message.get("stop_reason") != "refusal":
        return
    details = message.get("stop_details") or {}
    category = details.get("category") if isinstance(details, Mapping) else None
    raise ClaudeRefusal(f"claude refused ({category or 'uncategorized'})")


def _text(message: Mapping[str, Any]) -> str:
    return "".join(
        str(block.get("text", ""))
        for block in message.get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def chat_body(message: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a Messages API reply into the chat-shaped body callers parse."""
    _raise_on_refusal(message)
    content = list(message.get("content") or [])
    calls = [
        {
            "id": block.get("id"),
            "type": "function",
            "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input") or {})},
        }
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]
    tokens_in, tokens_out = _usage(message)
    return {
        "model": message.get("model"),
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": _text(message),
                    "tool_calls": calls,
                    RAW_CONTENT_KEY: content,
                }
            }
        ],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def _create(client: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    # A declined request re-runs on the routed fallback model in the same call.
    message = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
    return message.to_dict()


def chat(
    api_key: str,
    payload: Mapping[str, Any],
    timeout: float,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    client = (client_factory or _client)(api_key, max(timeout, MIN_TIMEOUT_SECONDS))
    return chat_body(_create(client, claude_request(payload)))


def grounded(
    api_key: str,
    model_id: str,
    question: str,
    tools: list[dict[str, Any]],
    effort: str,
    timeout: float,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Answer one question with server tools such as web search."""
    client = (client_factory or _client)(api_key, max(timeout, MIN_TIMEOUT_SECONDS))
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    tokens_in = tokens_out = 0
    message: dict[str, Any] = {}
    text = ""
    for _ in range(MAX_PAUSE_RESUMES + 1):
        message = _create(
            client,
            {
                "model": model_id,
                "max_tokens": MAX_TOKENS,
                "messages": messages,
                "tools": tools,
                "output_config": {"effort": effort},
            },
        )
        used_in, used_out = _usage(message)
        tokens_in, tokens_out = tokens_in + used_in, tokens_out + used_out
        text += _text(message)
        if message.get("stop_reason") != "pause_turn":
            break
        # A paused server-tool turn resumes when its content is sent back.
        messages.append({"role": "assistant", "content": message.get("content") or []})
    _raise_on_refusal(message)
    return {
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }
