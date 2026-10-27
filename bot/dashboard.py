from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
    day_change = bars[-1].close - bars[-2].close if len(bars) >= 2 else None
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


def _qualifies(contract: OptionContract) -> bool:
    return contract.open_interest > 0


def build_options(snapshot: ChainSnapshot, profile: GexProfile) -> OptionsView:
    qualifying = [c for c in snapshot.contracts_list if _qualifies(c)]
    return OptionsView(
        qualifying_count=len(qualifying),
        regime="long-gamma" if profile.total_exposure >= 0.0 else "short-gamma",
    )


def build_top_contract(snapshot: ChainSnapshot, signal: Signal) -> Optional[TopContractView]:
    qualifying = [c for c in snapshot.contracts_list if _qualifies(c)]
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
        # OptionContract carries no delta field yet, so the card reports none until the shape grows one.
        delta=None,
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
    return DashboardScreen(
        header=build_header(signal, snapshot, bars),
        strip=build_alignment(signal, snapshot),
        grid=build_conviction(signal, bar),
        options=build_options(snapshot, profile),
        card=build_top_contract(snapshot, signal),
        positions=tuple(positions),
        journal_view=tuple(journal.entries()),
    )
