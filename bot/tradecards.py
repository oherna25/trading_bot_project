from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from bot.metareason import generate_reason, select_reason_backend
from bot.exposure import compute_gex
from bot.ideas import cached_earnings, parse_timeframe, rank_ideas, symbols_for, unknown_symbols
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.models import HttpPost
from bot.shapes import Bar, ChainSnapshot, GexProfile
from bot.store import SnapshotStore

OPTIONS_DISABLED = "options ideas are disabled by your saved profile"
PROFILE_REQUIRED = "trade cards need a saved operator profile; reply profile for the questionnaire"
REASON_CACHE_MAX = 2000

_reason_cache: dict[str, str] = {}


def clear_reason_cache() -> None:
    _reason_cache.clear()


def _reason_cache_key(stats: Mapping[str, Any], http_post: HttpPost | None) -> str:
    post_id = "live" if http_post is None else f"stub-{id(http_post)}"
    return post_id + ":" + json.dumps(stats, sort_keys=True, default=str)


def _reason_cache_store(key: str, reason: str) -> None:
    if len(_reason_cache) >= REASON_CACHE_MAX:
        _reason_cache.pop(next(iter(_reason_cache)), None)
    _reason_cache[key] = reason


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
    earnings_date: Optional[str] = None
    iv_rank: Optional[float] = None


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
    earnings_date = stats.get("earnings_date")
    if earnings_date is not None:
        parts.append(f"earnings {earnings_date} inside expiry")
    return ", ".join(parts) + "."


def build_trade_cards(
    entries: Sequence[tuple[str, ChainSnapshot, GexProfile, Optional[Bar]]],
    max_days: Optional[int],
    now: datetime,
    journal: Journal,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    iv_history: Mapping[str, Sequence[float]] | None = None,
    earnings: Mapping[str, date] | None = None,
) -> list[TradeCard]:
    ideas = rank_ideas(entries, max_days, now=now, iv_history=iv_history, earnings=earnings)
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
            "earnings_date": idea.earnings_date,
            "iv_rank": idea.iv_rank,
        }
        reason_source = "fallback"
        if backend == "meta":
            cache_key = _reason_cache_key(stats, http_post)
            cached = _reason_cache.get(cache_key)
            if cached is not None:
                reason = cached
                reason_source = "meta"
                journal.append(
                    now,
                    "trade_card_reason",
                    {
                        "symbol": idea.symbol,
                        "snapshot_id": idea.snapshot_id,
                        "latency_ms": 0.0,
                        "cached": True,
                    },
                )
            else:
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
                        reason_source = "meta"
                        _reason_cache_store(cache_key, reason)
                        journal.append(
                            now,
                            "trade_card_reason",
                            {
                                "symbol": idea.symbol,
                                "snapshot_id": idea.snapshot_id,
                                "latency_ms": latency_ms,
                                "cached": False,
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
                earnings_date=idea.earnings_date,
                iv_rank=idea.iv_rank,
            )
        )
    return cards


def bar_at_or_before(store: SnapshotStore, symbol: str, stamp: datetime) -> Optional[Bar]:
    past = [bar for bar in store.read_bars(symbol) if bar.timestamp_utc <= stamp]
    if past:
        return max(past, key=lambda bar: bar.timestamp_utc)
    return store.read_latest_bar(symbol)


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
    if profile is None:
        journal.append(now, "ideas_blocked", {"reason": "profile required"})
        return None
    if not profile.options_ok:
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
        bar = store.read_latest_bar(symbol)
        entries.append(
            (
                snapshot_id,
                snapshot,
                compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None),
                bar,
            )
        )
        used.append(symbol)
        snapshot_ids.append(snapshot_id)
    if not entries:
        raise ValueError(f"no snapshot stored for {underlying}")
    cards = build_trade_cards(
        entries,
        max_days,
        now,
        journal,
        env,
        http_post,
        iv_history=histories_for(store, used),
        earnings=cached_earnings(used, now),
    )
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
        if store.read_profile() is None:
            return PROFILE_REQUIRED
        return OPTIONS_DISABLED
    return format_trade_cards(cards)


def format_trade_cards(cards: Sequence[TradeCard]) -> str:
    cards = list(cards)
    if not cards:
        return "trade cards: none found"
    lines = [f"trade cards ({len(cards)}):"]
    for number, card in enumerate(cards, 1):
        line = (
            f"{number}. {card.symbol} {card.direction} {card.spread} "
            f"expiry {card.expiry} confidence {card.confidence:.2f} "
            f"max loss {card.max_loss} breakeven {card.breakeven} wall {card.wall_exposure} "
            f"{card.regime} spot {card.spot} flip {card.flip_point}"
        )
        if card.earnings_date is not None:
            line += f" earnings {card.earnings_date}"
        if card.iv_rank is not None:
            line += f" iv rank {card.iv_rank:.1f}"
        lines.append(f"{line}: {card.reason}")
    return "\n".join(lines)


def _rounded(value: Optional[float], places: int) -> Optional[float]:
    return None if value is None else round(value, places)


def cards_to_json(cards: Sequence[TradeCard]) -> dict:
    return {
        "cards": [
            {
                "symbol": card.symbol,
                "direction": card.direction,
                "spread": card.spread,
                "strike": _rounded(card.strike, 2),
                "expiry": card.expiry,
                "call_or_put": card.call_or_put,
                "hedge_strike": _rounded(card.hedge_strike, 2),
                "confidence": _rounded(card.confidence, 4),
                "snapshot_id": card.snapshot_id,
                "max_loss": _rounded(card.max_loss, 2),
                "breakeven": _rounded(card.breakeven, 2),
                "net_debit": _rounded(card.net_debit, 2),
                "spread_pct": _rounded(card.spread_pct, 4),
                "wall_exposure": _rounded(card.wall_exposure, 2),
                "regime": card.regime,
                "spot": _rounded(card.spot, 2),
                "flip_point": _rounded(card.flip_point, 2),
                "reason": card.reason,
                "reason_source": card.reason_source,
                "earnings_date": card.earnings_date,
                "iv_rank": _rounded(card.iv_rank, 1),
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
        if card.earnings_date is not None:
            stats.append(f"earnings {card.earnings_date} inside expiry")
        if card.iv_rank is not None:
            stats.append(f"iv rank {card.iv_rank:.1f}")
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
