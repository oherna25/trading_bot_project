from __future__ import annotations

import html
from datetime import datetime
from typing import Mapping
from urllib.parse import quote as _urlencode

from bot.exposure import compute_gex
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.models import HttpPost
from bot.replay import replay_report
from bot.research import handle_chat_query
from bot.shock import shock_text
from bot.store import SnapshotStore, need_snapshot_text

SHOCK_BUTTON_PCTS = (-2.0, -1.0, 1.0, 2.0)
from bot.tradecards import PROFILE_REQUIRED, bar_at_or_before, build_trade_cards, format_sized_cards
from bot.wheel import wheel_response


def demo_cards(
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
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
        entries, None, now, journal, env, http_post,
        iv_history=histories_for(store, [underlying]), earnings=store.all_earnings(),
    )
    return snapshot_id, cards


def shock_buttons(underlying: str) -> str:
    links = []
    for pct in SHOCK_BUTTON_PCTS:
        label = f"{pct:+.0f}%"
        href = f"/shock?underlying={_urlencode(underlying)}&pct={pct:g}"
        links.append(f"<a class=\"shock-btn\" href=\"{html.escape(href, quote=True)}\">{label}</a>")
    return f"<p>{''.join(links)}</p>"


def shock_html(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime, pct: float
) -> str:
    from bot.theme import page

    body = shock_text(underlying, store, journal, now, pct)
    demo_href = f"/demo?underlying={_urlencode(underlying)}"
    title = f"{underlying} shock {pct:+g}%"
    content = (
        f"<p><a href=\"{html.escape(demo_href, quote=True)}\">back to demo</a></p>"
        f"{shock_buttons(underlying)}"
        f"<pre>{html.escape(body)}</pre>"
    )
    return page(
        title, f"{title}, from stored snapshots", body.splitlines()[0] if body else "",
        content, underlying, "demo", store.read_settings(),
        f"/shock?underlying={_urlencode(underlying)}&pct={pct:g}",
    )


def demo_html(underlying: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.theme import BRAND, page

    found = store.read_latest(underlying)
    if found is None:
        raise ValueError(need_snapshot_text(underlying, store))
    snapshot_id, _ = found
    brief, _ = handle_chat_query("judge demo brief", underlying, store, journal, now)
    _, cards = demo_cards(underlying, store, journal, now)
    if store.read_profile() is None:
        top = PROFILE_REQUIRED
    else:
        top = format_sized_cards(cards[:1], store.read_profile()) if cards else "trade cards: none found"
    wheel = wheel_response("wheel ideas", underlying, store, journal, now)
    replay_text, _, _ = replay_report(underlying, store)
    link = f"/card?underlying={_urlencode(underlying)}&snapshot={_urlencode(snapshot_id)}&index=0"
    section = lambda title, body: f"<h2>{title}</h2><pre>{html.escape(body)}</pre>"
    if store.read_profile() is not None and cards:
        take = (
            "<form class=\"block\" method=\"post\" action=\"/action\">"
            "<input type=\"hidden\" name=\"tool\" value=\"take_paper_trade\">"
            f"<input type=\"hidden\" name=\"underlying\" value=\"{html.escape(underlying, quote=True)}\">"
            f"<input type=\"hidden\" name=\"snapshot\" value=\"{html.escape(snapshot_id, quote=True)}\">"
            "<input type=\"hidden\" name=\"index\" value=\"0\">"
            "<button class=\"solid\" type=\"submit\">take this trade (paper)</button></form>"
        )
    else:
        take = ""
    content = (
        f"<p><a href=\"{html.escape(link, quote=True)}\">top card permalink</a></p>"
        f"{section('1. Regime read', brief)}"
        f"{section('2. Top card', top)}{take}"
        f"{section('3. Wheel screen', wheel)}"
        f"{section('4. Replay', replay_text)}"
        f"<h2>5. Shock</h2>{shock_buttons(underlying)}"
        "<p>Pick a shock to reprice walls against a moved spot.</p>"
    )
    title = f"{underlying} demo"
    return page(
        title, f"{title}, from stored snapshots, on {BRAND}",
        f"snapshot {snapshot_id}",
        content, underlying, "demo", store.read_settings(),
        f"/demo?underlying={_urlencode(underlying)}",
    )
