from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from bot.baseten import generate_reason, select_reason_backend
from bot.exposure import compute_gex
from bot.ideas import parse_timeframe, rank_ideas, symbols_for, unknown_symbols
from bot.journal import Journal
from bot.models import HttpPost
from bot.shapes import Bar, ChainSnapshot, GexProfile
from bot.store import SnapshotStore

OPTIONS_DISABLED = "options ideas are disabled by your saved profile"


@dataclass(frozen=True)
class TradeCard:
    symbol: str
    direction: str
    spread: str
    strike: float
    expiry: str
    call_or_put: str
    hedge_strike: Optional[float]
    confidence: float
    snapshot_id: str
    max_loss: Optional[float]
    breakeven: Optional[float]
    net_debit: Optional[float]
    spread_pct: Optional[float]
    wall_exposure: Optional[float]
    regime: str
    spot: Optional[float]
    flip_point: Optional[float]
    reason: str
    reason_source: str


def fallback_reason(stats: Mapping[str, Any]) -> str:
    parts = [f"{stats.get('direction', '')} via {stats.get('spread', '')}"]
    wall = stats.get("wall_exposure")
    if wall is not None:
        parts.append(f"wall exposure {wall:.0f}")
    flip = stats.get("flip_point")
    spot = stats.get("spot")
    if flip is not None and spot is not None:
        side = "above" if spot > flip else "below"
        parts.append(f"spot {spot:.1f} {side} flip {flip:.1f}")
    elif flip is not None:
        parts.append(f"flip {flip:.1f}")
    max_loss = stats.get("max_loss")
    if max_loss is not None:
        parts.append(f"capped loss ${max_loss:.0f}")
    breakeven = stats.get("breakeven")
    if breakeven is not None:
        parts.append(f"breakeven {breakeven:.1f}")
    return ", ".join(parts) + "."


def build_trade_cards(
    entries: Sequence[tuple[str, ChainSnapshot, GexProfile, Optional[Bar]]],
    max_days: Optional[int],
    now: datetime,
    journal: Journal,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> list[TradeCard]:
    ideas = rank_ideas(entries, max_days, now=now)
    by_snapshot = {
        snapshot_id: (snapshot, profile, bar) for snapshot_id, snapshot, profile, bar in entries
    }
    backend, backend_reason = select_reason_backend(journal, now, env)
    cards = []
    for idea in ideas:
        _, profile, bar = by_snapshot[idea.snapshot_id]
        spot = bar.close if bar is not None else None
        regime = "long-gamma" if profile.total_exposure >= 0 else "short-gamma"
        wall = profile.per_strike_exposure.get(idea.strike)
        stats: dict[str, Any] = {
            "symbol": idea.symbol,
            "direction": idea.direction,
            "spread": idea.spread,
            "strike": idea.strike,
            "expiry": idea.expiry,
            "call_or_put": idea.call_or_put,
            "hedge_strike": idea.hedge_strike,
            "confidence": idea.confidence,
            "max_loss": idea.max_loss,
            "breakeven": idea.breakeven,
            "net_debit": idea.net_debit,
            "spread_pct": idea.spread_pct,
            "wall_exposure": wall,
            "regime": regime,
            "spot": spot,
            "flip_point": profile.flip_point,
        }
        reason_source = "fallback"
        if backend == "baseten":
            try:
                reason, latency_ms = generate_reason(stats, env, http_post)
            except Exception as exc:
                reason = fallback_reason(stats)
                journal.append(
                    now,
                    "reason_fallback",
                    {
                        "symbol": idea.symbol,
                        "snapshot_id": idea.snapshot_id,
                        "reason": type(exc).__name__,
                    },
                )
            else:
                if not reason:
                    reason = fallback_reason(stats)
                    journal.append(
                        now,
                        "reason_fallback",
                        {"symbol": idea.symbol, "snapshot_id": idea.snapshot_id, "reason": "empty reason"},
                    )
                else:
                    reason_source = "baseten"
                    journal.append(
                        now,
                        "trade_card_reason",
                        {
                            "symbol": idea.symbol,
                            "snapshot_id": idea.snapshot_id,
                            "latency_ms": latency_ms,
                        },
                    )
        else:
            reason = fallback_reason(stats)
            journal.append(
                now,
                "reason_fallback",
                {"symbol": idea.symbol, "snapshot_id": idea.snapshot_id, "reason": backend_reason},
            )
        cards.append(
            TradeCard(
                symbol=idea.symbol,
                direction=idea.direction,
                spread=idea.spread,
                strike=idea.strike,
                expiry=idea.expiry,
                call_or_put=idea.call_or_put,
                hedge_strike=idea.hedge_strike,
                confidence=idea.confidence,
                snapshot_id=idea.snapshot_id,
                max_loss=idea.max_loss,
                breakeven=idea.breakeven,
                net_debit=idea.net_debit,
                spread_pct=idea.spread_pct,
                wall_exposure=wall,
                regime=regime,
                spot=spot,
                flip_point=profile.flip_point,
                reason=reason,
                reason_source=reason_source,
            )
        )
    return cards


def query_trade_cards(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> list[TradeCard] | None:
    profile = store.read_profile()
    if profile is not None and not profile.options_ok:
        journal.append(now, "ideas_blocked", {"reason": "options blocked by profile"})
        return None
    unknown = unknown_symbols(text, store)
    if unknown:
        missing = ", ".join(unknown)
        available = ", ".join(store.list_underlyings()) or "none"
        raise ValueError(
            f"no snapshot stored for {missing} (store has: {available}); "
            f"ingest it first, e.g. run_session.py --underlying {unknown[0]}"
        )
    max_days = parse_timeframe(text)
    entries = []
    used = []
    snapshot_ids = []
    for symbol in symbols_for(text, store, underlying):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        entries.append((snapshot_id, snapshot, compute_gex(snapshot, snapshot_id), store.read_latest_bar(symbol)))
        used.append(symbol)
        snapshot_ids.append(snapshot_id)
    if not entries:
        raise ValueError(f"no snapshot stored for {underlying}")
    cards = build_trade_cards(entries, max_days, now, journal, env, http_post)
    journal.append(
        now,
        "ideas",
        {
            "query": text,
            "symbols": used,
            "timeframe": max_days,
            "count": len(cards),
            "snapshot_ids": snapshot_ids,
            "cards": len(cards),
        },
    )
    return cards


def cards_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    cards = query_trade_cards(text, underlying, store, journal, now, env, http_post)
    if cards is None:
        return OPTIONS_DISABLED
    return format_trade_cards(cards)


def format_trade_cards(cards: Sequence[TradeCard]) -> str:
    cards = list(cards)
    if not cards:
        return "trade cards: none found"
    lines = [f"trade cards ({len(cards)}):"]
    for number, card in enumerate(cards, 1):
        lines.append(
            f"{number}. {card.symbol} {card.direction} {card.spread} "
            f"expiry {card.expiry} confidence {card.confidence:.2f} "
            f"max loss {card.max_loss} breakeven {card.breakeven} wall {card.wall_exposure} "
            f"{card.regime} spot {card.spot} flip {card.flip_point}: {card.reason}"
        )
    return "\n".join(lines)


def cards_to_json(cards: Sequence[TradeCard]) -> dict:
    return {
        "cards": [
            {
                "symbol": card.symbol,
                "direction": card.direction,
                "spread": card.spread,
                "strike": card.strike,
                "expiry": card.expiry,
                "call_or_put": card.call_or_put,
                "hedge_strike": card.hedge_strike,
                "confidence": card.confidence,
                "snapshot_id": card.snapshot_id,
                "max_loss": card.max_loss,
                "breakeven": card.breakeven,
                "net_debit": card.net_debit,
                "spread_pct": card.spread_pct,
                "wall_exposure": card.wall_exposure,
                "regime": card.regime,
                "spot": card.spot,
                "flip_point": card.flip_point,
                "reason": card.reason,
                "reason_source": card.reason_source,
            }
            for card in cards
        ]
    }


def _money(value: Optional[float]) -> str:
    return f"${value:.0f}" if value is not None else "n/a"


def _net_text(net_debit: Optional[float]) -> str:
    if net_debit is None:
        return "n/a"
    if net_debit >= 0:
        return f"${net_debit:.0f} debit"
    return f"${-net_debit:.0f} credit"


def trade_cards_html(cards: Sequence[TradeCard], underlying: str) -> str:
    cards = list(cards)
    title = f"{underlying} trade cards"
    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:2rem;line-height:1.5;color:#222}"
        "article{border:1px solid #ccc;padding:1.5rem;margin:0 0 1.5rem;max-width:42rem}"
        "h1{font-weight:600}h2{font-weight:600;margin:0 0 1rem}"
        "ul{list-style:none;padding:0;margin:0 0 1rem}"
        "li{margin:0 0 .25rem}p{margin:0}"
    )
    head = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><style>{style}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
    )
    if not cards:
        return head + "<p>trade cards: none found</p></body></html>"
    parts = [head]
    for card in cards:
        stats = [
            f"confidence {card.confidence:.2f}",
            f"max loss {_money(card.max_loss)}",
            f"breakeven {card.breakeven if card.breakeven is not None else 'n/a'}",
            f"net {_net_text(card.net_debit)}",
            f"spread width {card.spread_pct:.1f}%" if card.spread_pct is not None else "spread width n/a",
            f"wall exposure {card.wall_exposure:.0f}"
            if card.wall_exposure is not None
            else "wall exposure n/a",
            f"regime {card.regime}",
            f"spot {card.spot if card.spot is not None else 'n/a'} "
            f"vs flip {card.flip_point if card.flip_point is not None else 'n/a'}",
        ]
        items = "".join(f"<li>{html.escape(item)}</li>" for item in stats)
        parts.append(
            "<article>"
            f"<h2>{html.escape(card.symbol)} {html.escape(card.direction)} "
            f"{html.escape(card.spread)} expiry {html.escape(card.expiry)}</h2>"
            f"<ul>{items}</ul><p>{html.escape(card.reason)}</p>"
            "</article>"
        )
    parts.append("</body></html>")
    return "".join(parts)
