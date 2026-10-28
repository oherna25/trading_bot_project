from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Sequence

from bot.shapes import (
    DEFAULT_CONTRACT_MULTIPLIER,
    Bar,
    ChainSnapshot,
    GexProfile,
    OptionContract,
    TradeIdea,
)
from bot.store import SnapshotStore

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
    if not hits:
        return [default]
    hits.sort(key=lambda hit: hit[0])
    ordered = []
    for _, underlying in hits:
        if underlying not in ordered:
            ordered.append(underlying)
    return ordered


def _days_to_expiry(expiry: str, ref: datetime) -> Optional[int]:
    try:
        parsed = datetime.fromisoformat(expiry)
    except ValueError:
        return None
    return (parsed.date() - ref.date()).days


def _direction(bar: Optional[Bar], flip: Optional[float]) -> str:
    if flip is None or bar is None:
        return "flat"
    if bar.close > flip:
        return "long"
    return "short"


def _contract_exposure(contract: OptionContract) -> float:
    sign = 1.0 if contract.call_or_put == "call" else -1.0
    return sign * (contract.gamma or 0.0) * contract.open_interest * DEFAULT_CONTRACT_MULTIPLIER


def rank_ideas(
    entries: Sequence[tuple[str, ChainSnapshot, GexProfile, Optional[Bar]]],
    max_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> list[TradeIdea]:
    candidates = []
    for snapshot_id, snapshot, profile, bar in entries:
        ref = now if now is not None else snapshot.timestamp_utc
        direction = _direction(bar, profile.flip_point)
        for contract in snapshot.contracts_list:
            if contract.gamma is None or contract.open_interest <= 0:
                continue
            if max_days is not None:
                days = _days_to_expiry(contract.expiry, ref)
                if days is None or days < 0 or days > max_days:
                    continue
            score = abs(contract.gamma * contract.open_interest)
            candidates.append((score, snapshot_id, snapshot.underlying, profile.flip_point, direction, contract))
    if not candidates:
        return []
    total_interest = sum(contract.open_interest for _, _, _, _, _, contract in candidates)
    candidates.sort(key=lambda c: (-c[0], c[2], c[5].strike, c[5].expiry, c[5].call_or_put))
    ideas = []
    for _, snapshot_id, symbol, flip, direction, contract in candidates[:10]:
        exposure = _contract_exposure(contract)
        confidence = contract.open_interest / total_interest if total_interest > 0 else 0.0
        ideas.append(
            TradeIdea(
                symbol=symbol,
                direction=direction,
                thesis=f"{symbol} {contract.call_or_put} strike {contract.strike} expiry {contract.expiry} exposure {exposure:.1f} flip {flip}",
                trigger=f"trade strike {contract.strike} expiry {contract.expiry} exposure {exposure:.1f} on flip {flip}",
                strike=contract.strike,
                expiry=contract.expiry,
                call_or_put=contract.call_or_put,
                confidence=min(0.9, confidence),
                snapshot_id=snapshot_id,
            )
        )
    return ideas


def format_ideas(ideas: Sequence[TradeIdea]) -> str:
    ideas = list(ideas)
    if not ideas:
        return "trade ideas: none found"
    lines = [f"trade ideas ({len(ideas)}):"]
    for number, idea in enumerate(ideas, 1):
        lines.append(
            f"{number}. {idea.symbol} {idea.direction} {idea.call_or_put} strike {idea.strike} "
            f"expiry {idea.expiry} confidence {idea.confidence:.2f}: {idea.thesis} | {idea.trigger}"
        )
    return "\n".join(lines)
