"""Muse transport facade: server, loops, Harness, re-exports."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence
from urllib.parse import parse_qs

from bot.config import auth_token as _auth_token
from bot.feeds import FeedContext, default_context, ingest_bars, ingest_chain, live_context
from bot.journal import Journal
from bot.persist import bundle_mtimes, refresh_bundle, save_bundle
from bot.research import (
    scheduled_brief,
)
from bot.schedule import normalize_underlyings, tick_symbols
from bot.shapes import OperatorConfig, default_operator_config
from bot.store import SnapshotStore

from bot.muse_chat import dispatch_tool, handle_chat, llm_route, parse_chat_payload
from bot.muse_http import (FormData, HttpResponse, _fail, health_detail, openapi_spec, route_http)
from bot.muse_session import (MinuteKey, SESSION_TICK_SECONDS, SessionController, SessionSchedule, _scratch_run, parse_schedule, session_due)
from bot.muse_tools import (MuseChatReply, MuseChatRequest, MuseTool, TOOL_REGISTRY, ToolHandler)

def resolve_port(explicit: Optional[int]) -> int:
    if explicit is not None:
        return explicit
    raw = (os.environ.get("PORT") or "").strip()
    if not raw:
        return 8000
    return int(raw)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024


FORM_PATHS = frozenset({"/settings", "/action", "/holdings", "/profile", "/session"})


def content_length(headers: Any) -> int:
    raw = headers.get("Content-Length", "0") or "0"
    length = int(str(raw).strip())
    if length < 0:
        raise ValueError("negative content length")
    return length


def _body_payload(clean: str, raw: bytes) -> tuple[Any, Optional[HttpResponse]]:
    if clean == "/voice":
        if len(raw) > MAX_VOICE_BYTES:
            return None, _fail(413, "voice audio too large")
        return raw, None
    if clean in ("/chartread", "/docread"):
        if len(raw) > MAX_IMAGE_BYTES:
            label = "chart image too large" if clean == "/chartread" else "document image too large"
            return None, _fail(413, label)
        return raw, None
    try:
        return (json.loads(raw.decode("utf-8")) if raw else {}), None
    except ValueError:
        pass
    if clean in FORM_PATHS:
        try:
            text = raw.decode("utf-8")
        except ValueError:
            return None, _fail(400, "invalid form body")
        fields = {key: values[0] for key, values in parse_qs(text, keep_blank_values=True).items() if values}
        return FormData(fields), None
    return None, _fail(400, "invalid JSON")


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
    lock: threading.RLock,
    context_factory: Callable[[str], FeedContext] = live_context,
    stop: Optional[threading.Event] = None,
    after_save: Optional[Callable[[], None]] = None,
) -> threading.Event:
    from bot.schedule import refresh_core

    halt = stop or threading.Event()
    fixed = normalize_underlyings(symbols) if symbols else []

    def _loop() -> None:
        while not halt.wait(interval_seconds):
            try:
                with lock:
                    resolved = tick_symbols(fixed, store)
                with _scratch_run(lock, store, journal) as (scratch_store, scratch_journal):
                    refresh_core(
                        resolved,
                        scratch_store,
                        scratch_journal,
                        None,
                        None,
                        None,
                        context_factory,
                        store_dir,
                    )
                with lock:
                    store.prune()
                    journal.prune()
                    save_bundle(store_dir, store, journal)
                    if after_save is not None:
                        after_save()
            except Exception as exc:
                print(json.dumps({"refresh": "failed", "error": type(exc).__name__}), flush=True)

    threading.Thread(target=_loop, name="refresh-loop", daemon=True).start()
    return halt


def start_session_loop(
    controller: SessionController,
    schedule: SessionSchedule,
    symbols: Sequence[str],
    interval_seconds: float = SESSION_TICK_SECONDS,
    stop: Optional[threading.Event] = None,
) -> threading.Event:
    halt = stop or threading.Event()
    fixed = list(symbols) if symbols else ["SPY"]
    last_fired: MinuteKey | None = None

    def _loop() -> None:
        nonlocal last_fired
        while not halt.wait(interval_seconds):
            try:
                now_local = datetime.now().astimezone()
                if not session_due(schedule, now_local, last_fired):
                    continue
                text, _ = controller.run(fixed)
                last_fired = (now_local.year, now_local.month, now_local.day, now_local.hour, now_local.minute)
                print(text, flush=True)
            except Exception as exc:
                print(json.dumps({"session": "failed", "error": type(exc).__name__}), flush=True)

    threading.Thread(target=_loop, name="session-loop", daemon=True).start()
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
    session_schedule: str = "",
    session_underlyings: Sequence[str] = (),
):  # -> ThreadingHTTPServer
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    bound = resolve_port(port)
    lock = threading.RLock()
    expected = token if token is not None else _auth_token()
    warning = open_server_warning(host, expected)
    if warning is not None:
        print(warning, flush=True)
    mtimes = bundle_mtimes(store_dir) if store_dir else {}

    def note_own_save() -> None:
        nonlocal mtimes
        if store_dir:
            mtimes = bundle_mtimes(store_dir)

    controller = (
        SessionController(store, journal, store_dir, lock, after_save=note_own_save)
        if store_dir
        else None
    )

    class _Handler(BaseHTTPRequestHandler):
        def _send(self, response: HttpResponse, head_only: bool) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers:
                self.send_header(name, value)
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
                else:
                    mtimes = current

        def _handle(self) -> None:
            started = time.monotonic()
            head_only = self.command == "HEAD"
            try:
                length = content_length(self.headers)
            except ValueError:
                bad = _fail(400, "invalid content length")
                self._send(bad, head_only)
                self._access(bad.status, started)
                return
            if length > MAX_VOICE_BYTES:
                too_big = _fail(413, "request body too large")
                self._send(too_big, head_only)
                self._access(too_big.status, started)
                return
            raw = self.rfile.read(length) if length else b""
            payload, decode_error = _body_payload(self.path.split("?", 1)[0], raw)
            if decode_error is not None:
                self._send(decode_error, head_only)
                self._access(decode_error.status, started)
                return
            now = datetime.now(timezone.utc)
            authorization = self.headers.get("Authorization", "")
            with lock:
                self._maybe_reload()
                response = route_http(
                    self.command, self.path, payload, store, journal, config, now,
                    auth_token=expected, authorization=authorization,
                    session_controller=controller,
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
            after_save=note_own_save,
        )
    if session_schedule and store_dir and controller is not None:
        server.session_stop = start_session_loop(  # type: ignore[attr-defined]
            controller, parse_schedule(session_schedule), list(session_underlyings),
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
    session_schedule: str = "",
    session_underlyings: Sequence[str] = (),
) -> None:
    create_server(
        store, journal, config, host, port, store_dir,
        refresh_seconds=refresh_seconds,
        refresh_symbols=refresh_symbols,
        refresh_context_factory=refresh_context_factory,
        session_schedule=session_schedule,
        session_underlyings=session_underlyings,
    ).serve_forever()


class Harness:
    def __init__(self, underlying: str = "SPY") -> None:
        self.underlying = underlying
        self.store = SnapshotStore()
        self.journal = Journal()
        self.config = default_operator_config()
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
            self.outbox.append({"kind": "text", "body": text})

    def push_scheduled(self) -> None:
        body = scheduled_brief(self.underlying, self.store, self.journal, datetime.now(timezone.utc))
        self.outbox.append({"kind": "push", "body": body})

__all__ = [
    "FormData",
    "Harness",
    "MAX_VOICE_BYTES",
    "MuseChatReply",
    "MuseChatRequest",
    "MuseTool",
    "SessionController",
    "TOOL_REGISTRY",
    "ToolHandler",
    "_body_payload",
    "content_length",
    "create_server",
    "dispatch_tool",
    "handle_chat",
    "health_detail",
    "llm_route",
    "open_server_warning",
    "openapi_spec",
    "parse_chat_payload",
    "parse_schedule",
    "resolve_port",
    "route_http",
    "serve",
    "session_due",
    "start_refresh_loop",
    "start_session_loop",
]
