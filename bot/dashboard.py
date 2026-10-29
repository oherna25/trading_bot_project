from __future__ import annotations

import html
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from typing import Any, Optional

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.shapes import (
    Bar,
    ChainSnapshot,
    GexProfile,
    JournalEntry,
    OptionContract,
    Position,
    Signal,
)
from bot.store import SnapshotStore


@dataclass(frozen=True)
class HeaderView:
    rank: int
    scan_name: str
    symbol: str
    company: str
    status: str
    price: Optional[float]
    day_change: Optional[float]


@dataclass(frozen=True)
class AlignmentView:
    aligned_count: int
    checklist_total: int
    coverage_pct: float


@dataclass(frozen=True)
class ConvictionView:
    conviction: float
    trigger: str
    confirmation_score: float
    confirmation_band: str
    entry_band: str


@dataclass(frozen=True)
class OptionsView:
    qualifying_count: int
    regime: str


@dataclass(frozen=True)
class TopContractView:
    header: str
    delta: Optional[float]
    open_interest: float
    spread_pct: Optional[float]
    crossed: Optional[bool]
    iv: Optional[float]
    quality: str
    explainer: str


@dataclass(frozen=True)
class DashboardScreen:
    header: HeaderView
    strip: AlignmentView
    grid: ConvictionView
    options: OptionsView
    card: Optional[TopContractView]
    positions: tuple[Position, ...]
    journal_view: tuple[JournalEntry, ...]


def build_header(signal: Signal, snapshot: ChainSnapshot, bars: list[Bar]) -> HeaderView:
    price = bars[-1].close if bars else None
    if len(bars) >= 2:
        day_change = bars[-1].close - bars[-2].close
    elif bars:
        day_change = bars[-1].close - bars[-1].open
    else:
        day_change = None
    return HeaderView(
        rank=1,
        scan_name=f"{snapshot.underlying} scan",
        symbol=snapshot.underlying,
        company=snapshot.underlying,
        status=signal.status,
        price=price,
        day_change=day_change,
    )


def build_alignment(signal: Signal, snapshot: ChainSnapshot) -> AlignmentView:
    _ = snapshot
    return AlignmentView(
        aligned_count=len(signal.checklist),
        checklist_total=len(signal.checklist),
        coverage_pct=signal.coverage * 100.0,
    )


def build_conviction(signal: Signal, bar: Optional[Bar]) -> ConvictionView:
    _ = bar
    score = len(signal.checklist) / 3.0
    band = "strong" if score >= 1.0 else ("mixed" if score >= 0.5 else "weak")
    return ConvictionView(
        conviction=signal.confidence,
        trigger=signal.trigger,
        confirmation_score=score,
        confirmation_band=band,
        entry_band=signal.entry_band,
    )


MAX_SPOT_DISTANCE_PCT = 20.0
MAX_QUALIFYING_SPREAD_PCT = 50.0


def _quote_spread_pct(contract: OptionContract) -> Optional[float]:
    if contract.bid is None or contract.ask is None or not contract.price:
        return None
    return (contract.ask - contract.bid) / contract.price * 100.0


def _qualifies(contract: OptionContract, spot: Optional[float] = None) -> bool:
    if contract.open_interest <= 0:
        return False
    pct = _quote_spread_pct(contract)
    if pct is not None and pct > MAX_QUALIFYING_SPREAD_PCT:
        return False
    if spot:
        return abs(contract.strike - spot) / spot * 100.0 <= MAX_SPOT_DISTANCE_PCT
    return True


def build_options(
    snapshot: ChainSnapshot, profile: GexProfile, spot: Optional[float] = None
) -> OptionsView:
    qualifying = [c for c in snapshot.contracts_list if _qualifies(c, spot)]
    return OptionsView(
        qualifying_count=len(qualifying),
        regime="long-gamma" if profile.total_exposure >= 0.0 else "short-gamma",
    )


def build_top_contract(
    snapshot: ChainSnapshot, signal: Signal, spot: Optional[float] = None
) -> Optional[TopContractView]:
    qualifying = [c for c in snapshot.contracts_list if _qualifies(c, spot)]
    if not qualifying:
        return None
    top = max(qualifying, key=lambda c: c.open_interest)
    if top.bid is not None and top.ask is not None and top.price:
        spread_pct = (top.ask - top.bid) / top.price * 100.0
        crossed = top.bid >= top.ask
    else:
        spread_pct = None
        crossed = None
    quality = "tight" if spread_pct is not None and spread_pct < 5.0 else ("wide" if spread_pct is not None else "unknown")
    return TopContractView(
        header=f"{top.strike} {top.expiry} {top.call_or_put}",
        delta=top.delta,
        open_interest=top.open_interest,
        spread_pct=spread_pct,
        crossed=crossed,
        iv=top.iv,
        quality=quality,
        explainer=signal.thesis,
    )


def dashboard_view(
    snapshot_id: str,
    signal: Signal,
    store: SnapshotStore,
    journal: Journal,
    positions: list[Position],
) -> DashboardScreen:
    snapshot = store.read_snapshot(snapshot_id)
    if snapshot is None:
        raise ValueError(f"unknown snapshot {snapshot_id}")
    profile = compute_gex(snapshot, snapshot_id)
    bars = store.read_bars(snapshot.underlying)
    bar = bars[-1] if bars else None
    spot = bar.close if bar is not None else None
    return DashboardScreen(
        header=build_header(signal, snapshot, bars),
        strip=build_alignment(signal, snapshot),
        grid=build_conviction(signal, bar),
        options=build_options(snapshot, profile, spot),
        card=build_top_contract(snapshot, signal, spot),
        positions=tuple(positions),
        journal_view=tuple(journal.entries()),
    )


JOURNAL_ROWS_SHOWN = 50


def _to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: _to_json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    return str(value)


def screen_to_json(screen: DashboardScreen) -> dict:
    return {
        "header": _to_json_value(screen.header),
        "strip": _to_json_value(screen.strip),
        "grid": _to_json_value(screen.grid),
        "options": _to_json_value(screen.options),
        "card": _to_json_value(screen.card),
        "positions": _to_json_value(screen.positions),
        "journal_view": _to_json_value(screen.journal_view),
    }


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def dashboard_html(screen: DashboardScreen) -> str:
    header = screen.header
    strip = screen.strip
    grid = screen.grid
    options = screen.options
    rows = []
    rows.append("<!doctype html>")
    rows.append(f"<html><head><meta charset=\"utf-8\"><title>{_esc(header.symbol)} dashboard</title></head><body>")
    rows.append(
        f"<h1>{_esc(header.scan_name)} #{header.rank} {_esc(header.symbol)} {_esc(header.status)}</h1>"
    )
    rows.append(f"<p>price {_esc(header.price)} day change {_esc(header.day_change)}</p>")
    rows.append(
        f"<p>aligned {strip.aligned_count}/{strip.checklist_total} coverage {_esc(strip.coverage_pct)}</p>"
    )
    rows.append(
        f"<p>conviction {_esc(grid.conviction)} trigger {_esc(grid.trigger)} "
        f"confirmation {_esc(grid.confirmation_score)} ({_esc(grid.confirmation_band)}) "
        f"entry {_esc(grid.entry_band)}</p>"
    )
    rows.append(
        f"<p>options {options.qualifying_count} qualifying regime {_esc(options.regime)}</p>"
    )
    if screen.card is not None:
        card = screen.card
        rows.append(
            f"<p>top {_esc(card.header)} delta {_esc(card.delta)} oi {_esc(card.open_interest)} "
            f"spread {_esc(card.spread_pct)} crossed {_esc(card.crossed)} iv {_esc(card.iv)} "
            f"{_esc(card.quality)} {_esc(card.explainer)}</p>"
        )
    rows.append("<table><tr><th>symbol</th><th>size</th><th>avg</th><th>pnl</th></tr>")
    for position in screen.positions:
        rows.append(
            f"<tr><td>{_esc(position.symbol)}</td><td>{_esc(position.net_size)}</td>"
            f"<td>{_esc(position.average_price)}</td><td>{_esc(position.unrealized_pnl)}</td></tr>"
        )
    rows.append("</table>")
    rows.append("<table><tr><th>time</th><th>kind</th><th>payload</th></tr>")
    for entry in screen.journal_view[-JOURNAL_ROWS_SHOWN:]:
        rows.append(
            f"<tr><td>{_esc(entry.timestamp_utc.isoformat())}</td><td>{_esc(entry.kind)}</td>"
            f"<td>{_esc(entry.payload)}</td></tr>"
        )
    rows.append("</table></body></html>")
    return "".join(rows)
