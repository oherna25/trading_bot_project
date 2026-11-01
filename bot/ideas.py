from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar
from urllib.parse import urlencode

from bot.config import read_dotenv as _read_dotenv
from bot.diff import compact_dollars
from bot.ivrank import iv_rank as _iv_rank
from bot.risk import even_legs
from bot.shapes import (
    DEFAULT_CONTRACT_MULTIPLIER,
    Bar,
    ChainSnapshot,
    GexProfile,
    OptionContract,
    TradeIdea,
)
from bot.store import SnapshotStore

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
EARNINGS_LOOKAHEAD_DAYS = 60
EARNINGS_TIMEOUT_SECONDS = 15.0

_earnings_cache: dict[tuple[tuple[str, ...], str], dict[str, date]] = {}


def _http_get_json(url: str, params: Mapping[str, str], timeout: float) -> Any:
    query = urlencode(dict(params))
    request = urllib.request.Request(f"{url}?{query}" if query else url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_earnings(
    symbols: Sequence[str],
    token: str,
    start: date,
    end: date,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, date]:
    wanted = {str(symbol).upper() for symbol in symbols}
    get = http_get or _http_get_json
    out: dict[str, date] = {}
    for symbol in sorted(wanted):
        payload = get(
            FINNHUB_EARNINGS_URL,
            {"from": start.isoformat(), "to": end.isoformat(), "token": token, "symbol": symbol},
            EARNINGS_TIMEOUT_SECONDS,
        )
        rows = payload.get("earningsCalendar") if isinstance(payload, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("symbol") or "").upper()
            if name not in wanted:
                continue
            try:
                day = date.fromisoformat(str(row.get("date") or "")[:10])
            except ValueError:
                continue
            if name not in out or day < out[name]:
                out[name] = day
    return out


def cached_earnings(symbols: Sequence[str], ref: datetime) -> dict[str, date]:
    token = os.environ.get("FINNHUB_API_KEY") or _read_dotenv().get("FINNHUB_API_KEY", "")
    if not token or not symbols:
        return {}
    key = (tuple(sorted({str(s).upper() for s in symbols})), ref.date().isoformat())
    if key not in _earnings_cache:
        try:
            _earnings_cache[key] = fetch_earnings(
                symbols, token, ref.date(), ref.date() + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)
            )
        except Exception:
            _earnings_cache[key] = {}
    return _earnings_cache[key]


def spans_earnings(ref: datetime, expiry: str, earnings_day: date | None) -> bool:
    if earnings_day is None:
        return False
    try:
        expiry_day = date.fromisoformat(expiry)
    except ValueError:
        return False
    return ref.date() <= earnings_day <= expiry_day

TIMEFRAME = {
    "week": 7,
    "weekly": 7,
    "weeks": 7,
    "month": 30,
    "monthly": 30,
    "months": 30,
    "quarter": 90,
    "quarterly": 90,
    "quarters": 90,
    "year": 365,
    "yearly": 365,
    "years": 365,
    "leap": 365,
    "leaps": 365,
}

DAYS_RE = re.compile(r"(?<!\w)(\d+)\s*(?:-\s*)?(?:days?|d)\b", re.IGNORECASE)
TICKER_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z]{1,5})(?![A-Za-z0-9_])")
FOR_TICKER_RE = re.compile(r"\b(?:for|on)\s+([A-Za-z]{1,5})\b", re.IGNORECASE)

NON_TICKER_WORDS = frozenset(
    """
    A I AN ME MY US OUR YOUR ITS THE AND FOR WITH FROM THIS THAT THESE THOSE
    EACH EVERY WHAT WHICH ABOUT ABOVE BELOW OVER UNDER NEAR PER VS ON IN OF TO
    OR AS BY AT IS ARE BE DO SHOW GIVE GET FIND NEED WANT RANK LIST TRADE
    IDEA IDEAS BRIEF GEX VEX DEX CARD CHAIN WALL WALLS FLIP SPOT CALL CALLS
    PUT PUTS LONG SHORT FLAT DEBIT LOSS GAIN PNL RISK ENTRY PRICE ETF DAY
    DAYS DAILY WEEK WEEKS MONTH YEAR YEARS LEAP LEAPS TODAY NEXT LAST TOP
    BEST NEW HOT ALL ANY SOME ONLY JUST BOTH UP DOWN OUT
    """.split()
)

SCAN_RE = re.compile(r"\bscan\b|\bscreener\b|\bbreadth\b|\ball symbols\b|\ball tickers\b", re.IGNORECASE)


def broad_scan_requested(text: str) -> bool:
    return bool(SCAN_RE.search(text or ""))

MIN_OPEN_INTEREST = 100.0
MAX_SPREAD_PCT = 10.0
TIGHT_SPREAD_PCT = 5.0
EARNINGS_CONFIDENCE_HAIRCUT = 0.15
EARNINGS_CONFIDENCE_FLOOR = 0.05
MAX_IDEAS = 10
MAX_PER_DIRECTION = 7
MAX_PER_SYMBOL = 5
MAX_PER_EXPIRY = 5
MAX_PER_STRIKE_CLUSTER = 2
MIN_DAYS_TO_EXPIRY = 1


def ideas_limit() -> int:
    try:
        configured = int(os.environ.get("SCAN_MAX_IDEAS", "") or 0)
    except ValueError:
        configured = 0
    if configured > 0:
        return max(1, min(50, configured))
    return 15

T = TypeVar("T")


def strike_cluster(strike: float) -> int:
    return int(round(strike))


def diversify(
    candidates: Sequence[T],
    limit: int,
    symbol_of: Callable[[T], str],
    expiry_of: Callable[[T], str],
    strike_of: Callable[[T], float],
    direction_of: Callable[[T], str] | None = None,
    max_per_symbol: int = MAX_PER_SYMBOL,
    max_per_expiry: int = MAX_PER_EXPIRY,
    max_per_cluster: int = MAX_PER_STRIKE_CLUSTER,
    max_per_direction: int = MAX_PER_DIRECTION,
) -> list[T]:
    """Round-robin fill across symbols with per-symbol, per-expiry, and per-cluster caps.

    Candidates arrive quality-sorted. Each round takes the next eligible candidate
    from every symbol in first-seen order. Caps only tighten, so a blocked head is
    dropped instead of deferred.
    """
    groups: dict[str, list[T]] = {}
    order: list[str] = []
    for candidate in candidates:
        key = symbol_of(candidate)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)
    per_symbol: dict[str, int] = {}
    per_expiry: dict[str, int] = {}
    per_cluster: dict[tuple[str, int], int] = {}
    per_direction: dict[str, int] = {}

    def blocked(candidate: T) -> bool:
        if per_symbol.get(symbol_of(candidate), 0) >= max_per_symbol:
            return True
        if per_expiry.get(expiry_of(candidate), 0) >= max_per_expiry:
            return True
        cluster = (symbol_of(candidate), strike_cluster(strike_of(candidate)))
        if per_cluster.get(cluster, 0) >= max_per_cluster:
            return True
        if direction_of is not None and per_direction.get(direction_of(candidate), 0) >= max_per_direction:
            return True
        return False

    def take(candidate: T) -> None:
        symbol = symbol_of(candidate)
        per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
        expiry = expiry_of(candidate)
        per_expiry[expiry] = per_expiry.get(expiry, 0) + 1
        cluster = (symbol, strike_cluster(strike_of(candidate)))
        per_cluster[cluster] = per_cluster.get(cluster, 0) + 1
        if direction_of is not None:
            direction = direction_of(candidate)
            per_direction[direction] = per_direction.get(direction, 0) + 1

    picked: list[T] = []
    while len(picked) < limit:
        progressed = False
        for key in order:
            group = groups[key]
            while group and blocked(group[0]):
                group.pop(0)
            if len(picked) >= limit:
                break
            if group:
                candidate = group.pop(0)
                take(candidate)
                picked.append(candidate)
                progressed = True
        if not progressed:
            break
    return picked


def parse_timeframe(text: str) -> Optional[int]:
    lowered = text.lower()
    found = []
    for name, days in TIMEFRAME.items():
        if re.search(r"\b" + re.escape(name) + r"\b", lowered):
            found.append(days)
    for match in DAYS_RE.finditer(text):
        found.append(int(match.group(1)))
    return max(found) if found else None


def _store_underlyings(store: SnapshotStore) -> list[str]:
    return store.list_underlyings()


def candidate_symbols(text: str) -> list[str]:
    """Ticker-like tokens named in the query, independent of the store.

    All-caps words plus the word after for/on, minus query vocabulary.
    """
    found: list[tuple[int, str]] = []
    for match in TICKER_RE.finditer(text):
        word = match.group(1)
        if word not in NON_TICKER_WORDS:
            found.append((match.start(), word))
    for match in FOR_TICKER_RE.finditer(text):
        word = match.group(1).upper()
        if word not in NON_TICKER_WORDS:
            found.append((match.start(1), word))
    found.sort(key=lambda hit: hit[0])
    ordered = []
    for _, symbol in found:
        if symbol not in ordered:
            ordered.append(symbol)
    return ordered


def unknown_symbols(text: str, store: SnapshotStore) -> list[str]:
    available = set(store.list_underlyings())
    return [symbol for symbol in candidate_symbols(text) if symbol not in available]


def symbols_for(text: str, store: SnapshotStore, default: str) -> list[str]:
    hits = []
    for underlying in _store_underlyings(store):
        match = re.search(
            r"(?<![A-Za-z0-9_])" + re.escape(underlying) + r"(?![A-Za-z0-9_])",
            text,
            re.IGNORECASE,
        )
        if match:
            hits.append((match.start(), underlying))
    if hits:
        hits.sort(key=lambda hit: hit[0])
        ordered = []
        for _, underlying in hits:
            if underlying not in ordered:
                ordered.append(underlying)
        return ordered
    if broad_scan_requested(text):
        stored = _store_underlyings(store)
        if stored:
            return stored
    candidates = candidate_symbols(text)
    if candidates:
        return candidates
    return [default]


def _days_to_expiry(expiry: str, ref: datetime) -> Optional[int]:
    try:
        parsed = datetime.fromisoformat(expiry)
    except ValueError:
        return None
    return (parsed.date() - ref.date()).days


def resolve_direction(bar: Optional[Bar], flip: Optional[float], total_exposure: float) -> str:
    """Single direction rule shared by the brief and the ideas screener.

    Spot versus flip when both exist, otherwise the regime sign, flat only
    when total exposure is exactly zero.
    """
    if flip is not None and bar is not None:
        return "long" if bar.close > flip else "short"
    if total_exposure > 0:
        return "long"
    if total_exposure < 0:
        return "short"
    return "flat"


def spread_pct(contract: OptionContract) -> Optional[float]:
    if contract.bid is None or contract.ask is None or not contract.price:
        return None
    return (contract.ask - contract.bid) / contract.price * 100.0


def quality_bonus(
    contract: OptionContract,
    direction: str,
    pct: Optional[float],
    days: Optional[int],
    iv_rank_value: Optional[float] = None,
) -> float:
    """Small data-driven adjustments that only apply when real quotes exist.

    Fixtures without bid/ask/delta score exactly as before, so offline
    expectations stay stable while live chains get liquidity, alignment,
    tenor, and IV-rank refinement.
    """
    if pct is None and contract.delta is None and iv_rank_value is None and days is None:
        return 0.0
    has_quote = pct is not None
    bonus = 0.0
    if contract.delta is not None:
        if direction == "long":
            if contract.call_or_put == "call" and contract.delta > 0:
                bonus += 0.04
            elif contract.call_or_put == "put":
                bonus -= 0.03
            elif contract.delta <= 0:
                bonus -= 0.03
        elif direction == "short":
            if contract.call_or_put == "put" and contract.delta < 0:
                bonus += 0.04
            elif contract.call_or_put == "call":
                bonus -= 0.03
            elif contract.delta >= 0:
                bonus -= 0.03
    if has_quote:
        assert pct is not None
        if pct <= 2.0:
            bonus += 0.03
        if days is not None:
            if 7 <= days <= 45:
                bonus += 0.02
            elif days < 3 or days > 90:
                bonus -= 0.03
        if iv_rank_value is not None:
            if iv_rank_value >= 30.0:
                bonus += 0.03
            elif iv_rank_value <= 15.0:
                bonus -= 0.03
    return bonus


def _contract_index(
    snapshot: ChainSnapshot,
) -> tuple[dict[tuple[str, str], list[float]], dict[tuple[str, str, float], OptionContract]]:
    strikes: dict[tuple[str, str], list[float]] = {}
    by_key: dict[tuple[str, str, float], OptionContract] = {}
    for contract in snapshot.contracts_list:
        key = (contract.expiry, contract.call_or_put)
        strikes.setdefault(key, []).append(contract.strike)
        by_key[(contract.expiry, contract.call_or_put, contract.strike)] = contract
    return {key: sorted(set(values)) for key, values in strikes.items()}, by_key


def _hedge_strike(anchor: OptionContract, strikes: list[float]) -> Optional[float]:
    if anchor.call_or_put == "call":
        above = [strike for strike in strikes if strike > anchor.strike]
        return above[0] if above else None
    below = [strike for strike in strikes if strike < anchor.strike]
    return below[-1] if below else None


def _single_numbers(
    contract: OptionContract,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    per_share = contract.ask if contract.ask is not None else contract.price
    if per_share is None or per_share <= 0:
        return None, None, None
    net = per_share * DEFAULT_CONTRACT_MULTIPLIER
    if contract.call_or_put == "call":
        breakeven = contract.strike + per_share
    else:
        breakeven = contract.strike - per_share
    return net, net, breakeven


def _vertical_numbers(
    anchor: OptionContract, hedge: OptionContract
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if anchor.ask is None or hedge.bid is None:
        return None, None, None
    width = abs(hedge.strike - anchor.strike) * DEFAULT_CONTRACT_MULTIPLIER
    net = (anchor.ask - hedge.bid) * DEFAULT_CONTRACT_MULTIPLIER
    if net > 0:
        max_loss = min(net, width)
        if anchor.call_or_put == "call":
            breakeven = anchor.strike + net / DEFAULT_CONTRACT_MULTIPLIER
        else:
            breakeven = anchor.strike - net / DEFAULT_CONTRACT_MULTIPLIER
        return net, max_loss, breakeven
    if net < 0:
        credit = -net
        max_loss = max(0.0, width - credit)
        if anchor.call_or_put == "call":
            breakeven = hedge.strike + credit / DEFAULT_CONTRACT_MULTIPLIER
        else:
            breakeven = hedge.strike - credit / DEFAULT_CONTRACT_MULTIPLIER
        return net, max_loss, breakeven
    return None, None, None


def rank_ideas(
    entries: Sequence[tuple[str, ChainSnapshot, GexProfile, Optional[Bar]]],
    max_days: Optional[int] = None,
    now: Optional[datetime] = None,
    min_open_interest: float = MIN_OPEN_INTEREST,
    max_spread_pct: float = MAX_SPREAD_PCT,
    iv_history: Optional[Mapping[str, Sequence[float]]] = None,
    earnings: Optional[Mapping[str, date]] = None,
) -> list[TradeIdea]:
    indexes: dict[str, tuple[dict[tuple[str, str], list[float]], dict[tuple[str, str, float], OptionContract]]] = {}
    candidates = []
    for snapshot_id, snapshot, profile, bar in entries:
        if snapshot_id not in indexes:
            indexes[snapshot_id] = _contract_index(snapshot)
        ref = now if now is not None else snapshot.timestamp_utc
        direction = resolve_direction(bar, profile.flip_point, profile.total_exposure)
        if direction == "flat":
            continue
        for contract in snapshot.contracts_list:
            if contract.gamma is None or contract.open_interest < min_open_interest:
                continue
            pct = spread_pct(contract)
            if pct is not None and pct > max_spread_pct:
                continue
            days = _days_to_expiry(contract.expiry, ref)
            if days is None or days < MIN_DAYS_TO_EXPIRY:
                continue
            if max_days is not None and days > max_days:
                continue
            score = abs(contract.gamma * contract.open_interest)
            candidates.append((score, snapshot_id, snapshot, profile, bar, direction, contract, pct))
    if not candidates:
        return []
    top_score = max(score for score, *_ in candidates)
    histories = iv_history or {}
    rank_by_symbol = {symbol: _iv_rank(history) for symbol, history in histories.items()}
    ranked = []
    for score, snapshot_id, snapshot, profile, bar, direction, contract, pct in candidates:
        exposure = profile.per_strike_exposure.get(contract.strike, 0.0)
        regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
        strikes_by_key, contracts_by_key = indexes[snapshot_id]
        hedge_strike = _hedge_strike(contract, strikes_by_key.get((contract.expiry, contract.call_or_put), []))
        hedge = (
            contracts_by_key.get((contract.expiry, contract.call_or_put, hedge_strike))
            if hedge_strike is not None
            else None
        )
        debit, max_loss, breakeven = _single_numbers(contract)
        spread_label = f"single {contract.call_or_put} {contract.strike}"
        if hedge is not None:
            vertical = _vertical_numbers(contract, hedge)
            width = abs(hedge.strike - contract.strike) * DEFAULT_CONTRACT_MULTIPLIER
            if vertical[0] is not None and abs(vertical[0]) <= width:
                debit, max_loss, breakeven = vertical
                legs = even_legs(
                    2.0,
                    [
                        (snapshot.underlying, "buy", contract.strike, contract.expiry, contract.call_or_put),
                        (snapshot.underlying, "sell", hedge.strike, hedge.expiry, hedge.call_or_put),
                    ],
                )
                low, high = sorted([legs[0].strike or 0.0, legs[1].strike or 0.0])
                spread_label = f"{low:g}/{high:g} {contract.call_or_put} vertical"
            else:
                hedge_strike = None
        confidence = 0.30 + 0.45 * (score / top_score if top_score > 0 else 0.0)
        if pct is not None and pct <= TIGHT_SPREAD_PCT:
            confidence += 0.10
        if max_loss is not None:
            confidence += 0.05
        days = _days_to_expiry(contract.expiry, now if now is not None else snapshot.timestamp_utc)
        confidence += quality_bonus(
            contract, direction, pct, days, rank_by_symbol.get(snapshot.underlying)
        )
        confidence = min(0.9, max(0.05, confidence))
        earnings_map = earnings or {}
        ref = now if now is not None else snapshot.timestamp_utc
        earnings_day = earnings_map.get(snapshot.underlying)
        event_risk = None
        if earnings_day is not None and spans_earnings(ref, contract.expiry, earnings_day):
            event_risk = earnings_day.isoformat()
        if event_risk is not None:
            confidence = max(EARNINGS_CONFIDENCE_FLOOR, confidence - EARNINGS_CONFIDENCE_HAIRCUT)
        ranked.append(
            (
                confidence,
                score,
                snapshot_id,
                snapshot,
                profile,
                bar,
                direction,
                contract,
                pct,
                exposure,
                regime,
                hedge_strike,
                spread_label,
                debit,
                max_loss,
                breakeven,
                event_risk,
            )
        )
    ranked.sort(
        key=lambda c: (-c[0], -c[1], c[3].underlying, c[7].strike, c[7].expiry, c[7].call_or_put)
    )
    picked = diversify(
        ranked,
        ideas_limit(),
        symbol_of=lambda row: row[3].underlying,
        expiry_of=lambda row: row[7].expiry,
        strike_of=lambda row: row[7].strike,
        direction_of=lambda row: row[6],
    )
    ideas: list[TradeIdea] = []
    for row in picked:
        (
            confidence,
            _score,
            snapshot_id,
            snapshot,
            profile,
            bar,
            direction,
            contract,
            pct,
            exposure,
            regime,
            hedge_strike,
            spread_label,
            debit,
            max_loss,
            breakeven,
            event_risk,
        ) = row
        if profile.flip_point is not None:
            where = "above" if direction == "long" else "below"
            trigger = f"enter on spot holding {where} flip {profile.flip_point:.1f}"
        else:
            trigger = f"enter on spot confirming {direction} ({regime}, no flip in chain)"
        thesis = (
            f"{snapshot.underlying} {direction} via {spread_label} {contract.expiry}: "
            f"wall exposure {compact_dollars(exposure)} at {contract.strike}, {regime}"
        )
        if max_loss is not None and breakeven is not None:
            thesis += f", max loss {compact_dollars(max_loss)}, breakeven {breakeven:.1f}"
        rank = rank_by_symbol.get(snapshot.underlying)
        ideas.append(
            TradeIdea(
                symbol=snapshot.underlying,
                direction=direction,
                thesis=thesis,
                trigger=trigger,
                strike=contract.strike,
                expiry=contract.expiry,
                call_or_put=contract.call_or_put,
                confidence=min(0.9, confidence),
                snapshot_id=snapshot_id,
                hedge_strike=hedge_strike,
                spread=spread_label,
                max_loss=max_loss,
                breakeven=breakeven,
                net_debit=debit,
                spread_pct=pct,
                earnings_date=event_risk,
                iv_rank=rank,
            )
        )
    return ideas


def format_ideas(ideas: Sequence[TradeIdea]) -> str:
    ideas = list(ideas)
    if not ideas:
        return "trade ideas: none found"
    lines = [f"trade ideas ({len(ideas)}):"]
    for number, idea in enumerate(ideas, 1):
        line = (
            f"{number}. {idea.symbol} {idea.direction} {idea.spread or idea.call_or_put} "
            f"expiry {idea.expiry} confidence {idea.confidence:.2f}: {idea.thesis} | {idea.trigger}"
        )
        if idea.earnings_date is not None:
            line += f" | earnings {idea.earnings_date}"
        if idea.iv_rank is not None:
            line += f" | iv rank {idea.iv_rank:.1f}"
        lines.append(line)
    return "\n".join(lines)
