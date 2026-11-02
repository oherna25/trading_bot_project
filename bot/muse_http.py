"""HTTP primitives, OpenAPI spec, and route_http."""

from __future__ import annotations

import hashlib
import html
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs

from bot.charts import gamma_chart_html, regime_board_html
from bot.config import env_value as _env_value
from bot.demo import demo_html
from bot.exposure import compute_gex
from bot.feeds import feed_health, feed_health_html
from bot.journal import Journal
from bot.models import user_model_form_defaults, user_model_status
from bot.research import (
    paper_pnl_text,
)
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore, empty_store_text, need_snapshot_text
from bot.tradecards import (
    OPTIONS_DISABLED,
    PROFILE_REQUIRED,
    bar_signature,
    cards_to_json,
    chain_fingerprint,
    peek_trade_cards_key,
    query_trade_cards,
    trade_cards_html,
)
from bot.video import build_video_brief, composition_html

from bot.muse_chat import dispatch_tool, handle_chat, parse_chat_payload
from bot.muse_session import SESSION_PATH, SessionController
from bot.muse_tools import (TOOL_REGISTRY, save_profile_answers, save_settings_merge, settings_summary)

HEALTH_PATH = "/healthz"
HEALTH_DETAIL_PATH = "/health"
OPEN_PATHS = frozenset({HEALTH_PATH, HEALTH_DETAIL_PATH, "/openapi.json"})


class FormData(dict):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def json(status: int, obj: Any) -> HttpResponse:
        return HttpResponse(status, "application/json", json.dumps(obj).encode("utf-8"))

    @staticmethod
    def html(status: int, text: str) -> HttpResponse:
        return HttpResponse(status, "text/html; charset=utf-8", text.encode("utf-8"))

    @staticmethod
    def redirect(location: str) -> HttpResponse:
        safe = location if location.startswith("/") else "/settings"
        return HttpResponse(
            302, "text/html; charset=utf-8",
            f"<!doctype html><html><head><meta http-equiv=\"refresh\" content=\"0;url={html.escape(safe, quote=True)}\">"
            f"</head><body><a href=\"{html.escape(safe, quote=True)}\">continue</a></body></html>".encode("utf-8"),
            (("Location", safe),),
        )


def _fail(status: int, message: str) -> HttpResponse:
    return HttpResponse.json(status, {"ok": False, "reply": message, "error": message})


RESPONSE_CACHE_MAX = 64

_response_cache: OrderedDict[str, tuple[bytes, str, str]] = OrderedDict()


def clear_response_cache() -> None:
    _response_cache.clear()


def _response_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def _not_modified(etag: str, content_type: str) -> HttpResponse:
    return HttpResponse(304, content_type, b"", (("ETag", etag),))


def _cached_response(key: str, if_none_match: str) -> HttpResponse | None:
    hit = _response_cache.get(key)
    if hit is None:
        return None
    body, etag, content_type = hit
    _response_cache.move_to_end(key)
    if if_none_match in (etag, "*"):
        return _not_modified(etag, content_type)
    return HttpResponse(200, content_type, body, (("ETag", etag),))


def _store_response(key: str, body: bytes, content_type: str, if_none_match: str) -> HttpResponse:
    etag = _response_etag(body)
    if len(_response_cache) >= RESPONSE_CACHE_MAX:
        _response_cache.popitem(last=False)
    _response_cache[key] = (body, etag, content_type)
    if if_none_match in (etag, "*"):
        return _not_modified(etag, content_type)
    return HttpResponse(200, content_type, body, (("ETag", etag),))


def _minute_bucket(now: datetime) -> str:
    return now.replace(second=0, microsecond=0).isoformat()


def openapi_spec() -> dict:
    from bot.theme import BRAND

    return {
        "openapi": "3.0.0",
        "info": {"title": BRAND, "version": "0.1.0"},
        "paths": {
            "/chat": {
                "post": {
                    "summary": "Chat with Muse",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Chat reply"}},
                }
            },
            "/session": {
                "post": {
                    "summary": "Run a scheduled session now",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Session brief"}},
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
            "/feeds": {
                "get": {
                    "summary": "Feed health page",
                    "responses": {"200": {"description": "Feed health page"}},
                }
            },
            "/replay.csv": {
                "get": {
                    "summary": "Replay export as CSV",
                    "responses": {"200": {"description": "Replay steps, fills, or benchmark CSV"}},
                }
            },
            "/cards": {
                "get": {
                    "summary": "Trade cards page",
                    "responses": {"200": {"description": "Trade cards page"}},
                }
            },
            "/cards.json": {
                "get": {
                    "summary": "Trade cards JSON",
                    "responses": {"200": {"description": "Trade cards data"}},
                }
            },
            "/video": {
                "get": {
                    "summary": "Video brief composition",
                    "responses": {"200": {"description": "Video brief page"}},
                }
            },
            "/chartread": {
                "post": {
                    "summary": "Read a chart screenshot against engine positioning",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Chart reading"}},
                }
            },
            "/docread": {
                "post": {
                    "summary": "Segment and read a document screenshot",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Document reading"}},
                }
            },
            "/share": {
                "get": {
                    "summary": "Shareable trade card page",
                    "responses": {"200": {"description": "Share page"}},
                }
            },
            "/action": {
                "get": {
                    "summary": "Run a read-only tool and render the result page",
                    "responses": {"200": {"description": "Tool result page"}},
                },
                "post": {
                    "summary": "Run any tool from a dashboard form",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Tool result page"}},
                },
            },
            "/settings": {
                "get": {
                    "summary": "Global settings form",
                    "responses": {"200": {"description": "Settings page"}},
                },
                "post": {
                    "summary": "Save global settings",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Saved settings"}},
                },
            },
            "/live": {
                "get": {
                    "summary": "Live ordering status plus arm/disarm toggle",
                    "responses": {"200": {"description": "Live page"}},
                },
                "post": {
                    "summary": "Arm (enable) or disarm (kill) live ordering",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Live page with result"}},
                },
            },
            "/holdings": {
                "post": {
                    "summary": "Save operator holdings",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Saved holdings"}},
                }
            },
            "/profile": {
                "post": {
                    "summary": "Save operator profile",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Saved profile"}},
                }
            },
            "/dashboard": {
                "get": {
                    "summary": "Dashboard with tool actions",
                    "responses": {"200": {"description": "Dashboard page"}},
                }
            },
            "/board": {
                "get": {
                    "summary": "Regime board",
                    "responses": {"200": {"description": "Board page"}},
                }
            },
            "/board/live": {
                "get": {
                    "summary": "Live board fragment",
                    "responses": {"200": {"description": "Board table fragment"}},
                }
            },
            "/builder": {
                "get": {
                    "summary": "Visual strategy builder",
                    "responses": {"200": {"description": "Builder form plus preview"}},
                }
            },
            "/strategy": {
                "post": {
                    "summary": "Evaluate a user-stated strategy",
                    "requestBody": {"required": True},
                    "responses": {"200": {"description": "Strategy feedback"}},
                }
            },
            "/chart": {
                "get": {
                    "summary": "Gamma chart",
                    "responses": {"200": {"description": "Chart page"}},
                }
            },
            "/demo": {
                "get": {
                    "summary": "Demo brief",
                    "responses": {"200": {"description": "Demo page"}},
                }
            },
            "/shock": {
                "get": {
                    "summary": "Shock repricing",
                    "responses": {"200": {"description": "Shock page"}},
                }
            },
            "/card": {
                "get": {
                    "summary": "Single trade card",
                    "responses": {"200": {"description": "Card page"}},
                }
            },
            "/earnings": {
                "get": {
                    "summary": "Earnings radar",
                    "responses": {"200": {"description": "Earnings page"}},
                }
            },
            "/pnl": {
                "get": {
                    "summary": "Paper P&L",
                    "responses": {"200": {"description": "P&L page"}},
                }
            },
            "/trackrecord": {
                "get": {
                    "summary": "Paper track record",
                    "responses": {"200": {"description": "Track record page"}},
                }
            },
        },
    }


def health_detail(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    providers = []
    if _env_value("DISCORD_WEBHOOK_URL", "", env):
        providers.append("discord")
    if _env_value("TELEGRAM_BOT_TOKEN", "", env) and _env_value("TELEGRAM_CHAT_ID", "", env):
        providers.append("telegram")
    last_ingest = None
    feed_used = None
    snapshot_id = None
    for entry in journal.entries():
        if entry.kind in ("snapshot_ingested", "bars_ingested"):
            last_ingest = entry.timestamp_utc
            feed_used = entry.payload.get("source_name") or entry.payload.get("feed_name")
            snapshot_id = entry.payload.get("snapshot_id") or snapshot_id
    age_seconds = (now - last_ingest).total_seconds() if last_ingest is not None else None
    quote_age_seconds: dict[str, float | None] = {}
    for symbol in store.list_underlyings():
        bar = store.read_latest_bar(symbol)
        quote_age_seconds[symbol] = (
            (now - bar.timestamp_utc).total_seconds() if bar is not None else None
        )
    return {
        "ok": True,
        "last_ingest_utc": last_ingest.isoformat() if last_ingest is not None else None,
        "feed_used": feed_used,
        "snapshot_id": snapshot_id,
        "snapshot_age_seconds": age_seconds,
        "quote_age_seconds": quote_age_seconds,
        "underlyings": store.list_underlyings(),
        "push_providers": providers,
        "model_key_set": bool(_env_value("META_MODEL_API_KEY", "", env)),
        "user_key_set": bool(_env_value("USER_MODEL_API_KEY", "", env)),
        "user_model": user_model_status(env).get("user_model"),
        "feeds": feed_health(journal, now),
    }


def _authorized(clean: str, auth_token: Optional[str], authorization: str) -> bool:
    if not auth_token:
        return True
    if clean in OPEN_PATHS:
        return True
    return authorization.strip() == f"Bearer {auth_token}"


def _page_underlying(path: str, store: SnapshotStore) -> str:
    query_string = path.split("?", 1)[1] if "?" in path else ""
    params = parse_qs(query_string)
    raw = (params.get("underlying", [""])[0] or "").strip()
    return raw or store.read_settings().default_underlying


def _truthy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _multipart_file(payload: bytes) -> bytes | None:
    first_line, _, _ = payload.partition(b"\r\n")
    if not first_line.startswith(b"--") or len(first_line) < 4:
        first_line, _, _ = payload.partition(b"\n")
        if not first_line.startswith(b"--") or len(first_line) < 4:
            return None
    boundary = first_line.strip()
    for chunk in payload.split(boundary):
        if b"filename=" not in chunk and b"Content-Type: image" not in chunk:
            continue
        _, _, body = chunk.partition(b"\r\n\r\n")
        if not body:
            _, _, body = chunk.partition(b"\n\n")
        return body.strip(b"\r\n") or None
    return None


ACTION_POST_ONLY = frozenset({
    "set_holdings", "set_settings", "set_profile", "run_session",
    "backfill_iv", "load_snapshot", "read_chart_image", "read_document",
    "take_paper_trade", "take_live_trade", "onboarding_step",
    "set_alert_rule", "cancel_alert_rule",
    "set_user_model", "clear_user_model",
})

ACTION_ARG_KEYS = (
    "query", "command", "question", "symbol", "symbols", "count", "index",
    "snapshot", "shock_pct", "percent", "pct", "text", "holdings", "answer",
    "live_models", "grounding", "cards", "paper_trade",
    "commission", "commission_per_fill", "slippage_bps", "slippage",
    "rule", "rule_id", "id",
)

ACTION_FLAG_KEYS = frozenset({"live_models", "grounding", "cards", "paper_trade"})


def _action_args(params: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for key in ACTION_ARG_KEYS:
        if key not in params:
            continue
        value = params[key]
        if isinstance(value, list):
            value = value[0] if value else ""
        if key in ACTION_FLAG_KEYS:
            args[key] = _truthy(value)
        else:
            args[key] = value
    if "pct" in args and "shock_pct" not in args:
        args["shock_pct"] = args.pop("pct")
    return args


def _action_html(tool: str, underlying: str, output: str, now: datetime, store: SnapshotStore) -> str:
    from bot.theme import page

    settings = store.read_settings()
    stamp = f"{tool} {underlying} at {now.isoformat()}"
    body = f"<pre>{html.escape(output)}</pre>"
    return page(
        f"{tool} {underlying}", f"{tool}, from stored snapshots", stamp, body,
        underlying, "dashboard", settings, f"/dashboard?underlying={underlying}",
    )


def _settings_html(store: SnapshotStore) -> str:
    from bot.theme import page

    settings = store.read_settings()
    options = "".join(
        f"<option value=\"{choice}\"{' selected' if choice == settings.theme else ''}>{choice}</option>"
        for choice in ("dark", "light")
    )
    defaults = user_model_form_defaults()
    user_model = html.escape(defaults["model_id"], quote=True)
    user_base = html.escape(defaults["base_url"], quote=True)
    body = (
        "<form class=\"block\" method=\"post\" action=\"/settings\">"
        f"<label class=\"field\">theme<select name=\"theme\">{options}</select></label>"
        "<label class=\"field\">default_underlying"
        f"<input name=\"default_underlying\" value=\"{html.escape(settings.default_underlying, quote=True)}\" size=\"8\">"
        "</label><label class=\"field\">refresh_seconds"
        f"<input name=\"refresh_seconds\" value=\"{settings.refresh_seconds}\" size=\"6\">"
        "</label>"
        f"<label class=\"field\">live_auto_send<select name=\"live_auto_send\">"
        f"<option value=\"0\"{' selected' if not settings.live_auto_send else ''}>off</option>"
        f"<option value=\"1\"{' selected' if settings.live_auto_send else ''}>on</option>"
        "</select></label>"
        "<label class=\"field\">live_min_confidence"
        f"<input name=\"live_min_confidence\" value=\"{settings.live_min_confidence:.2f}\" size=\"6\">"
        "</label><input type=\"hidden\" name=\"next\" value=\"/settings\">"
        "<button class=\"solid\" type=\"submit\">save settings</button></form>"
        "<p class=\"sub\">live_auto_send on means qualifying trade cards "
        "(confidence at or above live_min_confidence) are sent to the live broker "
        "when the adapter is armed; the adapter itself is armed only on the "
        "<a href=\"/live\">live page</a>, never here.</p>"
        "<form class=\"block\" method=\"post\" action=\"/settings\">"
        "<label class=\"field\">model key (session only, never stored)"
        "<input type=\"password\" name=\"user_model_key\" value=\"\" size=\"24\" autocomplete=\"off\">"
        "</label><label class=\"field\">model endpoint"
        f"<input name=\"user_model_base_url\" value=\"{user_base}\" size=\"32\">"
        "</label><label class=\"field\">model id"
        f"<input name=\"user_model_id\" value=\"{user_model}\" size=\"20\">"
        "</label><label class=\"field\">clear the session key"
        "<input type=\"checkbox\" name=\"clear_user_model\" value=\"1\">"
        "</label><input type=\"hidden\" name=\"next\" value=\"/settings\">"
        "<button class=\"solid\" type=\"submit\">save model key</button></form>"
        f"<p class=\"sub\">{html.escape(settings_summary(settings))} -- every page reads this object</p>"
        "<p class=\"sub\">The model key lives in server memory only and clears on restart; "
        "use .env for a persistent key. Paste it here, never into chat.</p>"
    )
    return page(
        "settings", "settings, shared by all pages", settings_summary(settings), body,
        settings.default_underlying, "settings", settings, "/settings",
    )


def _live_html(store: SnapshotStore, journal: Journal, now: datetime, note: str = "") -> str:
    import bot.autonomy as autonomy
    from bot.live_trade import live_status_text
    from bot.theme import page

    settings = store.read_settings()
    status = live_status_text(store, journal, now)
    armed = autonomy.live_enabled
    badge = (
        "<p><span class=\"fresh\">armed</span> -- live orders can move money</p>"
        if armed
        else "<p><span class=\"stale\">off</span> -- live orders are blocked</p>"
    )
    note_html = f"<pre>{html.escape(note)}</pre>" if note else ""
    body = (
        f"<p class=\"sub\">{html.escape(status)}</p>{badge}{note_html}"
        "<h2>Arm</h2>"
        "<form class=\"block\" method=\"post\" action=\"/live\">"
        "<input type=\"hidden\" name=\"action\" value=\"enable\">"
        "<label class=\"field\">operator signature"
        "<input name=\"signature\" value=\"\" size=\"24\" autocomplete=\"off\">"
        "</label>"
        "<label class=\"field\">paper evidence reviewed"
        "<input type=\"checkbox\" name=\"paper_evidence_reviewed\" value=\"1\">"
        "</label>"
        "<label class=\"field\">risk limits set"
        "<input type=\"checkbox\" name=\"risk_limits_set\" value=\"1\">"
        "</label>"
        "<label class=\"field\">kill switch tested"
        "<input type=\"checkbox\" name=\"kill_switch_tested\" value=\"1\">"
        "</label>"
        "<button class=\"solid\" type=\"submit\">arm live ordering</button></form>"
        "<p class=\"sub\">Arming needs a signature, all three checklist boxes, "
        "journaled paper evidence (clean days, drawdown bound, a halt or kill drill), "
        "and a clean halt/kill state. Misses journal live_blocked with the reason.</p>"
        "<h2>Disarm</h2>"
        "<form class=\"block\" method=\"post\" action=\"/live\">"
        "<input type=\"hidden\" name=\"action\" value=\"kill\">"
        "<button class=\"solid\" type=\"submit\">kill switch (disarm)</button></form>"
        "<h2>Auto-send threshold</h2>"
        "<form class=\"block\" method=\"post\" action=\"/settings\">"
        f"<label class=\"field\">live_auto_send<select name=\"live_auto_send\">"
        f"<option value=\"0\"{' selected' if not settings.live_auto_send else ''}>off</option>"
        f"<option value=\"1\"{' selected' if settings.live_auto_send else ''}>on</option>"
        "</select></label>"
        "<label class=\"field\">live_min_confidence"
        f"<input name=\"live_min_confidence\" value=\"{settings.live_min_confidence:.2f}\" size=\"6\">"
        "</label><input type=\"hidden\" name=\"next\" value=\"/live\">"
        "<button class=\"solid\" type=\"submit\">save threshold</button></form>"
    )
    return page(
        "live ordering", "live ordering, armed only with signature plus evidence",
        status, body, settings.default_underlying, "live", settings, "/live",
    )


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
    session_controller: Optional[SessionController] = None,
    if_none_match: str = "",
) -> HttpResponse:
    if method == "HEAD":
        method = "GET"
    clean = path.split("?")[0]
    if method == "GET" and clean == HEALTH_PATH:
        return HttpResponse.json(200, {"ok": True})
    if method == "GET" and clean == HEALTH_DETAIL_PATH:
        return HttpResponse.json(200, health_detail(store, journal, now))
    if method == "GET" and clean == "/feeds":
        return HttpResponse.html(200, feed_health_html(feed_health(journal, now)))
    if method == "GET" and clean == "/replay.csv":
        from bot.replay import REPLAY_EXPORT_SECTIONS, replay_export

        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = _page_underlying(path, store)
        section = (params.get("section", ["steps"])[0] or "steps").strip() or "steps"
        if section not in REPLAY_EXPORT_SECTIONS:
            return HttpResponse.json(
                400, {"error": f"unknown replay section {section} -- want one of {', '.join(REPLAY_EXPORT_SECTIONS)}"}
            )
        try:
            body = replay_export(underlying, store, section)
        except ValueError as exc:
            return HttpResponse.json(400, {"error": str(exc)})
        filename = f"replay-{underlying}-{section}.csv"
        return HttpResponse(
            200, "text/csv", body.encode("utf-8"),
            (("Content-Disposition", f'attachment; filename="{filename}"'),),
        )
    if method == "GET" and clean == "/openapi.json":
        return HttpResponse.json(200, openapi_spec())
    if not _authorized(clean, auth_token, authorization):
        return _fail(401, "missing or invalid bearer token")
    if method == "POST" and clean == "/chat":
        if not isinstance(payload, dict):
            return _fail(400, "missing chat text")
        try:
            request = parse_chat_payload(payload)
        except ValueError as exc:
            return _fail(400, str(exc))
        reply = handle_chat(request, store, journal, config, now)
        return HttpResponse.json(200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error})
    if method == "POST" and clean == "/voice":
        from bot.voice import handle_voice_note

        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return _fail(400, "voice needs raw audio bytes")
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"

        def _flag(name: str, default: bool) -> bool:
            raw_flag = (params.get(name, [""])[0] or "").strip().lower()
            if not raw_flag:
                return default
            return raw_flag not in ("0", "false", "no", "off")

        voice_text = handle_voice_note(
            bytes(payload), underlying, store, journal, now,
            live_models=_flag("live_models", True),
            grounding=_flag("grounding", False),
            cards=_flag("cards", False),
        )
        return HttpResponse.json(200, {"ok": True, "reply": voice_text})
    if method == "POST" and clean in ("/chartread", "/docread"):
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return _fail(400, "image endpoint needs raw png or jpeg bytes")
        image = bytes(payload)
        if image.startswith(b"--"):
            extracted = _multipart_file(image)
            if extracted is None:
                return _fail(400, "upload needs one image file part")
            image = extracted
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
        raw_flag = (params.get("live_models", [""])[0] or "").strip().lower()
        live = raw_flag not in ("0", "false", "no", "off") if raw_flag else True
        if clean == "/chartread":
            from bot.chartread import read_chart_image

            question = (params.get("question", [""])[0] or "").strip()
            image_text = read_chart_image(
                image, underlying, store, journal, now,
                live_models=live, question=question,
            )
        else:
            from bot.docread import read_document

            question = (params.get("question", [""])[0] or "").strip()
            image_text = read_document(
                image, question, underlying, store, journal, now, live_models=live
            )
        return HttpResponse.json(200, {"ok": True, "reply": image_text})
    if method == "POST" and clean == SESSION_PATH:
        if not isinstance(payload, dict):
            return _fail(400, "invalid session body")
        underlying = payload.get("underlying", "SPY")
        if underlying is None:
            underlying = "SPY"
        if not isinstance(underlying, str):
            return _fail(400, "invalid session body")
        if not underlying.strip():
            underlying = "SPY"
        if session_controller is None:
            return _fail(503, "sessions not configured")

        def _session_flag(name: str, default: bool) -> bool:
            if name not in payload:
                return False if isinstance(payload, FormData) else default
            return _truthy(payload.get(name))

        text, symbols = session_controller.run(
            underlying,
            live_models=_session_flag("live_models", False),
            grounding=_session_flag("grounding", False),
            cards=_session_flag("cards", False),
            now=now,
            paper_trade=_session_flag("paper_trade", True),
        )
        if isinstance(payload, FormData):
            return HttpResponse.redirect(str(payload.get("next", f"/dashboard?underlying={underlying}")))
        return HttpResponse.json(200, {"ok": True, "reply": text, "underlyings": symbols})
    if method == "POST" and clean == "/mcp":
        if not isinstance(payload, dict):
            return _fail(400, "invalid MCP envelope")
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
            raw_params = payload.get("params")
            mcp_params = raw_params if isinstance(raw_params, dict) else {}
            name = mcp_params.get("name") or ""
            raw_arguments = mcp_params.get("arguments")
            mcp_arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
            underlying = mcp_params.get("underlying") or "SPY"
            reply = dispatch_tool(
                str(name), mcp_arguments, store, journal, config, now, underlying=str(underlying),
                session_controller=session_controller,
            )
            return HttpResponse.json(200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error})
        return _fail(400, f"unknown MCP method {mcp_method}")
    if method == "GET" and clean in ("/cards", "/cards.json"):
        want_json = clean == "/cards.json"
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = _page_underlying(path, store)
        query = (params.get("query", ["trade ideas"])[0] or "trade ideas").strip() or "trade ideas"
        cards_key = peek_trade_cards_key(query, underlying, store, now)
        response_key = (
            f"cards:{clean}:{cards_key}:{store.read_settings()!r}" if cards_key is not None else None
        )
        if response_key is not None:
            hit = _cached_response(response_key, if_none_match)
            if hit is not None:
                return hit
        try:
            cards = query_trade_cards(query, underlying, store, journal, now)
        except ValueError as exc:
            if want_json:
                return HttpResponse.json(400, {"error": str(exc)})
            return HttpResponse.html(400, _cards_error_html(str(exc)))
        if cards is None:
            reason = PROFILE_REQUIRED if store.read_profile() is None else OPTIONS_DISABLED
            if want_json:
                return HttpResponse.json(403, {"error": reason})
            return HttpResponse.html(403, _cards_error_html(reason))
        if want_json:
            raw = json.dumps(
                {"underlying": underlying, "count": len(cards), "cards": cards_to_json(cards)["cards"]}
            ).encode("utf-8")
            if response_key is None:
                return HttpResponse(200, "application/json", raw)
            return _store_response(response_key, raw, "application/json", if_none_match)
        text = trade_cards_html(cards, underlying, store.read_settings(), profile=store.read_profile())
        if response_key is None:
            return HttpResponse.html(200, text)
        return _store_response(response_key, text.encode("utf-8"), "text/html; charset=utf-8", if_none_match)
    if method == "GET" and clean == "/chart":
        underlying = _page_underlying(path, store)
        found = store.read_latest(underlying)
        if found is None:
            return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(underlying)
        profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
        return HttpResponse.html(
            200, gamma_chart_html(underlying, snapshot, profile, bar, now, store.read_settings())
        )
    if method == "GET" and clean == "/board":
        board_parts = []
        for name in sorted(store.list_underlyings()):
            found = store.read_latest(name)
            if found is None:
                board_parts.append(f"{name}:none")
                continue
            snapshot_id, snapshot = found
            board_parts.append(
                f"{name}:{snapshot_id}:{chain_fingerprint(snapshot)}"
                f":{bar_signature(store.read_latest_bar(name))}"
            )
        board_digest = hashlib.sha256("\n".join(board_parts).encode("utf-8")).hexdigest()
        board_key = f"board:{board_digest}:{store.read_settings()!r}:{_minute_bucket(now)}"
        hit = _cached_response(board_key, if_none_match)
        if hit is not None:
            return hit
        return _store_response(
            board_key, regime_board_html(store, now).encode("utf-8"),
            "text/html; charset=utf-8", if_none_match,
        )
    if method == "GET" and clean == "/board/live":
        from bot.charts import live_board_fragment

        return HttpResponse.html(200, live_board_fragment(store, now))
    if method == "GET" and clean == "/builder":
        from bot.builder import builder_html, builder_preview_text

        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        flat: dict[str, str] = {
            key: (values[0] if values else "") for key, values in params.items()
        }
        if "underlying" not in flat:
            flat["underlying"] = _page_underlying(path, store)
        preview = ""
        if params:
            preview = builder_preview_text(store, journal, now, flat)
        return HttpResponse.html(200, builder_html(store, journal, now, flat, preview))
    if method == "POST" and clean == "/strategy":
        from bot.strategy import evaluate_strategy, parse_strategy

        text = ""
        underlying = _page_underlying(path, store)
        if isinstance(payload, dict):
            text = str(payload.get("text", payload.get("strategy", "")) or "")
            raw_underlying = str(payload.get("underlying", "") or "").strip()
            if raw_underlying:
                underlying = raw_underlying.upper()
            if not text.strip():
                return _fail(400, "strategy needs text like 'my strategy: long SPY 7-30 DTE'")
        else:
            return _fail(400, "strategy needs a JSON object with text")
        return HttpResponse.json(
            200, {"ok": True, "reply": evaluate_strategy(parse_strategy(text, underlying, store), store, journal, now)}
        )
    if method == "GET" and clean == "/deployments/page":
        from bot.deployments import deployments_html

        return HttpResponse.html(200, deployments_html(store, journal, now))
    if method == "GET" and clean == "/deployments":
        from bot.deployments import deployments_text

        return HttpResponse.json(200, {"ok": True, "reply": deployments_text(store, journal)})
    if method == "POST" and clean == "/deployments":
        from bot.deployments import create_deployment

        if not isinstance(payload, dict):
            return _fail(400, "deployment needs a JSON object with strategy_id and symbols")
        try:
            raw_symbols = payload.get("symbols", payload.get("symbol", "SPY"))
            if isinstance(raw_symbols, (list, tuple)):
                symbols = [str(item) for item in raw_symbols]
            else:
                symbols = [item for item in str(raw_symbols).replace(",", " ").split() if item]
            deployment = create_deployment(
                store,
                str(payload.get("strategy_id", payload.get("strategy", "")) or ""),
                symbols,
                mode=str(payload.get("mode", "paper") or "paper"),
                capital_cap=float(payload.get("capital", payload.get("capital_cap", 50000.0)) or 50000.0),
            )
        except ValueError as exc:
            return _fail(400, str(exc))
        journal.append(
            now, "deployment_created",
            {"deployment_id": deployment.id, "strategy_id": deployment.strategy_id, "symbols": list(deployment.symbols), "mode": deployment.mode, "capital_cap": deployment.capital_cap},
        )
        return HttpResponse.json(
            200,
            {"ok": True, "reply": f"deployment {deployment.id} created: {deployment.strategy_id} on {','.join(deployment.symbols)} {deployment.mode} capital {deployment.capital_cap:.0f}"},
        )
    if method == "POST" and clean == "/deployments/run":
        from bot.deployments import run_deployments

        return HttpResponse.json(200, {"ok": True, "reply": run_deployments(store, journal, now) or "deployments: nothing opened or closed"})
    if method == "GET" and clean == "/earnings":
        from bot.earnings import earnings_html

        return HttpResponse.html(200, earnings_html(store, now, _page_underlying(path, store)))
    if method == "GET" and clean == "/pnl":
        return HttpResponse.html(
            200,
            _action_html(
                "paper_pnl", _page_underlying(path, store),
                paper_pnl_text(journal, store), now, store,
            ),
        )
    if method == "GET" and clean == "/trackrecord":
        from bot.trackrecord import trackrecord_html

        return HttpResponse.html(
            200, trackrecord_html(journal, store, _page_underlying(path, store), now)
        )
    if method == "GET" and clean == "/video":
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = _page_underlying(path, store)
        if store.read_latest(underlying) is None:
            return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
        brief = build_video_brief([underlying], store, journal, now)
        storyboard = params.get("storyboard", [""])[0] == "1"
        return HttpResponse.html(200, composition_html(brief, storyboard=storyboard))
    if method == "GET" and clean == "/dashboard":
        from bot.dashboard import dashboard_html

        underlying = _page_underlying(path, store)
        if not store.list_underlyings():
            return HttpResponse.html(400, _cards_error_html(f"dashboard: {empty_store_text()}"))
        return HttpResponse.html(200, dashboard_html(store, journal, now, underlying))
    if method == "GET" and clean == "/shock":
        from bot.demo import shock_html

        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = _page_underlying(path, store)
        try:
            pct = float((params.get("pct", [""])[0] or "").strip())
        except ValueError:
            return HttpResponse.html(400, _cards_error_html("usage: /shock?underlying=SPY&pct=-2"))
        if store.read_latest(underlying) is None:
            return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
        return HttpResponse.html(200, shock_html(underlying, store, journal, now, pct))
    if method == "GET" and clean in ("/demo", "/"):
        underlying = _page_underlying(path, store)
        found = store.read_latest(underlying)
        if found is None:
            return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
        snapshot_id, snapshot = found
        demo_key = (
            f"demo:{clean}:{underlying}:{snapshot_id}:{chain_fingerprint(snapshot)}:"
            f"{bar_signature(store.read_latest_bar(underlying))}:"
            f"{store.read_profile()!r}:{store.read_settings()!r}:{_minute_bucket(now)}"
        )
        hit = _cached_response(demo_key, if_none_match)
        if hit is not None:
            return hit
        return _store_response(
            demo_key, demo_html(underlying, store, journal, now).encode("utf-8"),
            "text/html; charset=utf-8", if_none_match,
        )
    if method == "GET" and clean == "/card":
        from bot.demo import demo_cards
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = _page_underlying(path, store)
        if store.read_profile() is None:
            return HttpResponse.html(403, _cards_error_html(PROFILE_REQUIRED))
        snapshot_id = (params.get("snapshot", [""])[0] or "").strip()
        try:
            index = int((params.get("index", ["0"])[0] or "0").strip())
        except ValueError:
            return HttpResponse.html(400, _cards_error_html("index must be an integer"))
        if not snapshot_id:
            latest = store.read_latest(underlying)
            if latest is None:
                return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
            snapshot_id = latest[0]
        _, cards = demo_cards(underlying, store, journal, now, snapshot_id)
        if not cards:
            return HttpResponse.html(400, _cards_error_html(f"no cards for {underlying} snapshot {snapshot_id}"))
        if index < 0 or index >= len(cards):
            return HttpResponse.html(400, _cards_error_html(f"index {index} out of range (0-{len(cards) - 1})"))
        page = trade_cards_html(
            cards[index : index + 1], underlying, store.read_settings(), start_index=index,
            profile=store.read_profile(),
        )
        if (params.get("debate", [""])[0] or "").strip() == "1":
            from bot.debate import attach_debate, debate_card

            live = (params.get("live_models", [""])[0] or "").strip() == "1"
            page = attach_debate(
                page, debate_card(cards[index], journal, now, live_models=live)
            )
        return HttpResponse.html(200, page)
    if method == "GET" and clean == "/share":
        from bot.cards import render_signal_art, share_html
        from bot.demo import demo_cards

        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = _page_underlying(path, store)
        if store.read_profile() is None:
            return HttpResponse.html(403, _cards_error_html(PROFILE_REQUIRED))
        snapshot_id = (params.get("snapshot", [""])[0] or "").strip()
        try:
            index = int((params.get("index", ["0"])[0] or "0").strip())
        except ValueError:
            return HttpResponse.html(400, _cards_error_html("index must be an integer"))
        if not snapshot_id:
            latest = store.read_latest(underlying)
            if latest is None:
                return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
            snapshot_id = latest[0]
        _, cards = demo_cards(underlying, store, journal, now, snapshot_id)
        if not cards:
            return HttpResponse.html(400, _cards_error_html(f"no cards for {underlying} snapshot {snapshot_id}"))
        if index < 0 or index >= len(cards):
            return HttpResponse.html(400, _cards_error_html(f"index {index} out of range (0-{len(cards) - 1})"))
        art = url = art_hash = None
        if (params.get("live_models", [""])[0] or "").strip() == "1":
            rendered = render_signal_art(cards[index], journal, now)
            if rendered is not None:
                art, url, art_hash = rendered["art"], rendered["url"], rendered["art_hash"]
        return HttpResponse.html(
            200, share_html(cards[index], art=art, url=url, art_hash=art_hash,
                            settings=store.read_settings())
        )
    if method == "GET" and clean == "/action":
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        tool = (params.get("tool", [""])[0] or "").strip()
        underlying = _page_underlying(path, store)
        if tool not in TOOL_REGISTRY:
            return HttpResponse.html(400, _cards_error_html(f"unknown tool {tool!r}"))
        if tool in ACTION_POST_ONLY:
            return HttpResponse.html(
                400, _cards_error_html(f"tool {tool} needs a POST form; GET would mutate on refresh")
            )
        reply = dispatch_tool(
            tool, _action_args(params), store, journal, config, now, underlying=underlying,
            session_controller=session_controller,
        )
        if not reply.ok:
            return HttpResponse.html(400, _cards_error_html(reply.error or reply.reply))
        return HttpResponse.html(200, _action_html(tool, underlying, reply.reply, now, store))
    if method == "POST" and clean == "/action":
        if not isinstance(payload, dict):
            return HttpResponse.html(400, _cards_error_html("action needs tool plus arguments"))
        tool = str(payload.get("tool", "")).strip()
        underlying = str(payload.get("underlying", "") or "").strip() or _page_underlying(path, store)
        if tool not in TOOL_REGISTRY:
            return HttpResponse.html(400, _cards_error_html(f"unknown tool {tool!r}"))
        args = _action_args({key: value for key, value in payload.items() if key not in ("tool", "underlying", "next")})
        if "symbol" in args and "underlying" not in payload:
            underlying = str(args["symbol"]).strip().upper() or underlying
        reply = dispatch_tool(
            tool, args, store, journal, config, now, underlying=underlying,
            session_controller=session_controller,
        )
        if not reply.ok:
            return HttpResponse.html(400, _cards_error_html(reply.error or reply.reply))
        return HttpResponse.html(200, _action_html(tool, underlying, reply.reply, now, store))
    if method == "GET" and clean == "/settings":
        return HttpResponse.html(200, _settings_html(store))
    if method == "GET" and clean == "/live":
        return HttpResponse.html(200, _live_html(store, journal, now))
    if method == "POST" and clean == "/live":
        if not isinstance(payload, dict):
            return HttpResponse.html(400, _cards_error_html("live needs an action plus arguments"))
        from bot.live_trade import enable_live_ui, kill_live_ui

        action = str(payload.get("action", "") or "").strip().lower()
        try:
            if action == "kill":
                note = kill_live_ui(store, journal, now)
            elif action == "enable":
                checklist = {
                    "paper_evidence_reviewed": _truthy(payload.get("paper_evidence_reviewed")),
                    "risk_limits_set": _truthy(payload.get("risk_limits_set")),
                    "kill_switch_tested": _truthy(payload.get("kill_switch_tested")),
                }
                note = enable_live_ui(
                    str(payload.get("signature", "") or ""), checklist,
                    store, journal, config, now,
                )
            else:
                return HttpResponse.html(400, _cards_error_html("live action must be enable or kill"))
        except ValueError as exc:
            return HttpResponse.html(400, _cards_error_html(str(exc)))
        if isinstance(payload, FormData) and str(payload.get("next", "") or ""):
            return HttpResponse.redirect(str(payload.get("next")))
        return HttpResponse.html(200, _live_html(store, journal, now, note))
    if method == "POST" and clean == "/settings":
        if not isinstance(payload, dict):
            return _fail(400, "settings must be an object or a form post")
        try:
            saved = save_settings_merge(payload, store, journal, now)
        except ValueError as exc:
            if isinstance(payload, FormData):
                return HttpResponse.html(400, _cards_error_html(str(exc)))
            return _fail(400, str(exc))
        if isinstance(payload, FormData):
            return HttpResponse.redirect(str(payload.get("next", "/settings")))
        user_status = user_model_status()
        return HttpResponse.json(200, {
            "ok": True,
            "settings": {
                "theme": saved.theme,
                "default_underlying": saved.default_underlying,
                "refresh_seconds": saved.refresh_seconds,
                "live_auto_send": saved.live_auto_send,
                "live_min_confidence": saved.live_min_confidence,
                "user_key_set": user_status.get("user_key_set"),
                "user_model": user_status.get("user_model"),
                "user_source": user_status.get("user_source"),
            },
        })
    if method == "POST" and clean == "/holdings":
        if not isinstance(payload, dict):
            return _fail(400, "holdings must be an object or a form post")
        args = {"holdings": payload["holdings"]} if "holdings" in payload else dict(payload)
        reply = dispatch_tool("set_holdings", args, store, journal, config, now,
                              underlying=_page_underlying(path, store))
        if isinstance(payload, FormData):
            if not reply.ok:
                return HttpResponse.html(400, _cards_error_html(reply.error or reply.reply))
            return HttpResponse.redirect(str(payload.get("next", "/dashboard")))
        status = 200 if reply.ok else 400
        return HttpResponse.json(status, {"ok": reply.ok, "reply": reply.reply, "error": reply.error})
    if method == "POST" and clean == "/profile":
        from bot.profile import parse_answers

        if not isinstance(payload, dict):
            return _fail(400, "profile must be an object or a form post")
        try:
            if "text" in payload:
                summary = save_profile_answers(parse_answers(str(payload.get("text", ""))), store, journal, now)
            else:
                summary = save_profile_answers(payload, store, journal, now)
        except ValueError as exc:
            if isinstance(payload, FormData):
                return HttpResponse.html(400, _cards_error_html(str(exc)))
            return _fail(400, str(exc))
        if isinstance(payload, FormData):
            return HttpResponse.redirect(str(payload.get("next", "/dashboard")))
        return HttpResponse.json(200, {"ok": True, "reply": summary, "error": ""})
    return HttpResponse.json(404, {"error": "not found"})


def _cards_error_html(message: str) -> str:
    from bot.theme import BRAND

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>trade cards | {BRAND}</title></head>"
        f"<body><h1>trade cards</h1><p>{html.escape(message, quote=True)}</p></body></html>"
    )
