"""MCP tool handlers and TOOL_REGISTRY for the Muse transport."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from bot.ivrank import histories_for, iv_rank
from bot.journal import Journal
from bot.research import (
    dex_brief_response,
    handle_chat_query,
    ideas_response,
    journal_tail_text,
    paper_pnl_text,
    vex_brief_response,
)
from bot.shapes import DashboardSettings, OperatorConfig, dashboard_settings_from
from bot.store import SnapshotStore
from bot.tradecards import cards_response
from bot.video import video_response
from bot.wheel import wheel_response

from bot.muse_session import SessionController

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
    del config
    from bot.replay import (
        REPLAY_COMMISSION_DEFAULT,
        REPLAY_SLIPPAGE_BPS_DEFAULT,
        replay_response,
    )

    raw_commission = None
    raw_slippage = None
    if isinstance(args, dict):
        raw_commission = args.get("commission", args.get("commission_per_fill"))
        raw_slippage = args.get("slippage_bps", args.get("slippage"))
    try:
        commission = float(raw_commission) if raw_commission is not None else REPLAY_COMMISSION_DEFAULT
    except (TypeError, ValueError):
        raise ValueError("commission must be a number -- next: call run_replay with commission 0") from None
    try:
        slippage = float(raw_slippage) if raw_slippage is not None else REPLAY_SLIPPAGE_BPS_DEFAULT
    except (TypeError, ValueError):
        raise ValueError("slippage_bps must be a number -- next: call run_replay with slippage_bps 0") from None
    if commission < 0 or slippage < 0:
        raise ValueError("commission and slippage_bps must be >= 0 -- next: call run_replay with commission 0")
    return replay_response(underlying, store, journal, now,
                           commission_per_fill=commission, slippage_bps=slippage)


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
    from bot.holdings import save_holdings_shapes

    if isinstance(args, dict) and "holdings" in args:
        raw = args.get("holdings")
    else:
        raw = args
    return save_holdings_shapes(raw, store, journal, now)


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
    from bot.alerts import alerts_header, collect_alerts

    alerts = []
    for symbol in sorted(store.list_underlyings()):
        alerts.extend(collect_alerts(symbol, store, journal, now))
    head = alerts_header(len(alerts), now, "the dashboard digest")
    if not alerts:
        return f"{head}\nnone"
    return f"{head}\n" + "\n".join(alerts)


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
    from bot.alerts import alerts_header, narrated_alerts

    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    lines = []
    for symbol in sorted(store.list_underlyings()):
        lines.extend(narrated_alerts(symbol, store, journal, now, live_models=live))
    head = alerts_header(len(lines), now, "the dashboard digest (narrated here)")
    if not lines:
        return f"{head}\nnone"
    return f"{head}\n" + "\n".join(lines)


def _tier_status_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, store, config, underlying
    from bot.tiers import format_tier_status, tier_status

    return format_tier_status(tier_status(journal, now))


def _earnings_radar_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, underlying
    from bot.earnings import earnings_text

    return earnings_text(store, now)


def _take_paper_trade_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.research import take_card_trade

    raw_index = args.get("index", 0)
    try:
        index = int(raw_index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("index must be an integer") from None
    snapshot = args.get("snapshot")
    return take_card_trade(
        index, underlying, store, journal, now,
        snapshot_id=str(snapshot) if snapshot else None,
    )


def _trackrecord_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, config, now, underlying
    from bot.trackrecord import trackrecord_text

    return trackrecord_text(journal, store)


def _explain_position_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.explain import explain_position

    raw = ""
    if isinstance(args, dict):
        raw = str(args.get("symbol", args.get("query", "")) or "").strip()
    symbol = raw or underlying
    return explain_position(symbol, store, journal, now)


def _onboarding_step_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.onboarding import onboarding_tool

    answer = ""
    if isinstance(args, dict):
        answer = str(args.get("answer", args.get("text", "")) or "")
    return onboarding_tool(answer, underlying, store, journal, now)


def _set_alert_rule_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    from bot.customalerts import set_rule_response

    text = str(args.get("text", args.get("rule", "")) or "") if isinstance(args, dict) else ""
    return set_rule_response(text, store, journal, now)


def _list_alert_rules_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, now, underlying
    from bot.customalerts import list_rules_text

    return list_rules_text(store)


def _cancel_alert_rule_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    from bot.customalerts import cancel_rule_response

    rule_id = ""
    if isinstance(args, dict):
        rule_id = str(args.get("rule_id", args.get("id", "")) or "")
    return cancel_rule_response(rule_id, store, journal, now)


def _open_brief_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.morning import generate_open_brief

    live = bool(args.get("live_models", False)) if isinstance(args, dict) else False
    return generate_open_brief(store, journal, now, underlying, live_models=live)


def _live_board_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, underlying
    from bot.charts import live_board_text

    return live_board_text(store, now)


def settings_summary(settings: DashboardSettings) -> str:
    return (
        f"settings: theme={settings.theme} "
        f"default_underlying={settings.default_underlying} "
        f"refresh_seconds={settings.refresh_seconds}"
    )


def save_settings_merge(
    raw: Mapping[str, Any], store: SnapshotStore, journal: Journal, now: datetime
) -> DashboardSettings:
    current = store.read_settings()
    merged = {
        "theme": current.theme,
        "default_underlying": current.default_underlying,
        "refresh_seconds": current.refresh_seconds,
    }
    for key in merged:
        if key in raw:
            merged[key] = raw[key]
    settings = dashboard_settings_from(merged)
    store.save_settings(settings)
    journal.append(
        now, "settings_saved",
        {
            "theme": settings.theme,
            "default_underlying": settings.default_underlying,
            "refresh_seconds": settings.refresh_seconds,
        },
    )
    return settings


def save_profile_answers(
    answers: Mapping[str, Any], store: SnapshotStore, journal: Journal, now: datetime
) -> str:
    from bot.profile import build_profile, format_summary, suggest_params

    profile = build_profile(answers)
    store.save_profile(profile)
    params = suggest_params(profile)
    journal.append(
        now,
        "profile_accepted",
        {
            "risk_tolerance": profile.risk_tolerance,
            "portfolio_size": profile.portfolio_size,
            "knowledge": profile.knowledge,
            "options_ok": profile.options_ok,
            **params,
        },
    )
    return format_summary(profile, params)


def _settings_write_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    if not isinstance(args, dict):
        raise ValueError("settings must be an object with theme, default_underlying, refresh_seconds")
    return settings_summary(save_settings_merge(args, store, journal, now))


def _settings_read_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del args, journal, config, now, underlying
    return settings_summary(store.read_settings())


def _load_snapshot_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config
    from bot.research import load_response

    raw = args.get("symbol", args.get("symbols", args.get("text", underlying))) if isinstance(args, dict) else underlying
    if isinstance(raw, (list, tuple)):
        symbols = [str(item).strip().upper() for item in raw if str(item).strip()]
    else:
        symbols = [str(raw).strip().upper()] if str(raw).strip() else [underlying]
    if not symbols:
        symbols = [underlying]
    return load_response(f"load {' '.join(symbols)}", underlying, store, journal, now)


def _profile_write_handler(
    args: dict[str, Any], store: SnapshotStore, journal: Journal, config: OperatorConfig, now: datetime, underlying: str
) -> str:
    del config, underlying
    from bot.profile import parse_answers

    if not isinstance(args, dict):
        raise ValueError("profile needs risk_tolerance, portfolio_size, knowledge, options_ok")
    if "text" in args:
        return save_profile_answers(parse_answers(str(args.get("text", ""))), store, journal, now)
    return save_profile_answers(args, store, journal, now)


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
    "set_settings": MuseTool(
        "set_settings", "Save global dashboard settings shared by all pages", _settings_write_handler
    ),
    "get_settings": MuseTool(
        "get_settings", "Read global dashboard settings shared by all pages", _settings_read_handler
    ),
    "load_snapshot": MuseTool(
        "load_snapshot", "Fetch a fresh snapshot for one or more symbols", _load_snapshot_handler
    ),
    "set_profile": MuseTool(
        "set_profile", "Save the operator questionnaire profile", _profile_write_handler
    ),
    "get_earnings_radar": MuseTool(
        "get_earnings_radar", "Prints in the next 14 days with regime and wall positioning",
        _earnings_radar_handler,
    ),
    "take_paper_trade": MuseTool(
        "take_paper_trade", "Take a trade card as a paper trade", _take_paper_trade_handler
    ),
    "get_trackrecord": MuseTool(
        "get_trackrecord", "Closed paper trades with win rate and equity",
        _trackrecord_handler,
    ),
    "explain_position": MuseTool(
        "explain_position", "Explain your shares in one paragraph from stored state",
        _explain_position_handler,
    ),
    "onboarding_step": MuseTool(
        "onboarding_step", "Answer the 4-question profile flow step by step",
        _onboarding_step_handler,
    ),
    "set_alert_rule": MuseTool(
        "set_alert_rule", "Save a custom break or flip-cross alert rule",
        _set_alert_rule_handler,
    ),
    "list_alert_rules": MuseTool(
        "list_alert_rules", "List custom alert rules with ids",
        _list_alert_rules_handler,
    ),
    "cancel_alert_rule": MuseTool(
        "cancel_alert_rule", "Cancel one custom alert rule by id",
        _cancel_alert_rule_handler,
    ),
    "get_open_brief": MuseTool(
        "get_open_brief", "Morning brief with regime, book risk, earnings, narrated alerts",
        _open_brief_handler,
    ),
    "get_live_board": MuseTool(
        "get_live_board", "Tickers with feed source and quote age badges",
        _live_board_handler,
    ),
}
