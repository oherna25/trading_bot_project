from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Mapping

from bot.diff import compact_dollars, pct
from bot.journal import Journal
from bot.models import HttpPost
from bot.shapes import DashboardSettings
from bot.store import SnapshotStore, need_snapshot_text
from bot.tradecards import PROFILE_REQUIRED, TradeCard


def _money(value: Any) -> str:
    try:
        return compact_dollars(float(value))
    except (TypeError, ValueError):
        return "n/a"


def share_text(card: TradeCard) -> str:
    from bot.theme import BRAND
    from bot.tradecards import entry_text, targets_text

    wall = _money(card.wall_exposure) if card.wall_exposure is not None else "n/a"
    spot = f"{card.spot:g}" if card.spot is not None else "n/a"
    flip = f"{card.flip_point:g}" if card.flip_point is not None else "none"
    loss = f"max loss {_money(card.max_loss)}" if card.max_loss is not None else "loss uncapped"
    lines = [
        f"contract {card.symbol} {card.direction} {card.spread} {card.expiry} (confidence {pct(card.confidence)})",
        f"{entry_text(card)} | {targets_text(card)}",
        f"wall {wall} | regime {card.regime} | spot {spot} vs flip {flip} | {loss}",
        f"why: {card.reason}",
        f"{BRAND} desk: /dashboard?underlying={card.symbol}",
    ]
    return "\n".join(lines)


def share_html(card: TradeCard, settings: DashboardSettings | None = None) -> str:
    from urllib.parse import quote as _urlencode

    from bot.theme import BRAND, page

    resolved = settings or DashboardSettings()
    title = f"{card.symbol} trade card"
    desk_href = f"/dashboard?underlying={_urlencode(card.symbol)}"
    loop = (
        f"<p>Shared via {html.escape(BRAND)}. "
        f"<a href=\"{html.escape(desk_href, quote=True)}\">Open the {html.escape(card.symbol)} desk</a>.</p>"
    )
    content = f"<pre>{html.escape(share_text(card), quote=False)}</pre>{loop}"
    return page(
        title, f"{card.symbol} {card.direction}", f"snapshot {card.snapshot_id}",
        content, card.symbol, "cards", resolved,
        f"/share?underlying={card.symbol}",
    )


def share_card(
    index: int,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    from bot.demo import demo_cards

    if store.read_profile() is None:
        return PROFILE_REQUIRED
    if snapshot_id is None:
        latest = store.read_latest(underlying)
        if latest is None:
            raise ValueError(need_snapshot_text(underlying, store))
        snapshot_id = latest[0]
    _, cards = demo_cards(underlying, store, journal, now, snapshot_id, env, http_post)
    if not cards:
        raise ValueError(f"no cards for {underlying} snapshot {snapshot_id}")
    if index < 0 or index >= len(cards):
        raise ValueError(f"index {index} out of range (0-{len(cards) - 1})")
    return share_text(cards[index])
