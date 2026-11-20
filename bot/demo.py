from __future__ import annotations

import html
from datetime import datetime
from typing import Mapping
from urllib.parse import quote as _urlencode

from bot.exposure import compute_gex
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.models import HttpPost
from bot.shock import shock_text
from bot.store import SnapshotStore
from bot.tradecards import bar_at_or_before, build_trade_cards

SHOCK_BUTTON_PCTS = (-2.0, -1.0, 1.0, 2.0)


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
        links.append(f"<a class=\"shock-btn\" href=\"{html.escape(href, quote=True)}\">{label} shock</a>")
    return f"<p>{''.join(links)}</p>"


def shock_html(
    underlying: str, store: SnapshotStore, journal: Journal, now: datetime, pct: float
) -> str:
    from bot.theme import page

    body = shock_text(underlying, store, journal, now, pct)
    desk_href = f"/dashboard?underlying={_urlencode(underlying)}"
    title = f"{underlying} shock {pct:+g}%"
    content = (
        f"<p><a class=\"btn\" href=\"{html.escape(desk_href, quote=True)}\">← back to {html.escape(underlying)} desk</a></p>"
        f"{shock_buttons(underlying)}"
        f"<pre>{html.escape(body)}</pre>"
    )
    return page(
        title, f"{title}, from stored snapshots", body.splitlines()[0] if body else "",
        content, underlying, "dashboard", store.read_settings(),
        f"/shock?underlying={_urlencode(underlying)}&pct={pct:g}",
    )
