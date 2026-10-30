from __future__ import annotations

from typing import Mapping, Sequence

MIN_IV_POINTS = 20


def iv_rank(values: Sequence[float]) -> float | None:
    points = list(values)
    if len(points) < MIN_IV_POINTS:
        return None
    current = points[-1]
    low, high = min(points), max(points)
    if high == low:
        return 50.0
    return round((current - low) / (high - low) * 100.0, 1)


def iv_percentile(values: Sequence[float]) -> float | None:
    points = list(values)
    if len(points) < MIN_IV_POINTS:
        return None
    current = points[-1]
    below = sum(1 for point in points if point < current)
    return round(below / len(points) * 100.0, 1)


def history_values(history: Mapping[str, float]) -> list[float]:
    return [history[day] for day in sorted(history)]


def histories_for(store, symbols: Sequence[str]) -> dict[str, list[float]]:
    return {symbol: history_values(store.read_iv_history(symbol)) for symbol in symbols}


def backfill_iv_history(store, journal, now) -> str:
    added = 0
    skipped = 0
    without_iv = 0
    filled_symbols = 0
    for symbol in sorted(store.list_underlyings()):
        history = store.read_iv_history(symbol)
        samples = 0
        first = None
        last = None
        for _, snapshot in store.snapshots_in_order():
            if snapshot.underlying != symbol:
                continue
            key = snapshot.timestamp_utc.isoformat()
            if key in history:
                skipped += 1
                continue
            ivs = [c.iv for c in snapshot.contracts_list if c.iv is not None]
            if not ivs:
                without_iv += 1
                continue
            average = sum(ivs) / len(ivs)
            store.save_iv(symbol, key, average)
            history[key] = average
            samples += 1
            first = key if first is None else min(first, key)
            last = key if last is None else max(last, key)
        if samples:
            filled_symbols += 1
            added += samples
            journal.append(
                now, "iv_backfilled",
                {"symbol": symbol, "samples": samples, "first": first, "last": last},
            )
    rankable = [
        symbol
        for symbol in sorted(store.list_underlyings())
        if len(store.read_iv_history(symbol)) >= MIN_IV_POINTS
    ]
    if added == 0:
        return (
            f"backfilled IV: no new samples "
            f"({without_iv} snapshots without priced IV, {skipped} already recorded)"
        )
    ranked = ", ".join(rankable) if rankable else "none yet"
    return (
        f"backfilled IV: {added} samples across {filled_symbols} symbols "
        f"({skipped} already recorded); rankable: {ranked}"
    )
