"""Chat payload parsing, LLM routing, and tool dispatch."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional

from bot.journal import Journal
from bot.models import MODEL_REGISTRY, HttpPost, model_api_key, receipt_payload, run_analyst
from bot.research import (
    route_chat,
)
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore

from bot.muse_session import SessionController
from bot.muse_tools import MuseChatReply, MuseChatRequest, TOOL_REGISTRY, _session_run_handler

def parse_chat_payload(raw: dict) -> MuseChatRequest:
    if not isinstance(raw, dict):
        raise ValueError("missing chat text")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("missing chat text")
    underlying = raw.get("underlying", "SPY")
    if not isinstance(underlying, str) or not underlying.strip():
        underlying = "SPY"
    return MuseChatRequest(
        text=text,
        underlying=underlying,
        live_models=bool(raw.get("live_models", False)),
        grounding=bool(raw.get("grounding", False)),
        cards=bool(raw.get("cards", False)),
        llm_router=bool(raw.get("llm_router", False)),
    )


ROUTE_SYSTEM = (
    "Pick exactly one tool for the user request. Reply with JSON only and no other text: "
    '{"tool": name, "arguments": {}, "underlying": "SYM"}. '
    "Keep the default underlying unless the text names another ticker."
)


def llm_route(
    text: str,
    underlying: str,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> tuple[str, dict[str, Any], str]:
    key = model_api_key(env)
    if not key:
        raise ValueError("missing META_MODEL_API_KEY")
    catalog = "\n".join(f"- {name}: {tool.description}" for name, tool in TOOL_REGISTRY.items())
    prompt = f"Request: {text}\nDefault underlying: {underlying}\nTools:\n{catalog}"
    messages = [
        {"role": "system", "content": ROUTE_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    reply_text, receipts = run_analyst(
        MODEL_REGISTRY["analyst"], key, messages, [], {}, prompt, http_post
    )
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, ""))
    start, end = reply_text.find("{"), reply_text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("router returned no JSON")
    try:
        body = json.loads(reply_text[start : end + 1])
    except ValueError:
        raise ValueError("router returned bad JSON") from None
    if not isinstance(body, dict):
        raise ValueError("router returned bad JSON")
    name = body.get("tool", "")
    if name not in TOOL_REGISTRY:
        raise ValueError(f"unknown tool {name}")
    args = body.get("arguments", {})
    if not isinstance(args, dict):
        raise ValueError("router arguments must be an object")
    symbol = body.get("underlying") or underlying
    if not isinstance(symbol, str) or not symbol.strip():
        symbol = underlying
    return name, args, symbol.strip().upper()


def handle_chat(
    request: MuseChatRequest,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> MuseChatReply:
    if request.llm_router:
        try:
            name, args, symbol = llm_route(request.text, request.underlying, journal, now, env, http_post)
        except Exception as exc:
            journal.append(now, "llm_route_failed", {"reason": type(exc).__name__})
        else:
            journal.append(now, "llm_routed", {"tool": name, "underlying": symbol})
            return dispatch_tool(name, args, store, journal, config, now, underlying=symbol)
    try:
        reply = route_chat(
            request.text,
            request.underlying,
            store,
            journal,
            config,
            now,
            live_models=request.live_models,
            grounding=request.grounding,
            cards=request.cards,
        )
    except ValueError as exc:
        return MuseChatReply(ok=False, reply=str(exc), error=str(exc))
    return MuseChatReply(ok=True, reply=reply, error="")


def dispatch_tool(
    name: str,
    args: dict,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    underlying: str = "SPY",
    session_controller: Optional[SessionController] = None,
) -> MuseChatReply:
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return MuseChatReply(ok=False, reply=f"unknown tool {name}", error=f"unknown tool {name}")
    try:
        handler_args = args if isinstance(args, dict) else {}
        if tool.handler is _session_run_handler:
            reply = tool.handler(
                handler_args, store, journal, config, now, underlying, session_controller
            )
        else:
            reply = tool.handler(handler_args, store, journal, config, now, underlying)
    except ValueError as exc:
        return MuseChatReply(ok=False, reply=str(exc), error=str(exc))
    return MuseChatReply(ok=True, reply=reply, error="")
