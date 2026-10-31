from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from bot.alerts import collect_alerts, narrate_alert, push_text
from bot.charts import board_rows
from bot.diff import compact_dollars
from bot.earnings import earnings_rows
from bot.holdings import holdings_text
from bot.journal import Journal
from bot.localmodels import LocalRunner
from bot.models import HttpPost
from bot.store import SnapshotStore, empty_store_text
from bot.video import build_video_brief

OPEN_BRIEF_HOUR_ET = 9
OPEN_BRIEF_MINUTE_ET = 25
OPEN_BRIEF_KIND = "open_brief"
ET_ZONE = ZoneInfo("America/New_York")


def _et_now(now: datetime) -> datetime:
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return moment.astimezone(ET_ZONE)


def et_date_iso(now: datetime) -> str:
    return _et_now(now).date().isoformat()


def brief_due(now: datetime, journal: Journal) -> bool:
    et = _et_now(now)
    if et.weekday() >= 5:
        return False
    if (et.hour, et.minute) < (OPEN_BRIEF_HOUR_ET, OPEN_BRIEF_MINUTE_ET):
        return False
    today = et.date().isoformat()
    return not any(
        entry.kind == OPEN_BRIEF_KIND and entry.payload.get("et_date") == today
        for entry in journal.entries()
    )


def _regime_lines(store: SnapshotStore) -> list[str]:
    lines = []
    for row in board_rows(store):
        spot = row["spot"]
        spot_text = f"{spot:.2f}" if spot is not None else "n/a"
        flip = row["flip"]
        flip_text = f"{flip:.1f}" if flip is not None else "none"
        strike, wall = row["wall_strike"], row["wall_value"]
        wall_text = f"{strike:g} {compact_dollars(float(wall))}" if strike is not None else "none"
        lines.append(
            f"{row['symbol']}: {row['regime']} spot {spot_text} flip {flip_text} wall {wall_text}"
        )
    return lines


def _today_earnings_lines(store: SnapshotStore, now: datetime) -> list[str]:
    rows = [row for row in earnings_rows(store, now) if row["days_out"] == 0]
    if not rows:
        return ["earnings today: no prints dated today in stored print dates"]
    lines = [f"earnings today ({len(rows)} print today):"]
    for row in rows:
        held = "held" if row["held"] else "not held"
        lines.append(
            f"{row['symbol']} prints {row['earnings_date']} | {row['regime']} | {held}"
        )
    return lines


def _narrated_lines(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool,
    env: Mapping[str, str] | None,
    http_post: HttpPost | None,
    local_runner: LocalRunner | None,
) -> list[str]:
    lines = []
    for symbol in sorted(store.list_underlyings()):
        for text in collect_alerts(symbol, store, journal, now):
            narration, source = narrate_alert(
                text, symbol, journal, now, live_models, env, http_post, local_runner
            )
            lines.append(f"{text}\nwhy: {narration} [{source}]")
    if not lines:
        return ["alerts: none"]
    return [f"alerts ({len(lines)}):"] + lines


def generate_open_brief(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    underlying: str = "SPY",
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    local_runner: LocalRunner | None = None,
) -> str:
    symbols = sorted(store.list_underlyings())
    if not symbols:
        return f"open brief: {empty_store_text()} -- next: say load SPY"
    if underlying in symbols:
        symbols.remove(underlying)
        symbols.insert(0, underlying)
    sections = [
        f"open brief {et_date_iso(now)} -- {len(symbols)} underlyings",
        "regime:",
        *_regime_lines(store),
        holdings_text(store),
        *_today_earnings_lines(store, now),
        *_narrated_lines(store, journal, now, live_models, env, http_post, local_runner),
    ]
    brief = build_video_brief(symbols, store, journal, now)
    sections.append(
        f"video: {len(brief.scenes)} scenes, {brief.total_seconds}s total ({brief.composition_id})"
    )
    text = "\n".join(sections)
    journal.append(
        now, OPEN_BRIEF_KIND,
        {"et_date": et_date_iso(now), "underlyings": symbols, "chars": len(text)},
    )
    return text


def maybe_send_open_brief(
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    live_models: bool = False,
    underlying: str = "SPY",
    local_runner: LocalRunner | None = None,
) -> str | None:
    if not store.list_underlyings():
        return None
    if not brief_due(now, journal):
        return None
    text = generate_open_brief(
        store, journal, now, underlying, live_models, env, http_post, local_runner
    )
    push_text(text, journal, now, env, http_post, kind="open_brief_sent",
              extra={"et_date": et_date_iso(now)})
    return text
