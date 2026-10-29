from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from bot.feeds import default_context, ingest_bars, ingest_chain
from bot.journal import Journal
from bot.research import (
    dashboard_link_response,
    handle_chat_query,
    ideas_response,
    route_chat,
    scheduled_brief,
)
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore


@dataclass(frozen=True)
class MuseChatRequest:
    text: str
    underlying: str = "SPY"
    live_models: bool = False
    grounding: bool = False
    cards: bool = False


@dataclass(frozen=True)
class MuseChatReply:
    ok: bool
    reply: str = ""
    error: str = ""


ToolHandler = Callable[[dict[str, Any], SnapshotStore, Journal, OperatorConfig, datetime, str], str]


@dataclass(frozen=True)
class MuseTool:
    name: str
    description: str
    handler: ToolHandler


def _gex_brief_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    reply, _ = handle_chat_query("gex brief", underlying, store, journal, now)
    return reply


def _dashboard_link_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    return dashboard_link_response(underlying, store, journal, config, now)


def _trade_ideas_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    query = args.get("query", "trade ideas") if isinstance(args, dict) else "trade ideas"
    if not isinstance(query, str) or not query.strip():
        query = "trade ideas"
    return ideas_response(query, underlying, store, journal, now)


TOOL_REGISTRY: dict[str, MuseTool] = {
    "get_gex_brief": MuseTool("get_gex_brief", "Read the computed GEX brief for an underlying", _gex_brief_handler),
    "get_dashboard_link": MuseTool(
        "get_dashboard_link", "Mint a pinned dashboard link for the latest snapshot", _dashboard_link_handler
    ),
    "get_trade_ideas": MuseTool(
        "get_trade_ideas", "Rank trade ideas from stored snapshots", _trade_ideas_handler
    ),
}


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
    )


def handle_chat(
    request: MuseChatRequest, store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime
) -> MuseChatReply:
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
        return MuseChatReply(ok=False, reply="", error=str(exc))
    return MuseChatReply(ok=True, reply=reply, error="")


def dispatch_tool(
    name: str,
    args: dict,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    underlying: str = "SPY",
) -> MuseChatReply:
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return MuseChatReply(ok=False, reply="", error=f"unknown tool {name}")
    try:
        reply = tool.handler(args if isinstance(args, dict) else {}, store, journal, config, now, underlying)
    except ValueError as exc:
        return MuseChatReply(ok=False, reply="", error=str(exc))
    return MuseChatReply(ok=True, reply=reply, error="")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Muse Chat", "version": "0.1.0"},
        "paths": {
            "/chat": {
                "post": {
                    "summary": "Chat with Muse",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Chat reply"}},
                }
            },
            "/mcp": {
                "post": {
                    "summary": "MCP tools",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Tool result"}},
                }
            },
        },
    }


def route_http(
    method: str,
    path: str,
    payload: Any,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
) -> tuple[int, dict]:
    clean = path.split("?")[0]
    if method == "GET" and clean == "/openapi.json":
        return 200, openapi_spec()
    if method == "POST" and clean == "/chat":
        if not isinstance(payload, dict):
            return 400, {"ok": False, "reply": "", "error": "missing chat text"}
        try:
            request = parse_chat_payload(payload)
        except ValueError as exc:
            return 400, {"ok": False, "reply": "", "error": str(exc)}
        reply = handle_chat(request, store, journal, config, now)
        return 200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error}
    if method == "POST" and clean == "/mcp":
        if not isinstance(payload, dict):
            return 400, {"ok": False, "reply": "", "error": "invalid MCP envelope"}
        mcp_method = payload.get("method") or ""
        if mcp_method == "tools/list":
            return 200, {
                "tools": [
                    {"name": tool.name, "description": tool.description}
                    for tool in TOOL_REGISTRY.values()
                ]
            }
        if mcp_method == "tools/call":
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            name = params.get("name") or ""
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            underlying = params.get("underlying") or "SPY"
            reply = dispatch_tool(
                str(name), arguments, store, journal, config, now, underlying=str(underlying)
            )
            return 200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error}
        return 400, {"ok": False, "reply": "", "error": f"unknown MCP method {mcp_method}"}
    return 404, {"error": "not found"}


def serve(
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def _send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                self._send_json({"ok": False, "reply": "", "error": "invalid JSON"}, 400)
                return
            now = datetime.now(timezone.utc)
            status, body = route_http(self.command, self.path, payload, store, journal, config, now)
            self._send_json(body, status)

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

    HTTPServer((host, port), _Handler).serve_forever()


class Harness:
    def __init__(self, underlying: str = "SPY") -> None:
        self.underlying = underlying
        self.store = SnapshotStore()
        self.journal = Journal()
        self.config = OperatorConfig(
            dashboard_base_url="https://ops.example.internal",
            dashboard_link_expiry_seconds=3600,
            max_day_loss=1000.0,
            day_profit_target=500.0,
            max_trade_size=5000.0,
        )
        self.inbox: list[dict[str, str]] = []
        self.outbox: list[dict[str, str]] = []
        ctx = default_context(underlying)
        ingest_chain("tradier_sandbox", self.store, self.journal, ctx)
        ingest_bars("alpaca", self.store, self.journal, ctx)

    def post(self, body: str, kind: str = "text") -> None:
        self.inbox.append({"kind": kind, "body": body})

    def run_once(self) -> None:
        pending = list(self.inbox)
        self.inbox.clear()
        now = datetime.now(timezone.utc)
        for message in pending:
            body = message.get("body", "") if isinstance(message, dict) else str(message)
            reply = handle_chat(MuseChatRequest(text=body, underlying=self.underlying), self.store, self.journal, self.config, now)
            text = reply.reply if reply.ok else reply.error
            kind = "link" if text.startswith("dashboard for snapshot") else "text"
            self.outbox.append({"kind": kind, "body": text})

    def push_scheduled(self) -> None:
        body = scheduled_brief(self.underlying, self.store, self.journal, datetime.now(timezone.utc))
        self.outbox.append({"kind": "push", "body": body})
