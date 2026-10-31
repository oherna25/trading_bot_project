from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from bot.charts import board_rows
from bot.diff import compact_dollars
from bot.exposure import compute_gex
from bot.holdings import describe_holding
from bot.store import SnapshotStore

EARNINGS_WINDOW_DAYS = 14


def _nearest_wall(symbol: str, store: SnapshotStore) -> tuple[float | None, float | None]:
    found = store.read_latest(symbol)
    if found is None:
        return None, None
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(symbol)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    if not profile.per_strike_exposure:
        return None, None
    if spot is None:
        strike, value = max(profile.per_strike_exposure.items(), key=lambda item: abs(item[1]))
        return strike, value
    strike = min(profile.per_strike_exposure, key=lambda level: abs(level - spot))
    return strike, profile.per_strike_exposure[strike]


def earnings_rows(store: SnapshotStore, now: datetime) -> list[dict[str, Any]]:
    ref = now.date()
    dates = store.all_earnings()
    holdings = {holding.symbol: holding for holding in store.read_holdings()}
    board = {str(row["symbol"]): row for row in board_rows(store)}
    rows = []
    for symbol in sorted(set(store.list_underlyings()) | set(holdings)):
        day = dates.get(symbol)
        if day is None:
            continue
        days_out = (day - ref).days
        if days_out < 0 or days_out > EARNINGS_WINDOW_DAYS:
            continue
        positioned = board.get(symbol)
        strike, wall = _nearest_wall(symbol, store)
        holding = holdings.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "earnings_date": day.isoformat(),
                "days_out": days_out,
                "held": holding is not None,
                "holding": describe_holding(holding) if holding is not None else None,
                "regime": str(positioned["regime"]) if positioned is not None else "unknown",
                "spot": positioned["spot"] if positioned is not None else None,
                "flip": positioned["flip"] if positioned is not None else None,
                "wall_strike": strike,
                "wall_value": wall,
                "wall": compact_dollars(wall) if wall is not None else "none",
            }
        )
    rows.sort(key=lambda row: (row["days_out"], row["symbol"]))
    return rows


def _position_bit(row: dict[str, Any]) -> str:
    if row["holding"] is not None:
        return f"held ({row['holding']})"
    return "not held"


def _row_line(row: dict[str, Any]) -> str:
    spot = row["spot"]
    spot_text = f"{spot:.2f}" if spot is not None else "n/a"
    strike = row["wall_strike"]
    wall_text = f"{strike:g} {row['wall']}" if strike is not None else "none"
    day_word = "day" if row["days_out"] == 1 else "days"
    return (
        f"{row['symbol']} prints {row['earnings_date']} ({row['days_out']} {day_word} out) | "
        f"{row['regime']} | spot {spot_text} | nearest wall {wall_text} | {_position_bit(row)}"
    )


def earnings_text(store: SnapshotStore, now: datetime) -> str:
    rows = earnings_rows(store, now)
    if not rows:
        return (
            f"earnings radar: no prints in the next {EARNINGS_WINDOW_DAYS} days -- "
            "next: set FINNHUB_API_KEY and ask for trade cards to cache print dates"
        )
    head = f"earnings radar ({len(rows)} in {EARNINGS_WINDOW_DAYS} days):"
    return "\n".join([head] + [_row_line(row) for row in rows])


def earnings_html(store: SnapshotStore, now: datetime, underlying: str) -> str:
    from bot.theme import page

    settings = store.read_settings()
    rows = earnings_rows(store, now)
    if not rows:
        body = f"<p>{html.escape(earnings_text(store, now))}</p>"
    else:
        cells = [
            "<table><thead><tr><th>Symbol</th><th>Print</th><th>Days out</th>"
            "<th>Regime</th><th>Spot</th><th>Nearest wall</th><th>Position</th>"
            "</tr></thead><tbody>"
        ]
        for row in rows:
            spot = row["spot"]
            spot_text = f"{spot:.2f}" if spot is not None else "n/a"
            strike = row["wall_strike"]
            wall_text = f"{strike:g} {row['wall']}" if strike is not None else "none"
            cls = "long" if row["regime"] == "long-gamma" else "short"
            cells.append(
                f"<tr><td>{html.escape(str(row['symbol']))}</td>"
                f"<td>{html.escape(str(row['earnings_date']))}</td>"
                f"<td>{row['days_out']} days out</td>"
                f"<td><span class=\"{cls}\">{html.escape(str(row['regime']))}</span></td>"
                f"<td>{spot_text}</td><td>{html.escape(wall_text)}</td>"
                f"<td>{html.escape(_position_bit(row))}</td></tr>"
            )
        cells.append("</tbody></table>")
        body = "".join(cells)
    return page(
        "earnings radar",
        "earnings radar, from stored print dates",
        f"{len(rows)} prints in {EARNINGS_WINDOW_DAYS} days at {now.isoformat()}",
        body,
        underlying,
        "earnings",
        settings,
        "/earnings",
    )
