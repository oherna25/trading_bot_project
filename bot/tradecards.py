from __future__ import annotations

import hashlib
import html
import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from bot.diff import compact_dollars, pct
from bot.metareason import generate_reason, select_reason_backend
from bot.exposure import compute_gex
from bot.ideas import cached_earnings, parse_timeframe, rank_ideas, symbols_for, unknown_symbols
from bot.ivrank import histories_for
from bot.journal import Journal
from bot.models import HttpPost
from bot.shapes import Bar, ChainSnapshot, DashboardSettings, GexProfile
from bot.store import SnapshotStore, need_snapshot_text

OPTIONS_DISABLED = "options ideas are disabled by your saved profile"
PROFILE_REQUIRED = "trade cards need a saved operator profile; reply profile for the questionnaire"
SIZING_HINT = "save a profile for sizing -- next: reply profile for the questionnaire"
SIZED_DEFAULT_CONTRACTS = 3
REASON_CACHE_MAX = 2000

_reason_cache: dict[str, str] = {}
_reason_cache_version = 0


def clear_reason_cache() -> None:
    _reason_cache.clear()


def reason_cache_version() -> int:
    return _reason_cache_version


def _reason_cache_key(stats: Mapping[str, Any], http_post: HttpPost | None) -> str:
    post_id = "live" if http_post is None else f"stub-{id(http_post)}"
    return post_id + ":" + json.dumps(stats, sort_keys=True, default=str)


def _reason_cache_store(key: str, reason: str) -> None:
    global _reason_cache_version
    if len(_reason_cache) >= REASON_CACHE_MAX:
        _reason_cache.pop(next(iter(_reason_cache)), None)
    _reason_cache[key] = reason
    _reason_cache_version += 1


REASON_QUEUE_MAX = 256

_reason_queue: deque[tuple] = deque()
_reason_queued_keys: set[str] = set()
_reason_queue_lock = threading.Lock()
_reason_worker: threading.Thread | None = None
_reason_stop = threading.Event()


def clear_reason_queue() -> None:
    with _reason_queue_lock:
        _reason_queue.clear()
        _reason_queued_keys.clear()


def pending_reason_count() -> int:
    with _reason_queue_lock:
        return len(_reason_queue)


def enqueue_reason(
    stats: dict[str, Any],
    cache_key: str,
    env: Mapping[str, str] | None,
    http_post: Any,
    journal: Journal,
    now: datetime,
    symbol: str,
    snapshot_id: str,
) -> str:
    with _reason_queue_lock:
        if cache_key in _reason_cache or cache_key in _reason_queued_keys:
            return "queued"
        if len(_reason_queue) >= REASON_QUEUE_MAX:
            return "reason queue full"
        _reason_queue.append((stats, cache_key, env, http_post, journal, now, symbol, snapshot_id))
        _reason_queued_keys.add(cache_key)
        return "queued"


def drain_reason_queue(max_jobs: int | None = None) -> int:
    done = 0
    while True:
        with _reason_queue_lock:
            if not _reason_queue or (max_jobs is not None and done >= max_jobs):
                break
            stats, cache_key, env, http_post, journal, now, symbol, snapshot_id = _reason_queue.popleft()
        try:
            reason, latency_ms = generate_reason(stats, env, http_post)
        except Exception as exc:
            journal.append(
                now, "reason_failed",
                {"symbol": symbol, "snapshot_id": snapshot_id, "reason": type(exc).__name__,
                 "error": str(exc)[:200]},
            )
        else:
            _reason_cache_store(cache_key, reason)
            clear_trade_cards_cache()
            journal.append(
                now,
                "trade_card_reason",
                {"symbol": symbol, "snapshot_id": snapshot_id, "latency_ms": latency_ms,
                 "cached": False},
            )
        finally:
            with _reason_queue_lock:
                _reason_queued_keys.discard(cache_key)
        done += 1
    return done


def _reason_loop(poll_seconds: float, max_jobs: int) -> None:
    while not _reason_stop.wait(poll_seconds):
        try:
            drain_reason_queue(max_jobs=max_jobs)
        except Exception:
            pass


def start_reason_worker(poll_seconds: float = 5.0, max_jobs: int = 8) -> None:
    global _reason_worker
    with _reason_queue_lock:
        if _reason_worker is not None and _reason_worker.is_alive():
            return
        _reason_stop.clear()
        _reason_worker = threading.Thread(
            target=_reason_loop, args=(poll_seconds, max_jobs), daemon=True, name="reason-worker"
        )
        _reason_worker.start()


def stop_reason_worker(timeout: float = 5.0) -> None:
    global _reason_worker
    with _reason_queue_lock:
        worker, _reason_worker = _reason_worker, None
    if worker is not None:
        _reason_stop.set()
        worker.join(timeout=timeout)


CARD_CACHE_MAX = 32

_card_cache: dict[str, list[TradeCard]] = {}


def clear_trade_cards_cache() -> None:
    _card_cache.clear()


def chain_fingerprint(snapshot: ChainSnapshot) -> str:
    bits = [
        (
            contract.strike, contract.expiry, contract.call_or_put, contract.open_interest,
            contract.gamma, contract.vega, contract.delta, contract.price, contract.bid,
            contract.ask, contract.iv,
        )
        for contract in snapshot.contracts_list
    ]
    return hashlib.sha256(repr(bits).encode("utf-8")).hexdigest()[:16]


def bar_signature(bar: Optional[Bar]) -> str:
    if bar is None:
        return "no-bar"
    return repr(
        (bar.interval, bar.timestamp_utc.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume)
    )


def trade_cards_cache_key(
    text: str,
    inputs: Sequence[tuple[str, ChainSnapshot, Optional[Bar]]],
    profile: Any,
    iv_history: Mapping[str, Sequence[float]] | None,
    earnings: Mapping[str, Any] | None,
    now: datetime,
) -> str:
    parts = [text, now.date().isoformat(), repr(profile)]
    for snapshot_id, snapshot, bar in inputs:
        parts.append(snapshot_id)
        parts.append(chain_fingerprint(snapshot))
        parts.append(bar_signature(bar))
    parts.append(repr(iv_history))
    parts.append(repr(earnings))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def peek_trade_cards_key(text: str, underlying: str, store: SnapshotStore, now: datetime) -> str | None:
    inputs: list[tuple[str, ChainSnapshot, Optional[Bar]]] = []
    used: list[str] = []
    for symbol in symbols_for(text, store, underlying):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        inputs.append((snapshot_id, snapshot, store.read_latest_bar(symbol)))
        used.append(symbol)
    if not inputs:
        return None
    return trade_cards_cache_key(
        text, inputs, store.read_profile(), histories_for(store, used), store.all_earnings(), now
    )


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
    entry: str = ""


def fallback_reason(stats: Mapping[str, Any]) -> str:
    parts = [f"{stats.get('direction', '')} via {stats.get('spread', '')}"]
    wall = stats.get("wall_exposure")
    if wall is not None:
        parts.append(f"wall exposure {compact_dollars(wall)}")
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
        if backend in ("meta", "user", "baseten"):
            cache_key = _reason_cache_key(stats, http_post)
            cached = _reason_cache.get(cache_key)
            if cached is not None:
                reason = cached
                reason_source = backend
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
                disposition = enqueue_reason(
                    stats, cache_key, env, http_post, journal, now, idea.symbol, idea.snapshot_id
                )
                reason = fallback_reason(stats)
                journal.append(
                    now,
                    "reason_fallback",
                    {"symbol": idea.symbol, "snapshot_id": idea.snapshot_id, "reason": disposition},
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
                entry=idea.trigger,
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
        raise ValueError(need_snapshot_text(missing, store))
    max_days = parse_timeframe(text)
    inputs: list[tuple[str, ChainSnapshot, Optional[Bar]]] = []
    used = []
    snapshot_ids = []
    for symbol in symbols_for(text, store, underlying):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        inputs.append((snapshot_id, snapshot, store.read_latest_bar(symbol)))
        used.append(symbol)
        snapshot_ids.append(snapshot_id)
    if not inputs:
        raise ValueError(need_snapshot_text(underlying, store))
    iv_history = histories_for(store, used)
    cache_key: Optional[str] = None
    if http_post is None and select_reason_backend(journal, now, env)[0] not in ("meta", "user", "baseten"):
        cache_key = trade_cards_cache_key(text, inputs, profile, iv_history, store.all_earnings(), now)
        hit = _card_cache.get(cache_key)
        if hit is not None:
            journal.append(
                now, "ideas_cached", {"query": text, "symbols": used, "count": len(hit)}
            )
            return list(hit)
    fetched = cached_earnings(used, now)
    for symbol, day in fetched.items():
        store.save_earnings(symbol, day.isoformat())
    entries = [
        (
            snapshot_id,
            snapshot,
            compute_gex(snapshot, snapshot_id, spot=bar.close if bar is not None else None),
            bar,
        )
        for snapshot_id, snapshot, bar in inputs
    ]
    cards = build_trade_cards(
        entries,
        max_days,
        now,
        journal,
        env,
        http_post,
        iv_history=iv_history,
        earnings={**store.all_earnings(), **fetched},
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
    if store.read_settings().live_auto_send:
        from bot.config import operator_config
        from bot.live_trade import maybe_auto_live

        maybe_auto_live(cards, store, journal, operator_config(store.read_profile()), now)
    if cache_key is not None:
        if len(_card_cache) >= CARD_CACHE_MAX:
            _card_cache.pop(next(iter(_card_cache)), None)
        _card_cache[cache_key] = list(cards)
    return cards


def risk_cap_for(profile) -> float:
    from bot.profile import suggest_params

    params = suggest_params(profile)
    return float(params["risk_per_trade"] * params["account_size"])


def size_for_card(
    max_loss: float | None, cap: float, default: int = SIZED_DEFAULT_CONTRACTS
) -> tuple[int, float | None, str]:
    cap_text = compact_dollars(cap)
    if max_loss is None or max_loss <= 0:
        return (
            1,
            None,
            f"1 contract, uncapped dollars at risk, within your {cap_text} cap unknown -- size by feel",
        )
    affordable = int(cap // max_loss)
    if affordable >= default:
        at_risk = default * max_loss
        return (
            default,
            at_risk,
            f"{default} contracts, {compact_dollars(at_risk)} at risk, within your {cap_text} cap",
        )
    if affordable >= 1:
        at_risk = affordable * max_loss
        word = "contract" if affordable == 1 else "contracts"
        return (
            affordable,
            at_risk,
            f"{affordable} {word}, {compact_dollars(at_risk)} at risk, "
            f"within your {cap_text} cap (shrunk from {default} to fit your cap)",
        )
    return (
        1,
        max_loss,
        f"1 contract, {compact_dollars(max_loss)} at risk, over your {cap_text} cap "
        f"-- shrunk from {default} to 1, still over",
    )


def format_sized_cards(cards: Sequence[TradeCard], profile) -> str:
    cards = list(cards)
    if not cards:
        return "trade cards: none found"
    if profile is None:
        return format_trade_cards(cards) + "\n" + SIZING_HINT
    cap = risk_cap_for(profile)
    base = format_trade_cards(cards).splitlines()
    out = [base[0]]
    for card, line in zip(cards, base[1:]):
        _, _, sizing = size_for_card(card.max_loss, cap)
        out.append(f"{line} | {sizing}")
    return "\n".join(out)


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
    return format_sized_cards(cards, store.read_profile())


def format_trade_cards(cards: Sequence[TradeCard]) -> str:
    cards = list(cards)
    if not cards:
        return "trade cards: none found"
    lines = [f"trade cards ({len(cards)}):"]
    for number, card in enumerate(cards, 1):
        line = (
            f"{number}. {contract_text(card)} "
            f"confidence {pct(card.confidence)} "
            f"max loss {_rounded(card.max_loss, 2)} breakeven {_rounded(card.breakeven, 2)} "
            f"wall {compact_dollars(card.wall_exposure) if card.wall_exposure is not None else 'none'} {card.regime} "
            f"spot {_rounded(card.spot, 2)} flip {_rounded(card.flip_point, 2)}"
        )
        if card.earnings_date is not None:
            line += f" earnings {card.earnings_date}"
        if card.iv_rank is not None:
            line += f" iv rank {card.iv_rank:.1f}"
        line += f" | {entry_text(card)} | {targets_text(card)}"
        lines.append(f"{line} | reason {card.reason}")
    return "\n".join(lines)


def _rounded(value: Optional[float], places: int) -> Optional[float]:
    return None if value is None else round(value, places)


def contract_text(card: TradeCard) -> str:
    base = f"{card.symbol} {card.direction} {card.spread} expiry {card.expiry}"
    legs = f"{card.call_or_put} {card.strike:g}"
    if card.hedge_strike is not None:
        legs += f" / hedge {card.hedge_strike:g}"
    debit = _net_text(card.net_debit)
    return f"contract {base} ({legs}, {debit})"


def targets_text(card: TradeCard) -> str:
    parts: list[str] = []
    if card.breakeven is not None:
        parts.append(f"breakeven {card.breakeven:g}")
    if card.hedge_strike is not None:
        parts.append(f"target {card.hedge_strike:g} (max profit beyond)")
    if card.max_loss is not None:
        parts.append(f"capped loss ${card.max_loss:.0f}")
    if not parts:
        return "targets open-ended"
    return "targets " + ", ".join(parts)


def entry_text(card: TradeCard) -> str:
    return f"entry {card.entry}" if card.entry else "entry n/a"


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
                "entry": card.entry,
                "contract": contract_text(card),
                "targets": targets_text(card),
            }
            for card in cards
        ]
    }


def _money(value: Optional[float]) -> str:
    return compact_dollars(value) if value is not None else "n/a"


def _net_text(net_debit: Optional[float]) -> str:
    if net_debit is None:
        return "n/a"
    if net_debit >= 0:
        return f"${net_debit:.0f} debit"
    return f"${-net_debit:.0f} credit"


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def confidence_word(confidence: float) -> str:
    if confidence >= 0.7:
        return "strong"
    if confidence >= 0.5:
        return "moderate"
    return "weak"


def confidence_meter_html(confidence: float) -> str:
    width = _clamp01(float(confidence)) * 100.0
    band = "hot" if confidence >= 0.7 else ("warm" if confidence >= 0.5 else "cool")
    return (
        f"<div class=\"conf-row\"><span>confidence <b>{pct(confidence)}</b> "
        f"({confidence_word(confidence)})</span>"
        f"<div class=\"meter {band}\" role=\"img\" aria-label=\"confidence {pct(confidence)}\">"
        f"<i style=\"width:{width:.0f}%\"></i></div></div>"
    )


REASON_SOURCE_LABELS = {
    "fallback": "reason: deterministic (offline)",
    "meta": "reason: Muse Spark",
    "baseten": "reason: Muse Spark Open (Baseten)",
    "user": "reason: your model",
}


def card_badges_html(card: TradeCard) -> str:
    arrow = "▲" if card.direction == "long" else ("▼" if card.direction == "short" else "●")
    regime_mark = "●"
    source = REASON_SOURCE_LABELS.get(card.reason_source, f"reason: {card.reason_source}")
    return (
        f"<span class=\"badge {html.escape(card.direction)}\">{html.escape(arrow)} "
        f"{html.escape(card.direction)}</span>"
        f"<span class=\"badge regime\">{html.escape(regime_mark)} "
        f"{html.escape(card.regime)}</span>"
        f"<span class=\"badge expiry\">{html.escape(card.expiry)}</span>"
        f"<span class=\"badge regime\">{html.escape(source)}</span>"
    )


def spot_flip_gauge_svg(spot: Optional[float], flip: Optional[float]) -> str:
    width, height = 300, 56
    left, right, top = 12, 12, 10
    track_y = top + 16
    if spot is None or flip is None:
        return (
            f"<svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" "
            "role=\"img\" aria-label=\"spot versus flip unavailable\">"
            "<text x=\"12\" y=\"32\" font-size=\"12\" fill=\"#8b949e\">"
            "spot vs flip n/a</text></svg>"
        )
    center = (spot + flip) / 2.0
    half = max(abs(spot - flip) * 2.0, abs(spot) * 0.03, 1.0)

    def x_of(value: float) -> float:
        frac = (value - (center - half)) / (2.0 * half)
        return left + _clamp01(frac) * (width - left - right)

    spot_x = x_of(spot)
    flip_x = x_of(flip)
    above = spot >= flip
    caption = f"spot {spot:g} {'above' if above else 'below'} flip {flip:g}"
    return (
        f"<svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" "
        f"role=\"img\" aria-label=\"{html.escape(caption)}\">"
        f"<rect x=\"{left}\" y=\"{track_y}\" width=\"{width - left - right}\" height=\"8\" "
        "rx=\"4\" fill=\"#eef1f4\"/>"
        f"<line x1=\"{flip_x:.1f}\" y1=\"{top}\" x2=\"{flip_x:.1f}\" y2=\"{track_y + 14}\" "
        "stroke=\"#24292f\" stroke-width=\"2\" stroke-dasharray=\"5 3\">"
        f"<title>flip {flip:g}</title></line>"
        f"<circle cx=\"{spot_x:.1f}\" cy=\"{track_y + 4:.1f}\" r=\"7\" fill=\"#0969da\">"
        f"<title>spot {spot:g}</title></circle>"
        f"<text x=\"{left}\" y=\"{height - 6}\" font-size=\"11\" fill=\"#57606a\">"
        f"{html.escape(f'{center - half:g}')}</text>"
        f"<text x=\"{width - right}\" y=\"{height - 6}\" text-anchor=\"end\" "
        f"font-size=\"11\" fill=\"#57606a\">{html.escape(f'{center + half:g}')}</text>"
        "</svg>"
    )


def payoff_svg(card: TradeCard) -> str:
    width, height = 320, 132
    left, right, top, bottom = 44, 12, 12, 26
    loss = card.max_loss if card.max_loss is not None and card.max_loss > 0 else (
        abs(card.net_debit) if card.net_debit else 100.0
    )
    low = high = card.strike
    if card.hedge_strike is not None:
        low = min(card.strike, card.hedge_strike)
        high = max(card.strike, card.hedge_strike)
        span = max(high - low, 1.0)
        x0, x1 = low - span * 0.6, high + span * 0.6
    else:
        ref = abs((card.breakeven or card.strike) - card.strike)
        span = max(ref * 2.0, abs(card.strike) * 0.05, 5.0)
        x0, x1 = card.strike - span, card.strike + span
    width_dollars = abs(high - low) * 100.0 if card.hedge_strike is not None else loss * 2.0
    profit = max(width_dollars - loss, loss * 0.5, 10.0)
    y_top, y_bot = profit * 1.15, -loss * 1.15
    if y_top <= 0:
        y_top = 10.0
    if y_bot >= 0:
        y_bot = -10.0

    def x_of(value: float) -> float:
        frac = (value - x0) / (x1 - x0) if x1 != x0 else 0.5
        return left + _clamp01(frac) * (width - left - right)

    def y_of(pnl: float) -> float:
        frac = (y_top - pnl) / (y_top - y_bot) if y_top != y_bot else 0.5
        return top + frac * (height - top - bottom)

    zero_y = y_of(0.0)
    if card.hedge_strike is None:
        if card.call_or_put == "put":
            points = [(x0, profit), (card.strike, -loss), (x1, -loss)]
        else:
            points = [(x0, -loss), (card.strike, -loss), (x1, profit)]
    elif card.call_or_put == "put":
        points = [(x0, profit), (low, profit), (high, -loss), (x1, -loss)]
    else:
        points = [(x0, -loss), (low, -loss), (high, profit), (x1, profit)]
    path = "M " + " L ".join(f"{x_of(x):.1f} {y_of(y):.1f}" for x, y in points)
    kink_labels = []
    for mark, label in ((low, f"{low:g}"), (high, f"{high:g}")):
        kink_labels.append(
            f"<text x=\"{x_of(mark):.1f}\" y=\"{height - 8}\" text-anchor=\"middle\" "
            f"font-size=\"10\" fill=\"#57606a\">{html.escape(label)}</text>"
        )
    caption = f"payoff at expiry, max loss {_money(card.max_loss)}"
    if card.breakeven is not None:
        caption += f", breakeven {card.breakeven:g}"
    return (
        f"<svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" "
        f"role=\"img\" aria-label=\"{html.escape(caption)}\">"
        f"<line x1=\"{left}\" y1=\"{zero_y:.1f}\" x2=\"{width - right}\" y2=\"{zero_y:.1f}\" "
        "stroke=\"#57606a\" stroke-width=\"1\" stroke-dasharray=\"4 3\"/>"
        f"<path d=\"{path}\" fill=\"none\" stroke=\"#0969da\" stroke-width=\"2.5\" "
        "stroke-linejoin=\"round\" stroke-linecap=\"round\"/>"
        f"<text x=\"{left}\" y=\"{top + 10}\" font-size=\"10\" fill=\"#1a7f37\">"
        f"+{_money(profit)}</text>"
        f"<text x=\"{left}\" y=\"{height - bottom + 12:.0f}\" font-size=\"10\" fill=\"#cf222e\">"
        f"-{_money(loss)}</text>"
        f"{''.join(kink_labels)}</svg>"
    )


def card_facts_html(card: TradeCard, profile=None) -> str:
    facts = [
        ("Spread", f"{card.spread}", "the option structure: which legs you buy and sell"),
        ("Strike", f"{card.strike:g}", "the price level this trade is anchored to"),
        ("Net", _net_text(card.net_debit), "what you pay (debit) or collect (credit) per share to open"),
        ("Max loss", _money(card.max_loss), "the most you can lose on this trade if it goes fully wrong"),
        ("Breakeven", f"{card.breakeven:g}" if card.breakeven is not None else "n/a",
         "spot must cross this price by expiry for the trade to profit"),
        ("Wall", _money(card.wall_exposure),
         "dealer gamma exposure at this strike, in dollars; big walls tend to slow or pin price"),
        ("IV rank", f"{card.iv_rank:.0f}" if card.iv_rank is not None else "n/a",
         "where implied volatility sits in its recent range: 0 cheap, 100 expensive"),
        ("Earnings", card.earnings_date or "none", "earnings date inside the trade window, if any"),
    ]
    if profile is None:
        sizing = SIZING_HINT
    else:
        _, _, sizing = size_for_card(card.max_loss, risk_cap_for(profile))
        sizing = sizing
    cells = "".join(
        f"<div class=\"fact\" title=\"{html.escape(hint, quote=True)}\">"
        f"<span>{html.escape(name)}</span><b>{html.escape(value)}</b></div>"
        for name, value, hint in facts
    )
    return f"<div class=\"facts\">{cells}</div><p class=\"sub\">size: {html.escape(sizing)}</p>"


def card_article(
    card: TradeCard,
    index: int,
    profile=None,
    compact: bool = False,
) -> str:
    stats = [
        f"contract {card.symbol} {card.direction} {card.spread} expiry {card.expiry}",
        f"{entry_text(card)}",
        f"{targets_text(card)}",
        f"confidence {pct(card.confidence)} ({confidence_word(card.confidence)})",
        f"max loss {_money(card.max_loss)}",
        f"breakeven {_rounded(card.breakeven, 2) if card.breakeven is not None else 'n/a'}",
        f"net {_net_text(card.net_debit)}",
        f"spread width {card.spread_pct:.1f}%" if card.spread_pct is not None else "spread width n/a",
        f"wall exposure {_money(card.wall_exposure)}",
        f"regime {card.regime}",
        f"spot {_rounded(card.spot, 2) if card.spot is not None else 'n/a'} "
        f"vs flip {_rounded(card.flip_point, 2) if card.flip_point is not None else 'n/a'}",
    ]
    if card.earnings_date is not None:
        stats.append(f"earnings {card.earnings_date} inside expiry")
    if card.iv_rank is not None:
        stats.append(f"iv rank {card.iv_rank:.1f}")
    if profile is None:
        stats.append(SIZING_HINT)
    else:
        _, _, sizing = size_for_card(card.max_loss, risk_cap_for(profile))
        stats.append(f"size: {sizing}")
    items = "".join(f"<li>{html.escape(item)}</li>" for item in stats)
    take = (
        "<form class=\"block\" method=\"post\" action=\"/action\">"
        "<input type=\"hidden\" name=\"tool\" value=\"take_paper_trade\">"
        f"<input type=\"hidden\" name=\"underlying\" value=\"{html.escape(card.symbol, quote=True)}\">"
        f"<input type=\"hidden\" name=\"snapshot\" value=\"{html.escape(card.snapshot_id, quote=True)}\">"
        f"<input type=\"hidden\" name=\"index\" value=\"{index}\">"
        "<button class=\"solid\" type=\"submit\">take this trade (paper)</button></form>"
    )
    gauge_cap = "spot above flip: supports longs" if (
        card.spot is not None and card.flip_point is not None and card.spot >= card.flip_point
    ) else (
        "spot below flip: favors shorts" if (
            card.spot is not None and card.flip_point is not None
        ) else "spot vs flip unavailable"
    )
    regime_plain = (
        "dealers are long gamma, so moves tend to be damped and price often pins near big walls"
        if card.regime == "long-gamma"
        else "dealers are short gamma, so moves can speed up and walls break more easily"
    )
    plain = (
        f"in plain terms: a {card.direction} {card.spread} on {card.symbol}, "
        f"{pct(card.confidence)} confidence ({confidence_word(card.confidence)}); {regime_plain}."
    )
    visual = (
        "<div class=\"card-visual\">"
        f"<div class=\"card-head\"><h2>{html.escape(card.symbol)} "
        f"{html.escape(card.direction)} {html.escape(card.spread)}</h2>"
        f"{card_badges_html(card)}</div>"
        f"{confidence_meter_html(card.confidence)}"
        f"<p class=\"sub\">{html.escape(plain)}</p>"
        "<div class=\"visual-grid\">"
        "<div class=\"visual\"><h3>Payoff at expiry</h3>"
        f"{payoff_svg(card)}"
        f"<p class=\"cap\">{html.escape(targets_text(card))}</p></div>"
        "<div class=\"visual\"><h3>Spot vs flip</h3>"
        f"{spot_flip_gauge_svg(card.spot, card.flip_point)}"
        f"<p class=\"cap\">{html.escape(gauge_cap)}</p></div>"
        "</div>"
        f"{card_facts_html(card, profile)}"
        "</div>"
        f"<div class=\"card-body\"><ul>{items}</ul>"
        f"<p>reason: {html.escape(card.reason)}</p>{take}</div>"
    )
    return f"<article><div class=\"trade-card\">{visual}</div></article>"


def trade_cards_html(
    cards: Sequence[TradeCard],
    underlying: str,
    settings: Optional[DashboardSettings] = None,
    start_index: int = 0,
    profile=None,
) -> str:
    from bot.theme import page

    resolved = settings or DashboardSettings()
    cards = list(cards)
    title = f"{underlying} trade cards"
    if not cards:
        return page(
            title, title, f"{len(cards)} cards", "<p>trade cards: none found</p>",
            underlying, "cards", resolved, f"/cards?underlying={underlying}",
        )
    parts = [
        card_article(card, start_index + offset, profile=profile)
        for offset, card in enumerate(cards)
    ]
    glossary = (
        "<details><summary>new here? what am I looking at</summary>"
        "<p>Each card is one paper-trade idea built from the stored options chain. "
        "<b>Confidence</b> is how strongly the idea screens, as a percent. "
        "<b>Wall</b> is dealer gamma exposure at the strike, in dollars: big walls tend to slow or pin price. "
        "<b>Flip</b> is the spot price where dealer gamma changes sign; above it supports longs, below it favors shorts. "
        "<b>Long-gamma</b> means dealers dampen moves; <b>short-gamma</b> means they can amplify them. "
        "<b>Max loss</b> is the most the trade can lose. Hover any fact on a card for a short explanation. "
        "Nothing here is financial advice, and every take is paper until you arm live on the live page.</p></details>"
    )
    return page(
        title, title, f"{len(cards)} cards, from stored snapshots", glossary + "".join(parts),
        underlying, "cards", resolved, f"/cards?underlying={underlying}",
    )
