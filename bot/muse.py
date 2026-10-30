from __future__ import annotations

import base64
import html
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import parse_qs

from bot.charts import gamma_chart_html, regime_board_html
from bot.config import auth_token as _auth_token
from bot.demo import demo_html
from bot.exposure import compute_gex
from bot.feeds import FeedContext, default_context, ingest_bars, ingest_chain, live_context
from bot.ivrank import histories_for, iv_rank
from bot.journal import Journal
from bot.models import MODEL_REGISTRY, HttpPost, model_api_key, receipt_payload, run_analyst
from bot.persist import bundle_mtimes, refresh_bundle, save_bundle
from bot.research import (
    dex_brief_response,
    handle_chat_query,
    ideas_response,
    journal_tail_text,
    paper_pnl_text,
    route_chat,
    scheduled_brief,
    vex_brief_response,
)
from bot.schedule import normalize_underlyings, run_session_core, tick_symbols
from bot.shapes import OperatorConfig, default_operator_config
from bot.store import SnapshotStore
from bot.tradecards import OPTIONS_DISABLED, PROFILE_REQUIRED, cards_response, cards_to_json, query_trade_cards, trade_cards_html
from bot.video import build_video_brief, composition_html, video_response
from bot.wheel import wheel_response


@dataclass(frozen=True)
class MuseChatRequest:
    text: str
    underlying: str = "SPY"
    live_models: bool = False
    grounding: bool = False
    cards: bool = False
    llm_router: bool = False


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


def _trade_cards_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    query = args.get("query", "trade ideas") if isinstance(args, dict) else "trade ideas"
    if not isinstance(query, str) or not query.strip():
        query = "trade ideas"
    return cards_response(query, underlying, store, journal, now)


def _trade_ideas_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    query = args.get("query", "trade ideas") if isinstance(args, dict) else "trade ideas"
    if not isinstance(query, str) or not query.strip():
        query = "trade ideas"
    return ideas_response(query, underlying, store, journal, now)


def _wheel_ideas_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    query = args.get("query", "wheel ideas") if isinstance(args, dict) else "wheel ideas"
    if not isinstance(query, str) or not query.strip():
        query = "wheel ideas"
    return wheel_response(query, underlying, store, journal, now)


def _video_brief_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    query = args.get("query", "video brief") if isinstance(args, dict) else "video brief"
    if not isinstance(query, str) or not query.strip():
        query = "video brief"
    return video_response(query, underlying, store, journal, now)


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


def _replay_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, config
    from bot.replay import replay_response

    return replay_response(underlying, store, journal, now)


def _iv_rank_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, now
    symbols = sorted(store.list_underlyings())
    if not symbols:
        return "iv ranks: none stored"
    if underlying in symbols:
        symbols.remove(underlying)
        symbols.insert(0, underlying)
    histories = histories_for(store, symbols)
    lines = ["iv ranks:"]
    for symbol in symbols:
        points = histories.get(symbol, ())
        rank = iv_rank(points)
        if rank is None:
            lines.append(f"{symbol}: n/a ({len(points)} points)")
        else:
            lines.append(f"{symbol}: {rank:.1f} ({len(points)} points)")
    return "\n".join(lines)


def _backfill_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, config, underlying
    from bot.ivrank import backfill_iv_history

    return backfill_iv_history(store, journal, now)


def _diff_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, config, journal, now
    from bot.diff import snapshot_diff_text

    return snapshot_diff_text(underlying, store)


def _shock_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.shock import SHOCK_USAGE, parse_shock_pct, shock_text

    raw = args.get("shock_pct")
    if raw is None:
        raw = args.get("percent")
    shock_pct: float | None = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        shock_pct = float(raw)
    elif isinstance(raw, str):
        shock_pct = parse_shock_pct(raw)
    if shock_pct is None:
        return SHOCK_USAGE
    return shock_text(underlying, store, journal, now, shock_pct)


def _holdings_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    from bot.holdings import save_holdings_text

    return save_holdings_text(args.get("holdings"), store, journal, now)


def _debate_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.debate import card_debate

    raw_index = args.get("index", 0)
    try:
        index = int(raw_index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("index must be an integer") from None
    snapshot = args.get("snapshot")
    return card_debate(
        underlying, store, journal, now,
        snapshot_id=str(snapshot) if snapshot else None,
        index=index,
        live_models=bool(args.get("live_models", False)),
    )


def _profile_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, now, underlying
    from bot.profile import format_profile, suggest_params

    profile = store.read_profile()
    if profile is None:
        return 'no profile is saved; chat "profile" to answer the questionnaire'
    return format_profile(profile, suggest_params(profile))


def _dashboard_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, config, underlying
    from bot.dashboard import dashboard_text

    return dashboard_text(store, journal, now)


def _alerts_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, config, underlying
    from bot.alerts import collect_alerts

    alerts = []
    for symbol in sorted(store.list_underlyings()):
        alerts.extend(collect_alerts(symbol, store, journal, now))
    if not alerts:
        return "alerts: none"
    return "\n".join(alerts)


def _holdings_read_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, now, underlying
    from bot.holdings import holdings_text

    return holdings_text(store)


def _session_run_handler(
    args: dict[str, Any],
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    underlying: str,
    session_controller: Optional[SessionController] = None,
) -> str:
    if session_controller is None:
        raise ValueError("sessions not configured")
    flags = args if isinstance(args, dict) else {}
    text, _ = session_controller.run(
        underlying,
        live_models=bool(flags.get("live_models", False)),
        grounding=bool(flags.get("grounding", False)),
        cards=bool(flags.get("cards", False)),
        now=now,
        paper_trade=bool(flags.get("paper_trade", True)),
    )
    return text


def _coach_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    from bot.coach import coach_review

    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    return coach_review(store, journal, now, live_models=live)


def _portfolio_tool_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.commands import PORTFOLIO_USAGE, portfolio_response

    command = args.get("command", "") if isinstance(args, dict) else ""
    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    if not isinstance(command, str) or not command.strip():
        return PORTFOLIO_USAGE
    return portfolio_response(command, underlying, store, journal, now, live_models=live)


def _shock_news_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.news import shock_news_response
    from bot.shock import SHOCK_USAGE, parse_shock_pct

    raw = args.get("shock_pct")
    if raw is None:
        raw = args.get("percent")
    shock_pct: float | None = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        shock_pct = float(raw)
    elif isinstance(raw, str):
        shock_pct = parse_shock_pct(raw)
    if shock_pct is None:
        return SHOCK_USAGE
    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    return shock_news_response(underlying, store, journal, now, shock_pct, live_models=live)


def _decode_tool_image(args: dict[str, Any]) -> bytes:
    raw = args.get("image_b64", "") if isinstance(args, dict) else ""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("read needs image_b64 with base64 png or jpeg bytes")
    try:
        return base64.b64decode(raw.strip(), validate=True)
    except ValueError:
        raise ValueError("chart image rejected: bad base64") from None


def _chart_image_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.chartread import read_chart_image

    image = _decode_tool_image(args)
    question = args.get("question", "") if isinstance(args, dict) else ""
    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    return read_chart_image(
        image, underlying, store, journal, now, live_models=live,
        question=question if isinstance(question, str) else "",
    )


def _document_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.docread import read_document

    image = _decode_tool_image(args)
    question = args.get("question", "") if isinstance(args, dict) else ""
    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    return read_document(
        image, question if isinstance(question, str) else "", underlying,
        store, journal, now, live_models=live,
    )


def _share_tool_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.cards import share_card

    raw_index = args.get("index", 0)
    try:
        index = int(raw_index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("index must be an integer") from None
    snapshot = args.get("snapshot")
    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    return share_card(
        index, underlying, store, journal, now,
        snapshot_id=str(snapshot) if snapshot else None,
        live_models=live,
    )


def _narrated_alerts_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    from bot.alerts import narrated_alerts

    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    lines = []
    for symbol in sorted(store.list_underlyings()):
        lines.extend(narrated_alerts(symbol, store, journal, now, live_models=live))
    if not lines:
        return "alerts: none"
    return "\n".join(lines)


def _tier_status_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, store, config, underlying
    from bot.tiers import format_tier_status, tier_status

    return format_tier_status(tier_status(journal, now))


TOOL_REGISTRY: dict[str, MuseTool] = {
    "get_gex_brief": MuseTool("get_gex_brief", "Read the computed GEX brief for an underlying", _gex_brief_handler),
    "get_trade_cards": MuseTool(
        "get_trade_cards", "Read per-trade cards with reasons from stored snapshots", _trade_cards_handler
    ),
    "get_trade_ideas": MuseTool(
        "get_trade_ideas", "Rank trade ideas from stored snapshots", _trade_ideas_handler
    ),
    "get_wheel_ideas": MuseTool(
        "get_wheel_ideas", "Screen cash-secured-put and covered-call premium ideas", _wheel_ideas_handler
    ),
    "get_vex_brief": MuseTool("get_vex_brief", "Read the computed VEX brief for an underlying", _vex_brief_handler),
    "get_dex_brief": MuseTool("get_dex_brief", "Read the computed DEX brief for an underlying", _dex_brief_handler),
    "journal_tail": MuseTool("journal_tail", "Read the tail of the journal", _journal_tail_handler),
    "paper_pnl": MuseTool("paper_pnl", "Report paper P&L from journaled fills", _paper_pnl_handler),
    "get_iv_rank": MuseTool("get_iv_rank", "Report IV rank per underlying from stored histories", _iv_rank_handler),
    "backfill_iv": MuseTool("backfill_iv", "Backfill IV history from stored snapshots", _backfill_handler),
    "snapshot_diff": MuseTool("snapshot_diff", "Diff the latest two stored snapshots", _diff_handler),
    "shock_test": MuseTool("shock_test", "Shock spot and reprice walls plus cards", _shock_handler),
    "set_holdings": MuseTool("set_holdings", "Save operator holdings for position-aware risk", _holdings_handler),
    "debate_card": MuseTool("debate_card", "Bull-bear debate for one trade card", _debate_handler),
    "run_replay": MuseTool("run_replay", "Replay stored snapshots into a paper backtest summary", _replay_handler),
    "run_session": MuseTool("run_session", "Run a scheduled session now and return the brief", _session_run_handler),
    "get_profile": MuseTool("get_profile", "Read the saved operator profile", _profile_handler),
    "get_dashboard": MuseTool("get_dashboard", "Read the one-screen digest across stored underlyings", _dashboard_handler),
    "get_alerts": MuseTool("get_alerts", "Read current alerts across stored underlyings", _alerts_handler),
    "get_holdings": MuseTool("get_holdings", "Read the saved operator book against live positioning", _holdings_read_handler),
    "get_video_brief": MuseTool(
        "get_video_brief", "Render-ready HyperFrames video brief project from stored snapshots", _video_brief_handler
    ),
    "get_coach_review": MuseTool(
        "get_coach_review", "Wonder trading-coach review of the journaled record", _coach_handler
    ),
    "portfolio_command": MuseTool(
        "portfolio_command", "Run a natural-language paper portfolio command", _portfolio_tool_handler
    ),
    "shock_news": MuseTool(
        "shock_news", "Shock spot and explain the move with cited news", _shock_news_handler
    ),
    "read_chart_image": MuseTool(
        "read_chart_image", "Read a chart screenshot against engine positioning", _chart_image_handler
    ),
    "read_document": MuseTool(
        "read_document", "Segment and read a document screenshot with SAM plus Spark", _document_handler
    ),
    "share_card": MuseTool(
        "share_card", "Shareable trade card caption with art", _share_tool_handler
    ),
    "get_narrated_alerts": MuseTool(
        "get_narrated_alerts", "Current alerts with one-line reasons", _narrated_alerts_handler
    ),
    "get_tier_status": MuseTool(
        "get_tier_status", "Model tier spend, budget, and local backend status", _tier_status_handler
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
    session_controller: Optional[SessionController] = None,
) -> MuseChatReply:
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return MuseChatReply(ok=False, reply="", error=f"unknown tool {name}")
    try:
        handler_args = args if isinstance(args, dict) else {}
        if tool.handler is _session_run_handler:
            reply = tool.handler(
                handler_args, store, journal, config, now, underlying, session_controller
            )
        else:
            reply = tool.handler(handler_args, store, journal, config, now, underlying)
    except ValueError as exc:
        return MuseChatReply(ok=False, reply="", error=str(exc))
    return MuseChatReply(ok=True, reply=reply, error="")


SESSION_PATH = "/session"
SESSION_TICK_SECONDS = 30.0


@dataclass(frozen=True)
class SessionSchedule:
    """Typed parse of cron subset "M H * * DOW" in machine-local time.

    Weekdays are Python weekdays (Monday 0 through Sunday 6).
    """

    minute: int
    hour: int
    weekdays: frozenset[int]

    def matches(self, now: datetime) -> bool:
        return now.minute == self.minute and now.hour == self.hour and now.weekday() in self.weekdays


def _parse_dow_value(raw: str, text: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"invalid schedule {text!r}: bad weekday {raw!r}") from None
    if value == 7:
        value = 0
    if not 0 <= value <= 6:
        raise ValueError(f"invalid schedule {text!r}: weekday {raw!r} out of range 0-7")
    return value


def _parse_dow_field(field: str, text: str) -> frozenset[int]:
    if field == "*":
        return frozenset(range(7))
    days: set[int] = set()
    for part in field.split(","):
        if not part.strip():
            raise ValueError(f"invalid schedule {text!r}: empty weekday entry")
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise ValueError(f"invalid schedule {text!r}: bad weekday range {part!r}")
            start = _parse_dow_value(bounds[0], text)
            end = _parse_dow_value(bounds[1], text)
            if end < start:
                raise ValueError(f"invalid schedule {text!r}: reversed weekday range {part!r}")
            days.update(range(start, end + 1))
        else:
            days.add(_parse_dow_value(part, text))
    return frozenset((day + 6) % 7 for day in days)


def parse_schedule(text: str) -> SessionSchedule:
    """Parse cron subset "M H * * DOW". Raises ValueError when malformed."""
    fields = (text or "").split()
    if len(fields) != 5:
        raise ValueError(f"invalid schedule {text!r}: want 'M H * * DOW'")
    minute_text, hour_text, day_of_month, month, dow = fields
    try:
        minute = int(minute_text)
        hour = int(hour_text)
    except ValueError:
        raise ValueError(f"invalid schedule {text!r}: minute and hour must be numbers") from None
    if not 0 <= minute <= 59:
        raise ValueError(f"invalid schedule {text!r}: minute out of range 0-59")
    if not 0 <= hour <= 23:
        raise ValueError(f"invalid schedule {text!r}: hour out of range 0-23")
    if day_of_month != "*" or month != "*":
        raise ValueError(f"invalid schedule {text!r}: day-of-month and month must be '*'")
    return SessionSchedule(minute, hour, _parse_dow_field(dow, text))


MinuteKey = tuple[int, int, int, int, int]


def session_due(schedule: SessionSchedule, now: datetime, last_fired: MinuteKey | None) -> bool:
    key = (now.year, now.month, now.day, now.hour, now.minute)
    return schedule.matches(now) and key != last_fired


@contextmanager
def _scratch_run(
    lock: threading.Lock, store: SnapshotStore, journal: Journal
) -> Iterator[tuple[SnapshotStore, Journal]]:
    """Yield a scratch copy of live state, merging it back on clean exit.

    The core runs with no lock held. A raised core skips the commit.
    Pruning and saving stay with the caller.
    """
    scratch_store = SnapshotStore()
    scratch_journal = Journal()
    with lock:
        scratch_store.restore(store)
        scratch_journal.restore(journal)
    yield scratch_store, scratch_journal
    with lock:
        store.merge_from(scratch_store)
        journal.merge(scratch_journal)


@dataclass
class SessionController:
    """Shared session primitive for the HTTP trigger and the timer.

    run is the only entry; both drivers call it. lock is the server
    lock, reentrant because the HTTP path already holds it.
    """

    store: SnapshotStore
    journal: Journal
    store_dir: str
    lock: threading.RLock
    context_factory: Callable[[str], FeedContext] = live_context

    def run(
        self,
        underlying: str | Sequence[str] = "SPY",
        live_models: bool = False,
        grounding: bool = False,
        cards: bool = False,
        now: datetime | None = None,
        paper_trade: bool = True,
    ) -> tuple[str, list[str]]:
        with self.lock:
            symbols = tick_symbols(normalize_underlyings(underlying), self.store)
        with _scratch_run(self.lock, self.store, self.journal) as (scratch_store, scratch_journal):
            text = run_session_core(
                symbols,
                scratch_store,
                scratch_journal,
                self.store_dir,
                now,
                None,
                None,
                self.context_factory,
                live_models,
                grounding,
                cards,
                paper_trade,
            )
        with self.lock:
            moment = now or datetime.now(timezone.utc)
            try:
                requested = underlying if isinstance(underlying, str) else ",".join(underlying)
                video_response(
                    "", requested, self.store, self.journal, moment,
                    project_dir=os.path.join(self.store_dir, "video-brief"),
                )
            except Exception as exc:
                self.journal.append(moment, "video_brief_failed", {"error": type(exc).__name__})
            save_bundle(self.store_dir, self.store, self.journal)
        return text, symbols


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
    if method == "POST" and clean == "/voice":
        from bot.voice import handle_voice_note

        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": "voice needs raw audio bytes"})
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"

        def _flag(name: str, default: bool) -> bool:
            raw_flag = (params.get(name, [""])[0] or "").strip().lower()
            if not raw_flag:
                return default
            return raw_flag not in ("0", "false", "no", "off")

        reply = handle_voice_note(
            bytes(payload), underlying, store, journal, now,
            live_models=_flag("live_models", True),
            grounding=_flag("grounding", False),
            cards=_flag("cards", False),
        )
        return HttpResponse.json(200, {"ok": True, "reply": reply})
    if method == "POST" and clean in ("/chartread", "/docread"):
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": "image endpoint needs raw png or jpeg bytes"})
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
        raw_flag = (params.get("live_models", [""])[0] or "").strip().lower()
        live = raw_flag not in ("0", "false", "no", "off") if raw_flag else True
        if clean == "/chartread":
            from bot.chartread import read_chart_image

            question = (params.get("question", [""])[0] or "").strip()
            reply = read_chart_image(
                bytes(payload), underlying, store, journal, now,
                live_models=live, question=question,
            )
        else:
            from bot.docread import read_document

            question = (params.get("question", [""])[0] or "").strip()
            reply = read_document(
                bytes(payload), question, underlying, store, journal, now, live_models=live
            )
        return HttpResponse.json(200, {"ok": True, "reply": reply})
    if method == "POST" and clean == SESSION_PATH:
        if not isinstance(payload, dict):
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": "invalid session body"})
        underlying = payload.get("underlying", "SPY")
        if underlying is None:
            underlying = "SPY"
        if not isinstance(underlying, str):
            return HttpResponse.json(400, {"ok": False, "reply": "", "error": "invalid session body"})
        if not underlying.strip():
            underlying = "SPY"
        if session_controller is None:
            return HttpResponse.json(
                503, {"ok": False, "reply": "", "error": "sessions not configured"}
            )
        text, symbols = session_controller.run(
            underlying,
            live_models=bool(payload.get("live_models", False)),
            grounding=bool(payload.get("grounding", False)),
            cards=bool(payload.get("cards", False)),
            now=now,
            paper_trade=bool(payload.get("paper_trade", True)),
        )
        return HttpResponse.json(200, {"ok": True, "reply": text, "underlyings": symbols})
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
                str(name), arguments, store, journal, config, now, underlying=str(underlying),
                session_controller=session_controller,
            )
            return HttpResponse.json(200, {"ok": reply.ok, "reply": reply.reply, "error": reply.error})
        return HttpResponse.json(400, {"ok": False, "reply": "", "error": f"unknown MCP method {mcp_method}"})
    if method == "GET" and clean in ("/cards", "/cards.json"):
        want_json = clean == "/cards.json"
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
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
        return HttpResponse.html(200, trade_cards_html(cards, underlying))
    if method == "GET" and clean == "/chart":
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
        found = store.read_latest(underlying)
        if found is None:
            return HttpResponse.html(400, _cards_error_html(f"no snapshot stored for {underlying}"))
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(underlying)
        profile = compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None)
        return HttpResponse.html(200, gamma_chart_html(underlying, snapshot, profile, bar, now))
    if method == "GET" and clean == "/board":
        return HttpResponse.html(200, regime_board_html(store, now))
    if method == "GET" and clean == "/video":
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
        if store.read_latest(underlying) is None:
            return HttpResponse.html(400, _cards_error_html(f"no snapshot stored for {underlying}"))
        brief = build_video_brief([underlying], store, journal, now)
        storyboard = params.get("storyboard", [""])[0] == "1"
        return HttpResponse.html(200, composition_html(brief, storyboard=storyboard))
    if method == "GET" and clean == "/dashboard":
        from bot.dashboard import dashboard_html

        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
        if not store.list_underlyings():
            return HttpResponse.html(400, _cards_error_html("dashboard: no snapshots stored"))
        return HttpResponse.html(200, dashboard_html(store, journal, now, underlying))
    if method == "GET" and clean in ("/demo", "/"):
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
        if store.read_latest(underlying) is None:
            return HttpResponse.html(400, _cards_error_html(f"no snapshot stored for {underlying}"))
        return HttpResponse.html(200, demo_html(underlying, store, journal, now))
    if method == "GET" and clean == "/card":
        from bot.demo import demo_cards
        query_string = path.split("?", 1)[1] if "?" in path else ""
        params = parse_qs(query_string)
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
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
                return HttpResponse.html(400, _cards_error_html(f"no snapshot stored for {underlying}"))
            snapshot_id = latest[0]
        _, cards = demo_cards(underlying, store, journal, now, snapshot_id)
        if not cards:
            return HttpResponse.html(400, _cards_error_html(f"no cards for {underlying} snapshot {snapshot_id}"))
        if index < 0 or index >= len(cards):
            return HttpResponse.html(400, _cards_error_html(f"index {index} out of range (0-{len(cards) - 1})"))
        page = trade_cards_html(cards[index : index + 1], underlying)
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
        underlying = (params.get("underlying", ["SPY"])[0] or "SPY").strip() or "SPY"
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
                return HttpResponse.html(400, _cards_error_html(f"no snapshot stored for {underlying}"))
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
        return HttpResponse.html(200, share_html(cards[index], art=art, url=url, art_hash=art_hash))
    return HttpResponse.json(404, {"error": "not found"})


def _cards_error_html(message: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>trade cards</title></head>"
        f"<body><h1>trade cards</h1><p>{html.escape(message, quote=True)}</p></body></html>"
    )


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


def _body_payload(clean: str, raw: bytes) -> tuple[Any, Optional[HttpResponse]]:
    if clean == "/voice":
        if len(raw) > MAX_VOICE_BYTES:
            return None, HttpResponse.json(413, {"ok": False, "reply": "", "error": "voice audio too large"})
        return raw, None
    if clean in ("/chartread", "/docread"):
        if len(raw) > MAX_IMAGE_BYTES:
            label = "chart image too large" if clean == "/chartread" else "document image too large"
            return None, HttpResponse.json(413, {"ok": False, "reply": "", "error": label})
        return raw, None
    try:
        return (json.loads(raw.decode("utf-8")) if raw else {}), None
    except ValueError:
        return None, HttpResponse.json(400, {"ok": False, "reply": "", "error": "invalid JSON"})


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
    from bot.schedule import normalize_underlyings, refresh_core

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
    controller = SessionController(store, journal, store_dir, lock) if store_dir else None
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
