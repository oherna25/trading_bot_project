from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import quote as _urlencode

from bot.exposure import compute_gex
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.replay import replay_report
from bot.research import handle_chat_query
from bot.store import SnapshotStore
from bot.tradecards import PROFILE_REQUIRED, bar_at_or_before, build_trade_cards, format_trade_cards
from bot.wheel import wheel_response


def demo_cards(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime, snapshot_id: str | None = None
):
    if snapshot_id is None:
        found = store.read_latest(underlying)
        if found is None:
            return None, []
        snapshot_id, snapshot = found
    else:
        match = [
            s for sid, s in store.snapshots_in_order()
            if sid == snapshot_id and s.underlying == underlying
        ]
        if not match:
            return None, []
        snapshot = match[0]
    bar = bar_at_or_before(store, underlying, snapshot.timestamp_utc)
    entries = [
        (
            snapshot_id,
            snapshot,
            compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None),
            bar,
        )
    ]
    cards = build_trade_cards(
        entries, None, now, journal, {}, None,
        iv_history=histories_for(store, [underlying]), earnings={},
    )
    return snapshot_id, cards


def demo_html(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(f"no snapshot stored for {underlying}")
    snapshot_id, _ = found
    brief, _ = handle_chat_query("judge demo brief", underlying, store, journal, now)
    _, cards = demo_cards(underlying, store, journal, now)
    if store.read_profile() is None:
        top = PROFILE_REQUIRED
    else:
        top = format_trade_cards(cards[:1]) if cards else "trade cards: none found"
    wheel = wheel_response("wheel ideas", underlying, store, journal, now)
    replay_text, _, _ = replay_report(underlying, store)
    link = f"/card?underlying={_urlencode(underlying)}&snapshot={_urlencode(snapshot_id)}&index=0"
    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:2rem;line-height:1.5;color:#24292f;background:#fff;max-width:56rem}"
        "h1{font-size:1.5rem}h2{font-size:1.15rem;margin:2rem 0 .5rem;border-bottom:1px solid #d0d7de}"
        "pre{background:#f6f8fa;padding:1rem;overflow-x:auto;white-space:pre-wrap}"
        "nav a{margin-right:1rem}"
    )
    section = lambda title, body: f"<h2>{title}</h2><pre>{html.escape(body)}</pre>"
    nav = (
        f"<nav><a href=\"{html.escape(link, quote=True)}\">top card permalink</a>"
        f"<a href=\"/chart?underlying={html.escape(underlying, quote=True)}\">chart</a>"
        f"<a href=\"/board\">board</a></nav>"
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(underlying)} demo</title><style>{style}</style></head><body>"
        f"<h1>{html.escape(underlying)} demo, from stored snapshots</h1>"
        f"{nav}"
        f"{section('1. Regime read', brief)}"
        f"{section('2. Top card', top)}"
        f"{section('3. Wheel screen', wheel)}"
        f"{section('4. Replay', replay_text)}"
        "</body></html>"
    )
