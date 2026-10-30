from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from bot.journal import Journal
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore

ArgsBuilder = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class SlashCommand:
    name: str
    tool: str
    usage: str
    args: ArgsBuilder


def _no_args(rest: str) -> dict[str, Any]:
    del rest
    return {}


def _cards_args(rest: str) -> dict[str, Any]:
    return {"query": rest or "trade cards"}


def _wheel_args(rest: str) -> dict[str, Any]:
    return {"query": rest or "wheel ideas"}


def _shock_args(rest: str) -> dict[str, Any]:
    from bot.shock import parse_shock_pct

    shock_pct = parse_shock_pct(rest)
    if shock_pct is None:
        raise ValueError("usage: /shock <percent>, e.g. /shock -2%")
    return {"shock_pct": shock_pct}


SLASH_COMMANDS: dict[str, SlashCommand] = {
    "profile": SlashCommand("profile", "get_profile", "/profile", _no_args),
    "dashboard": SlashCommand("dashboard", "get_dashboard", "/dashboard", _no_args),
    "alerts": SlashCommand("alerts", "get_alerts", "/alerts", _no_args),
    "holdings": SlashCommand("holdings", "get_holdings", "/holdings", _no_args),
    "brief": SlashCommand("brief", "get_gex_brief", "/brief", _no_args),
    "cards": SlashCommand("cards", "get_trade_cards", "/cards [query]", _cards_args),
    "wheel": SlashCommand("wheel", "get_wheel_ideas", "/wheel [query]", _wheel_args),
    "diff": SlashCommand("diff", "snapshot_diff", "/diff", _no_args),
    "shock": SlashCommand("shock", "shock_test", "/shock <percent>", _shock_args),
    "replay": SlashCommand("replay", "run_replay", "/replay", _no_args),
    "pnl": SlashCommand("pnl", "paper_pnl", "/pnl", _no_args),
    "help": SlashCommand("help", "help", "/help", _no_args),
    "commands": SlashCommand("commands", "commands", "/commands", _no_args),
    "info": SlashCommand("info", "info", "/info", _no_args),
}


COMMAND_DESCRIPTIONS = {
    "profile": "show your operator profile",
    "dashboard": "show the account dashboard",
    "alerts": "list active alerts",
    "holdings": "show saved holdings",
    "brief": "show the GEX positioning brief",
    "cards": "show trade cards with reasons",
    "wheel": "show wheel premium ideas",
    "diff": "diff the last two snapshots",
    "shock": "shock spot and reprice walls and cards",
    "replay": "replay snapshots as a backtest",
    "pnl": "show paper P&L",
    "help": "list command usages",
    "commands": "list every command with descriptions",
    "info": "explain app features",
}


def help_text() -> str:
    lines = ["slash commands:"]
    for command in SLASH_COMMANDS.values():
        lines.append(command.usage)
    return "\n".join(lines)


def commands_text() -> str:
    lines = ["slash commands:"]
    for command in SLASH_COMMANDS.values():
        lines.append(f"{command.usage} - {COMMAND_DESCRIPTIONS[command.name]}")
    return "\n".join(lines)


def info_text() -> str:
    return "\n".join(
        [
            "trading bot features:",
            "briefs: GEX/VEX/DEX positioning reads via /brief and chat",
            "cards: per-trade cards with spread stats and a one-line reason",
            "ideas: ranked defined-risk trade ideas (chat: trade ideas)",
            "wheel: cash-secured-put and covered-call premium screen",
            "diff: what changed between the last two snapshots",
            "shock: reprice walls and card P&L at a shocked spot",
            "replay: replay snapshots as a signal-to-fill backtest",
            "paper: paper trading plus marked P&L (/pnl)",
            "alerts: flip, regime, and wall alerts with Discord/Telegram push",
            "holdings: saved book with risk vs flip, spot, and walls",
            "debate: bull vs bear debate on any card (chat: debate)",
            "profile: risk questionnaire with suggested risk params (/profile)",
            "pages: /chart, /board, /cards, /card, /demo, /dashboard over HTTP",
            "voice: voice notes transcribed into chat commands",
            "sessions: scheduled live briefs with alerts and paper trades",
        ]
    )


def parse_slash(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        raise ValueError("slash commands start with /")
    parts = stripped[1:].split(None, 1)
    name = parts[0].lower() if parts and parts[0] else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    return name, rest


def slash_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
) -> str:
    from bot.muse import dispatch_tool

    try:
        name, rest = parse_slash(text)
    except ValueError:
        return help_text()
    command = SLASH_COMMANDS.get(name)
    if command is None or name == "help":
        return help_text()
    if name == "commands":
        return commands_text()
    if name == "info":
        return info_text()
    try:
        args = command.args(rest)
    except ValueError as exc:
        return str(exc)
    reply = dispatch_tool(command.tool, args, store, journal, config, now, underlying=underlying)
    if reply.ok:
        return reply.reply
    return f"/{name} failed: {reply.error}"
