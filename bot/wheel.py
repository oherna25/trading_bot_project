from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Mapping, Optional, Sequence

from bot.ideas import symbols_for, unknown_symbols
from bot.ivrank import histories_for, iv_rank
from bot.journal import Journal
from bot.shapes import Bar, ChainSnapshot, OptionContract, PremiumCandidate
from bot.store import SnapshotStore

WHEEL_MIN_DTE = 7
WHEEL_MAX_DTE = 30
WHEEL_MIN_DELTA = 0.15
WHEEL_MAX_DELTA = 0.30
WHEEL_MIN_PREMIUM_RETURN = 0.01
WHEEL_MIN_OI = 100.0
WHEEL_MAX_SPREAD_PCT = 30.0
WHEEL_MAX_SPREAD_ABS = 0.15
WHEEL_EXPIRY_COUNT = 3
WHEEL_MIN_IV_RANK = 30.0
WHEEL_EARNINGS_BUFFER_DAYS = 2
WHEEL_MAX_RESULTS = 10

DROP_STAGES = (
    "side",
    "expiry",
    "earnings",
    "open_interest",
    "spread",
    "delta",
    "no_bar",
    "premium_return",
    "iv_rank",
)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def premium_return(
    bid: Optional[float], strike: float, spot_or_none: Optional[float], call_or_put: str
) -> Optional[float]:
    if bid is None or bid <= 0:
        return None
    if call_or_put == "call":
        if spot_or_none is None or spot_or_none <= 0:
            return None
        return bid * 100.0 / (spot_or_none * 100.0)
    if strike <= 0:
        return None
    return bid * 100.0 / (strike * 100.0)


def spans_earnings(
    today: date,
    expiry_date: date,
    earnings_date_or_none: Optional[date],
    buffer_days: int = WHEEL_EARNINGS_BUFFER_DAYS,
) -> bool:
    if earnings_date_or_none is None:
        return False
    return today <= earnings_date_or_none <= expiry_date + timedelta(days=buffer_days)


def pick_expiries(
    expiry_strings: Sequence[str],
    ref_date: date | datetime,
    min_dte: int = WHEEL_MIN_DTE,
    max_dte: int = WHEEL_MAX_DTE,
    count: int = WHEEL_EXPIRY_COUNT,
) -> list[str]:
    ref = _as_date(ref_date)
    seen: dict[str, date] = {}
    for raw in expiry_strings:
        try:
            parsed = datetime.fromisoformat(raw).date()
        except ValueError:
            continue
        dte = (parsed - ref).days
        if dte < min_dte or dte > max_dte:
            continue
        seen.setdefault(raw, parsed)
    ordered = sorted(seen.items(), key=lambda item: (0 if item[1].weekday() == 4 else 1, item[1]))
    return [raw for raw, _ in ordered[:count]]


def spread_ok(contract: OptionContract) -> bool:
    if contract.bid is None or contract.bid <= 0:
        return False
    if contract.ask is None:
        return True
    if contract.bid >= 1.00:
        return (contract.ask - contract.bid) / contract.bid * 100.0 <= WHEEL_MAX_SPREAD_PCT
    return contract.ask - contract.bid <= WHEEL_MAX_SPREAD_ABS


def screen_premium(
    entries: Sequence[tuple[str, ChainSnapshot, Optional[Bar]]],
    ref_date: date | datetime,
    iv_history: Optional[Mapping[str, Sequence[float]]] = None,
    earnings: Optional[Mapping[str, date]] = None,
    min_iv_rank: float = WHEEL_MIN_IV_RANK,
) -> tuple[list[PremiumCandidate], dict[str, int]]:
    ref = _as_date(ref_date)
    histories = iv_history or {}
    earnings_map = earnings or {}
    dropped = {stage: 0 for stage in DROP_STAGES}
    survivors: list[PremiumCandidate] = []
    for snapshot_id, snapshot, bar in entries:
        picked = set(pick_expiries({c.expiry for c in snapshot.contracts_list}, ref))
        spot = bar.close if bar is not None else None
        symbol_hits: list[PremiumCandidate] = []
        for contract in snapshot.contracts_list:
            side = contract.call_or_put
            if side not in ("put", "call"):
                dropped["side"] += 1
                continue
            if contract.expiry not in picked:
                dropped["expiry"] += 1
                continue
            expiry_date = datetime.fromisoformat(contract.expiry).date()
            if spans_earnings(ref, expiry_date, earnings_map.get(snapshot.underlying)):
                dropped["earnings"] += 1
                continue
            if contract.open_interest < WHEEL_MIN_OI:
                dropped["open_interest"] += 1
                continue
            if not spread_ok(contract):
                dropped["spread"] += 1
                continue
            delta = contract.delta
            if (
                delta is None
                or not (WHEEL_MIN_DELTA <= abs(delta) <= WHEEL_MAX_DELTA)
                or (side == "put" and delta >= 0)
                or (side == "call" and delta <= 0)
            ):
                dropped["delta"] += 1
                continue
            if side == "call" and bar is None:
                dropped["no_bar"] += 1
                continue
            ret = premium_return(contract.bid, contract.strike, spot, side)
            if ret is None or ret < WHEEL_MIN_PREMIUM_RETURN:
                dropped["premium_return"] += 1
                continue
            symbol_hits.append(
                PremiumCandidate(
                    symbol=snapshot.underlying,
                    action="SELL_PUT" if side == "put" else "SELL_CALL",
                    strike=contract.strike,
                    expiry=contract.expiry,
                    dte=(expiry_date - ref).days,
                    bid=contract.bid,
                    ask=contract.ask,
                    delta=delta,
                    iv=contract.iv,
                    open_interest=contract.open_interest,
                    premium_return=ret,
                    capital_at_risk=contract.strike * 100.0 if side == "put" else spot * 100.0,
                    needs_shares=side == "call",
                    iv_rank=None,
                    snapshot_id=snapshot_id,
                )
            )
        rank = iv_rank(histories.get(snapshot.underlying, ()))
        if rank is not None and rank < min_iv_rank:
            dropped["iv_rank"] += len(symbol_hits)
            continue
        survivors.extend(replace(hit, iv_rank=rank) for hit in symbol_hits)
    survivors.sort(key=lambda c: (-c.premium_return, c.symbol, c.strike, c.expiry, c.action))
    return survivors[:WHEEL_MAX_RESULTS], dropped


def format_wheel(candidates: Sequence[PremiumCandidate]) -> str:
    candidates = list(candidates)
    if not candidates:
        return "wheel ideas: none found"
    lines = [f"wheel ideas ({len(candidates)}):"]
    for number, candidate in enumerate(candidates, 1):
        quote = f"bid {candidate.bid:.2f}"
        if candidate.ask is not None:
            quote += f" ask {candidate.ask:.2f}"
        parts = [
            f"{number}. {candidate.symbol} {candidate.action}",
            f"strike {candidate.strike:g}",
            f"expiry {candidate.expiry}",
            f"dte {candidate.dte}",
            f"delta {candidate.delta:.2f}",
            quote,
            f"return {candidate.premium_return * 100.0:.2f}%",
            f"capital ${candidate.capital_at_risk:,.0f}",
        ]
        if candidate.needs_shares:
            parts.append("needs 100 shares")
        if candidate.iv_rank is not None:
            parts.append(f"iv rank {candidate.iv_rank:.1f}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def wheel_response(
    text: str,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    earnings: Optional[Mapping[str, date]] = None,
) -> str:
    unknown = unknown_symbols(text, store)
    if unknown:
        missing = ", ".join(unknown)
        available = ", ".join(store.list_underlyings()) or "none"
        raise ValueError(
            f"no snapshot stored for {missing} (store has: {available}); "
            f"ingest it first, e.g. run_session.py --underlying {unknown[0]}"
        )
    entries: list[tuple[str, ChainSnapshot, Optional[Bar]]] = []
    used: list[str] = []
    snapshot_ids: list[str] = []
    for symbol in symbols_for(text, store, underlying):
        found = store.read_latest(symbol)
        if found is None:
            continue
        snapshot_id, snapshot = found
        entries.append((snapshot_id, snapshot, store.read_latest_bar(symbol)))
        used.append(symbol)
        snapshot_ids.append(snapshot_id)
    if not entries:
        raise ValueError(f"no snapshot stored for {underlying}")
    candidates, dropped = screen_premium(
        entries, now, iv_history=histories_for(store, used), earnings=earnings
    )
    day_iso = now.date().isoformat()
    for symbol in used:
        ivs = [c.iv for c in candidates if c.symbol == symbol and c.iv is not None]
        if not ivs:
            continue
        average = sum(ivs) / len(ivs)
        store.save_iv(symbol, day_iso, average)
        journal.append(
            now, "iv_recorded", {"symbol": symbol, "date": day_iso, "iv": average, "points": len(ivs)}
        )
    counts = {symbol: sum(1 for c in candidates if c.symbol == symbol) for symbol in used}
    journal.append(
        now,
        "wheel_screened",
        {"symbols": used, "counts": counts, "dropped": dropped, "snapshot_ids": snapshot_ids},
    )
    return format_wheel(candidates)
