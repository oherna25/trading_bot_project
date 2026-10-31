"""HTTP primitives, OpenAPI spec, and route_http."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs

from bot.charts import gamma_chart_html, regime_board_html
from bot.config import env_value as _env_value
from bot.demo import demo_html
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.research import (
    paper_pnl_text,
)
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore, empty_store_text, need_snapshot_text
from bot.tradecards import OPTIONS_DISABLED, PROFILE_REQUIRED, cards_to_json, query_trade_cards, trade_cards_html
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
    "take_paper_trade", "onboarding_step",
    "set_alert_rule", "cancel_alert_rule",
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
    body = (
        "<form class=\"block\" method=\"post\" action=\"/settings\">"
        f"<label class=\"field\">theme<select name=\"theme\">{options}</select></label>"
        "<label class=\"field\">default_underlying"
        f"<input name=\"default_underlying\" value=\"{html.escape(settings.default_underlying, quote=True)}\" size=\"8\">"
        "</label><label class=\"field\">refresh_seconds"
        f"<input name=\"refresh_seconds\" value=\"{settings.refresh_seconds}\" size=\"6\">"
        "</label><input type=\"hidden\" name=\"next\" value=\"/settings\">"
        "<button class=\"solid\" type=\"submit\">save settings</button></form>"
        f"<p class=\"sub\">{html.escape(settings_summary(settings))} -- every page reads this object</p>"
    )
    return page(
        "settings", "settings, shared by all pages", settings_summary(settings), body,
        settings.default_underlying, "settings", settings, "/settings",
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
            return HttpResponse.json(
                200, {"underlying": underlying, "count": len(cards), "cards": cards_to_json(cards)["cards"]}
            )
        return HttpResponse.html(
            200, trade_cards_html(cards, underlying, store.read_settings(), profile=store.read_profile())
        )
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
        return HttpResponse.html(200, regime_board_html(store, now))
    if method == "GET" and clean == "/board/live":
        from bot.charts import live_board_fragment

        return HttpResponse.html(200, live_board_fragment(store, now))
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
        if store.read_latest(underlying) is None:
            return HttpResponse.html(400, _cards_error_html(need_snapshot_text(underlying, store)))
        return HttpResponse.html(200, demo_html(underlying, store, journal, now))
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
        return HttpResponse.json(200, {
            "ok": True,
            "settings": {
                "theme": saved.theme,
                "default_underlying": saved.default_underlying,
                "refresh_seconds": saved.refresh_seconds,
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
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>trade cards | {BRAND}</title></head>"
        f"<body><h1>trade cards</h1><p>{html.escape(message, quote=True)}</p></body></html>"
    )
