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
