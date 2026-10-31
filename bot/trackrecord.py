from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import quote as _urlencode

from bot.diff import compact_dollars, signed_dollars
from bot.journal import Journal
from bot.store import SnapshotStore

EQUITY_WIDTH = 920
EQUITY_HEIGHT = 260
LINE_COLOR = "#4493f8"
AXIS_COLOR = "#57606a"


def _paper_fills(journal: Journal) -> list[tuple[datetime, dict[str, Any]]]:
    fills = [
        (entry.timestamp_utc, dict(entry.payload))
        for entry in journal.entries()
        if entry.kind == "fill" and entry.payload.get("venue", "paper") != "replay"
    ]
    fills.sort(key=lambda item: item[0])
    return fills


def closed_trades(journal: Journal) -> list[dict[str, Any]]:
    longs: dict[str, list[dict[str, Any]]] = {}
    shorts: dict[str, list[dict[str, Any]]] = {}
    trades: list[dict[str, Any]] = []
    for stamp, fill in _paper_fills(journal):
        symbol = str(fill.get("symbol", ""))
        price = fill.get("price")
        size = fill.get("size")
        if not symbol or price is None or size is None:
            continue
        qty = abs(float(size))
        if qty <= 0:
            continue
        side = str(fill.get("side", "buy"))
        if side == "buy":
            book = shorts.get(symbol, [])
            closed_qty = 0.0
            cost = 0.0
            entry_time = None
            entry_fill = None
            while book and closed_qty < qty:
                lot = book[0]
                take = min(lot["qty"], qty - closed_qty)
                closed_qty += take
                cost += take * lot["price"]
                lot["qty"] -= take
                if entry_time is None:
                    entry_time = lot["time"]
                    entry_fill = lot["fill"]
                if lot["qty"] <= 0:
                    book.pop(0)
            if closed_qty > 0:
                avg_entry = cost / closed_qty
                trades.append({
                    "symbol": symbol,
                    "qty": closed_qty,
                    "entry_price": avg_entry,
                    "exit_price": float(price),
                    "entry_time": entry_time,
                    "exit_time": stamp,
                    "pnl": (avg_entry - float(price)) * closed_qty,
                    "direction": "short",
                    "entry_fill": entry_fill or {},
                    "exit_fill": fill,
                })
            remainder = qty - closed_qty
            if remainder > 0:
                longs.setdefault(symbol, []).append(
                    {"qty": remainder, "price": float(price), "time": stamp, "fill": fill}
                )
        else:
            book = longs.get(symbol, [])
            closed_qty = 0.0
            cost = 0.0
            entry_time = None
            entry_fill = None
            while book and closed_qty < qty:
                lot = book[0]
                take = min(lot["qty"], qty - closed_qty)
                closed_qty += take
                cost += take * lot["price"]
                lot["qty"] -= take
                if entry_time is None:
                    entry_time = lot["time"]
                    entry_fill = lot["fill"]
                if lot["qty"] <= 0:
                    book.pop(0)
            if closed_qty > 0:
                avg_entry = cost / closed_qty
                trades.append({
                    "symbol": symbol,
                    "qty": closed_qty,
                    "entry_price": avg_entry,
                    "exit_price": float(price),
                    "entry_time": entry_time,
                    "exit_time": stamp,
                    "pnl": (float(price) - avg_entry) * closed_qty,
                    "direction": "long",
                    "entry_fill": entry_fill or {},
                    "exit_fill": fill,
                })
            remainder = qty - closed_qty
            if remainder > 0:
                shorts.setdefault(symbol, []).append(
                    {"qty": remainder, "price": float(price), "time": stamp, "fill": fill}
                )
    return trades


def win_rate(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0.0)) > 0)
    return wins / len(trades)


def _mark_at(store: SnapshotStore, symbol: str, stamp: datetime) -> float | None:
    bars = [bar for bar in store.read_bars(symbol) if bar.timestamp_utc <= stamp]
    if bars:
        return max(bars, key=lambda bar: bar.timestamp_utc).close
    latest = store.read_latest_bar(symbol)
    return latest.close if latest is not None else None


def equity_curve(
    journal: Journal, store: SnapshotStore
) -> list[tuple[datetime, float]]:
    fills = _paper_fills(journal)
    if not fills:
        return []
    longs: dict[str, list[dict[str, Any]]] = {}
    shorts: dict[str, list[dict[str, Any]]] = {}
    realized = 0.0
    points: list[tuple[datetime, float]] = []
    for stamp, fill in fills:
        symbol = str(fill.get("symbol", ""))
        price = fill.get("price")
        size = fill.get("size")
        if not symbol or price is None or size is None:
            continue
        qty = abs(float(size))
        side = str(fill.get("side", "buy"))
        book = shorts.get(symbol, []) if side == "buy" else longs.get(symbol, [])
        closed_qty = 0.0
        cost = 0.0
        while book and closed_qty < qty:
            lot = book[0]
            take = min(lot["qty"], qty - closed_qty)
            closed_qty += take
            cost += take * lot["price"]
            lot["qty"] -= take
            if lot["qty"] <= 0:
                book.pop(0)
        if closed_qty > 0:
            avg_entry = cost / closed_qty
            if side == "buy":
                realized += (avg_entry - float(price)) * closed_qty
            else:
                realized += (float(price) - avg_entry) * closed_qty
        remainder = qty - closed_qty
        if remainder > 0:
            target = longs if side == "buy" else shorts
            target.setdefault(symbol, []).append({"qty": remainder, "price": float(price)})
        unrealized = 0.0
        for sym, lots in list(longs.items()) + list(shorts.items()):
            net = sum(lot["qty"] for lot in lots)
            if net <= 0:
                continue
            avg = sum(lot["qty"] * lot["price"] for lot in lots) / net
            mark = _mark_at(store, sym, stamp)
            if mark is None:
                mark = float(price) if sym == symbol else avg
            if lots is longs.get(sym):
                unrealized += (mark - avg) * net
            else:
                unrealized += (avg - mark) * net
        points.append((stamp, realized + unrealized))
    latest_mark_total = 0.0
    for sym, lots in list(longs.items()) + list(shorts.items()):
        net = sum(lot["qty"] for lot in lots)
        if net <= 0:
            continue
        avg = sum(lot["qty"] * lot["price"] for lot in lots) / net
        latest = store.read_latest_bar(sym)
        mark = latest.close if latest is not None else avg
        if lots is longs.get(sym):
            latest_mark_total += (mark - avg) * net
        else:
            latest_mark_total += (avg - mark) * net
    final_equity = realized + latest_mark_total
    if not points or abs(points[-1][1] - final_equity) > 0.005:
        latest_stamp = points[-1][0]
        for sym in set(list(longs) + list(shorts)):
            latest = store.read_latest_bar(sym)
            if latest is not None and latest.timestamp_utc > latest_stamp:
                latest_stamp = latest.timestamp_utc
        points.append((latest_stamp, final_equity))
    return points


def _card_link(trade: dict[str, Any], store: SnapshotStore) -> str:
    entry = trade.get("entry_fill", {}) or {}
    snapshot = entry.get("card_snapshot")
    index = entry.get("card_index")
    if (
        isinstance(snapshot, str)
        and snapshot
        and isinstance(index, int)
        and index >= 0
        and store.read_snapshot(snapshot) is not None
    ):
        symbol = str(trade.get("symbol", ""))
        return (
            f"/card?underlying={_urlencode(symbol)}"
            f"&snapshot={_urlencode(snapshot)}&index={index}"
        )
    return "/board"


def equity_svg(points: list[tuple[datetime, float]]) -> str:
    width, height = EQUITY_WIDTH, EQUITY_HEIGHT
    left, right, top, bottom = 76, 16, 18, 34
    plot_width = width - left - right
    plot_height = height - top - bottom
    if not points:
        return (
            f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<text x="{left}" y="{top + 20}" font-size="13" fill="{AXIS_COLOR}">'
            "no paper fills yet</text></svg>"
        )
    values = [value for _, value in points]
    low, high = min(values + [0.0]), max(values + [0.0])
    if high == low:
        high = low + 1.0
    span = high - low

    def x_of(i: int) -> float:
        if len(points) == 1:
            return left + plot_width / 2
        return left + i / (len(points) - 1) * plot_width

    def y_of(value: float) -> float:
        return top + (high - value) / span * plot_height

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif" '
        'style="font-variant-numeric:tabular-nums">'
    ]
    for value, label in (
        (high, signed_dollars(high)),
        (0.0, "$0"),
        (low, signed_dollars(low)),
    ):
        y = y_of(value)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{AXIS_COLOR}" stroke-width="{1.5 if value == 0 else 0.5}" opacity="0.5"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="12" '
            f'fill="{AXIS_COLOR}">{label}</text>'
        )
    path = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, (_, v) in enumerate(points))
    parts.append(
        f'<polyline points="{path}" fill="none" stroke="{LINE_COLOR}" stroke-width="2"/>'
    )
    for i in (0, len(points) - 1):
        stamp, value = points[i]
        parts.append(
            f'<circle cx="{x_of(i):.1f}" cy="{y_of(value):.1f}" r="3" fill="{LINE_COLOR}">'
            f"<title>{stamp.isoformat()} {signed_dollars(value)}</title></circle>"
        )
    parts.append("</svg>")
    return "".join(parts)


def trackrecord_text(journal: Journal, store: SnapshotStore) -> str:
    fills = _paper_fills(journal)
    if not fills:
        return (
            "track record: no paper fills journaled "
            "-- next: take a card via take_paper_trade or POST /action"
        )
    trades = closed_trades(journal)
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0.0)) > 0)
    rate = win_rate(trades)
    total = sum(float(trade.get("pnl", 0.0)) for trade in trades)
    points = equity_curve(journal, store)
    equity = points[-1][1] if points else 0.0
    lines = [
        f"track record: {len(trades)} closed trades, win rate {rate:.0%} ({wins}/{len(trades)}), "
        f"realized {signed_dollars(total)}, equity {signed_dollars(equity)}",
    ]
    if not trades:
        lines.append("closed trades: none yet (positions still open)")
        return "\n".join(lines)
    lines.append("closed trades:")
    for trade in trades:
        link = _card_link(trade, store)
        entry = trade["entry_time"]
        exit_time = trade["exit_time"]
        entry_text = entry.strftime("%m-%d %H:%M") if entry is not None else "n/a"
        exit_text = exit_time.strftime("%m-%d %H:%M") if exit_time is not None else "n/a"
        lines.append(
            f"  {trade['symbol']} {trade['direction']} {trade['qty']:g} "
            f"{trade['entry_price']:.2f} -> {trade['exit_price']:.2f} "
            f"pnl {signed_dollars(float(trade['pnl']))} "
            f"{entry_text} to {exit_text} {link}"
        )
    return "\n".join(lines)


def trackrecord_html(
    journal: Journal, store: SnapshotStore, underlying: str, now: datetime
) -> str:
    from bot.theme import page

    settings = store.read_settings()
    fills = _paper_fills(journal)
    trades = closed_trades(journal)
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0.0)) > 0)
    rate = win_rate(trades)
    total = sum(float(trade.get("pnl", 0.0)) for trade in trades)
    points = equity_curve(journal, store)
    equity = points[-1][1] if points else 0.0
    if not fills:
        body = (
            "<p>track record: no paper fills journaled. "
            "Next: take a card via take_paper_trade or POST /action.</p>"
        )
    else:
        stats = (
            "<div class=\"stats\">"
            f"<div class=\"stat\"><b>{len(trades)}</b><span>closed trades</span></div>"
            f"<div class=\"stat\"><b>{rate:.0%}</b><span>win rate ({wins}/{len(trades)})</span></div>"
            f"<div class=\"stat\"><b>{html.escape(signed_dollars(total))}</b>"
            "<span>realized</span></div>"
            f"<div class=\"stat\"><b>{html.escape(signed_dollars(equity))}</b>"
            "<span>equity</span></div>"
            "</div>"
        )
        if trades:
            rows = [
                "<table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th>"
                "<th>Entry</th><th>Exit</th><th>PnL</th><th>Card</th></tr></thead><tbody>"
            ]
            for trade in trades:
                link = _card_link(trade, store)
                label = "card" if link.startswith("/card") else "board"
                pnl = signed_dollars(float(trade["pnl"]))
                cls = "long" if float(trade["pnl"]) > 0 else "short"
                rows.append(
                    f"<tr><td>{html.escape(str(trade['symbol']))}</td>"
                    f"<td>{html.escape(str(trade['direction']))}</td>"
                    f"<td>{trade['qty']:g}</td>"
                    f"<td>{trade['entry_price']:.2f}</td>"
                    f"<td>{trade['exit_price']:.2f}</td>"
                    f"<td class=\"{cls}\">{html.escape(pnl)}</td>"
                    f"<td><a href=\"{html.escape(link, quote=True)}\">{label}</a></td></tr>"
                )
            rows.append("</tbody></table>")
            table = "".join(rows)
        else:
            table = "<p>closed trades: none yet (positions still open)</p>"
        body = (
            f"{stats}<h2>Equity</h2>"
            f"<div class=\"chart-panel\">{equity_svg(points)}</div>"
            f"<h2>Closed trades</h2>{table}"
        )
    return page(
        "track record",
        "track record, from journaled paper fills",
        f"{len(trades)} closed trades at {now.isoformat()}",
        body,
        underlying,
        "trackrecord",
        settings,
        f"/trackrecord?underlying={_urlencode(underlying)}",
    )
