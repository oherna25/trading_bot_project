from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from bot.config import auth_token as _auth_token, dashboard_base_url as _dashboard_base_url
from bot.dashboard import dashboard_html, screen_to_json
from bot.feeds import FeedContext, default_context, ingest_bars, ingest_chain, live_context
from bot.journal import Journal
from bot.persist import bundle_mtimes, refresh_bundle
from bot.research import (
    ExpiredDashboard,
    UnknownDashboard,
    dashboard_link_response,
    dex_brief_response,
    handle_chat_query,
    ideas_response,
    journal_tail_text,
    paper_pnl_text,
    route_chat,
    scheduled_brief,
    serve_pinned_screen,
    vex_brief_response,
)
from bot.shapes import OperatorConfig, default_operator_config
from bot.store import SnapshotStore, parse_snapshot_id


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


def _vex_brief_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    return vex_brief_response(underlying, store, journal, now)


def _dex_brief_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    return dex_brief_response(underlying, store, journal, now)


def _journal_tail_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    raw = args.get("count", 20) if isinstance(args, dict) else 20
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = 20
    return journal_tail_text(journal, count)


def _paper_pnl_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    return paper_pnl_text(journal, store)


TOOL_REGISTRY: dict[str, MuseTool] = {
    "get_gex_brief": MuseTool("get_gex_brief", "Read the computed GEX brief for an underlying", _gex_brief_handler),
    "get_dashboard_link": MuseTool(
        "get_dashboard_link", "Mint a pinned dashboard link for the latest snapshot", _dashboard_link_handler
    ),
    "get_trade_ideas": MuseTool(
        "get_trade_ideas", "Rank trade ideas from stored snapshots", _trade_ideas_handler
    ),
    "get_vex_brief": MuseTool("get_vex_brief", "Read the computed VEX brief for an underlying", _vex_brief_handler),
    "get_dex_brief": MuseTool("get_dex_brief", "Read the computed DEX brief for an underlying", _dex_brief_handler),
    "journal_tail": MuseTool("journal_tail", "Read the tail of the journal", _journal_tail_handler),
    "paper_pnl": MuseTool("paper_pnl", "Report paper P&L from journaled fills", _paper_pnl_handler),
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


DASHBOARD_PREFIX = "/dashboard/"
JSON_SUFFIX = ".json"
HEALTH_PATH = "/healthz"
HEALTH_DETAIL_PATH = "/health"
OPEN_PATHS = frozenset({HEALTH_PATH, HEALTH_DETAIL_PATH, "/openapi.json"})


@dataclass(frozen=True)
class HttpResponse:
    status: int
    content_type: str
    body: bytes

    @staticmethod
    def json(status: int, obj: Any) -> HttpResponse:
        return HttpResponse(status, "application/json", json.dumps(obj).encode("utf-8"))

    @staticmethod
    def html(status: int, text: str) -> HttpResponse:
        return HttpResponse(status, "text/html; charset=utf-8", text.encode("utf-8"))


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
            HEALTH_PATH: {
                "get": {
                    "summary": "Health check",
                    "responses": {"200": {"description": "Server is up"}},
                }
            },
            HEALTH_DETAIL_PATH: {
                "get": {
                    "summary": "Detailed health with last ingest",
                    "responses": {"200": {"description": "Ingest status"}},
                }
            },
            DASHBOARD_PREFIX + "{snapshot_id}": {
                "get": {
                    "summary": "Pinned dashboard screen",
                    "responses": {"200": {"description": "Dashboard page"}},
                }
            },
        },
    }


def health_detail(store: SnapshotStore, journal: Journal, now: datetime) -> dict[str, Any]:
    last_ingest = None
    feed_used = None
    snapshot_id = None
    for entry in journal.entries():
        if entry.kind in ("snapshot_ingested", "bars_ingested"):
            last_ingest = entry.timestamp_utc
            feed_used = entry.payload.get("source_name") or entry.payload.get("feed_name")
            snapshot_id = entry.payload.get("snapshot_id") or snapshot_id
    age_seconds = (now - last_ingest).total_seconds() if last_ingest is not None else None
    return {
        "ok": True,
        "last_ingest_utc": last_ingest.isoformat() if last_ingest is not None else None,
        "feed_used": feed_used,
        "snapshot_id": snapshot_id,
        "snapshot_age_seconds": age_seconds,
        "underlyings": store.list_underlyings(),
    }


def _authorized(clean: str, auth_token: Optional[str], authorization: str) -> bool:
    if not auth_token:
        return True
    if clean in OPEN_PATHS:
        return True
    return authorization.strip() == f"Bearer {auth_token}"


def route_http(
    method: str,
    path: str,
    payload: Any,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    auth_token: Optional[str] = None,
    authorization: str = "",
) -> HttpResponse:
    if method == "HEAD":
        method = "GET"
    clean = path.split("?")[0]
    if method == "GET" and clean == HEALTH_PATH:
        return HttpResponse.json(200, {"ok": True})
    if method == "GET" and clean == HEALTH_DETAIL_PATH:
        return HttpResponse.json(200, health_detail(store, journal, now))
    if method == "GET" and clean == "/openapi.json":
        return HttpResponse.json(200, openapi_spec())
    if not _authorized(clean, auth_token, authorization):
        return HttpResponse.json(401, {"ok": False, "error": "missing or invalid bearer token"})
    if method == "POST" and clean == "/chat":
        if not isinstance(payload, dict):
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": "missing chat text"})
        try:
            request = parse_chat_payload(payload)
        except ValueError as exc:
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": str(exc)})
        reply = handle_chat(request, store, journal, config, now)
        return HttpResponse.json(200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error})
    if method == "POST" and clean == "/mcp":
        if not isinstance(payload, dict):
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": "invalid MCP envelope"})
        mcp_method = payload.get("method") or ""
        if mcp_method == "tools/list":
            return HttpResponse.json(
                200,
                {
                    "tools": [
                        {"name": tool.name, "description": tool.description}
                        for tool in TOOL_REGISTRY.values()
                    ]
                },
            )
        if mcp_method == "tools/call":
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            name = params.get("name") or ""
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            underlying = params.get("underlying") or "SPY"
            reply = dispatch_tool(
                str(name), arguments, store, journal, config, now, underlying=str(underlying)
            )
            return HttpResponse.json(200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error})
        return HttpResponse.json(400, {"ok": False, "reply": "", "error": f"unknown MCP method {mcp_method}"})
    if method == "GET" and clean.startswith(DASHBOARD_PREFIX):
        rest = clean[len(DASHBOARD_PREFIX):]
        want_json = rest.endswith(JSON_SUFFIX)
        if want_json:
            rest = rest[: -len(JSON_SUFFIX)]
        try:
            snapshot_id = parse_snapshot_id(rest)
        except ValueError:
            return HttpResponse.json(404, {"error": "not found"})
        try:
            screen = serve_pinned_screen(snapshot_id, store, journal, config, now)
        except ExpiredDashboard:
            return HttpResponse.json(410, {"error": "dashboard link expired"})
        except UnknownDashboard:
            return HttpResponse.json(404, {"error": "not found"})
        if want_json:
            return HttpResponse.json(200, screen_to_json(screen))
        return HttpResponse.html(200, dashboard_html(screen))
    return HttpResponse.json(404, {"error": "not found"})


def resolve_port(explicit: Optional[int]) -> int:
    if explicit is not None:
        return explicit
    raw = (os.environ.get("PORT") or "").strip()
    if not raw:
        return 8000
    return int(raw)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def open_server_warning(host: str, auth_token: Optional[str]) -> Optional[str]:
    if host not in LOOPBACK_HOSTS and not auth_token:
        return f"warning: serving unauthenticated on {host}; set MUSE_AUTH_TOKEN or bind 127.0.0.1"
    return None


def start_refresh_loop(
    store: SnapshotStore,
    journal: Journal,
    symbols: Sequence[str],
    store_dir: str,
    interval_seconds: float,
    lock: threading.Lock,
    context_factory: Callable[[str], FeedContext] = live_context,
    stop: Optional[threading.Event] = None,
) -> threading.Event:
    from bot.schedule import normalize_underlyings, refresh_store

    halt = stop or threading.Event()
    fixed = normalize_underlyings(symbols) if symbols else []

    def _loop() -> None:
        while not halt.wait(interval_seconds):
            try:
                with lock:
                    refresh_store(
                        fixed or store.list_underlyings() or ["SPY"],
                        store,
                        journal,
                        store_dir,
                        context_factory=context_factory,
                    )
            except Exception as exc:
                print(json.dumps({"refresh": "failed", "error": type(exc).__name__}), flush=True)

    threading.Thread(target=_loop, name="refresh-loop", daemon=True).start()
    return halt


def create_server(
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    host: str = "0.0.0.0",
    port: Optional[int] = None,
    store_dir: Optional[str] = None,
    token: Optional[str] = None,
    refresh_seconds: float = 0,
    refresh_symbols: Sequence[str] = (),
    refresh_context_factory: Callable[[str], FeedContext] = live_context,
):  # -> ThreadingHTTPServer
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    bound = resolve_port(port)
    lock = threading.Lock()
    expected = token if token is not None else _auth_token()
    warning = open_server_warning(host, expected)
    if warning is not None:
        print(warning, flush=True)
    mtimes = bundle_mtimes(store_dir) if store_dir else {}

    class _Handler(BaseHTTPRequestHandler):
        def _send(self, response: HttpResponse, head_only: bool) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(response.body)

        def _access(self, status: int, started: float) -> None:
            line: dict[str, Any] = {
                "method": self.command,
                "path": self.path,
                "status": status,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
            }
            request_id = self.headers.get("X-Baseten-Request-Id")
            if request_id:
                line["request_id"] = request_id
            print(json.dumps(line), flush=True)

        def _maybe_reload(self) -> None:
            nonlocal mtimes
            if not store_dir:
                return
            current = bundle_mtimes(store_dir)
            if current != mtimes:
                if refresh_bundle(store_dir, store, journal):
                    mtimes = bundle_mtimes(store_dir)

        def _handle(self) -> None:
            started = time.monotonic()
            head_only = self.command == "HEAD"
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                response = HttpResponse.json(400, {"ok": False, "reply": "", "error": "invalid JSON"})
                self._send(response, head_only)
                self._access(response.status, started)
                return
            now = datetime.now(timezone.utc)
            authorization = self.headers.get("Authorization", "")
            with lock:
                self._maybe_reload()
                response = route_http(
                    self.command, self.path, payload, store, journal, config, now,
                    auth_token=expected, authorization=authorization,
                )
            self._send(response, head_only)
            self._access(response.status, started)

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def do_HEAD(self) -> None:
            self._handle()

    server = ThreadingHTTPServer((host, bound), _Handler)
    if refresh_seconds and refresh_seconds > 0 and store_dir:
        server.refresh_stop = start_refresh_loop(  # type: ignore[attr-defined]
            store, journal, refresh_symbols, store_dir, refresh_seconds, lock,
            context_factory=refresh_context_factory,
        )
    return server


def serve(
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    host: str = "0.0.0.0",
    port: Optional[int] = None,
    store_dir: Optional[str] = None,
    refresh_seconds: float = 0,
    refresh_symbols: Sequence[str] = (),
    refresh_context_factory: Callable[[str], FeedContext] = live_context,
) -> None:
    create_server(
        store, journal, config, host, port, store_dir,
        refresh_seconds=refresh_seconds,
        refresh_symbols=refresh_symbols,
        refresh_context_factory=refresh_context_factory,
    ).serve_forever()


def default_dashboard_base_url(env: Mapping[str, str] | None = None) -> str:
    value = _dashboard_base_url(env)
    if value:
        return value
    port = (os.environ.get("PORT") or "").strip() or "8000"
    return f"http://127.0.0.1:{port}"


class Harness:
    def __init__(self, underlying: str = "SPY") -> None:
        self.underlying = underlying
        self.store = SnapshotStore()
        self.journal = Journal()
        self.config = default_operator_config(default_dashboard_base_url())
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
