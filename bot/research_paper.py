"""Paper trade execution and PnL reporting."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional

from bot.journal import Journal
from bot.risk import build_intent, realized_pnl, review_intent, submit_paper, update_position
from bot.shapes import (
    Bar,
    Fill,
    OrderIntent,
    Position,
    RiskLimits,
    RiskState,
    Signal,
)
from bot.store import SnapshotStore, need_snapshot_text
from bot.tradecards import PROFILE_REQUIRED

PAPER_TRADE_STATE = RiskState(
    account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0, halt_active=False, kill_active=False
)
PAPER_TRADE_LIMITS = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)


def open_trade_size(journal: Journal, symbol: str) -> float:
    net = 0.0
    for entry in journal.entries():
        if entry.kind != "fill" or entry.payload.get("symbol") != symbol:
            continue
        if entry.payload.get("venue", "paper") == "replay":
            continue
        size = entry.payload.get("size")
        if size is None:
            continue
        net += float(size) if entry.payload.get("side", "buy") == "buy" else -float(size)
    return net


def paper_state_from_journal(journal: Journal, symbol: str, mark: float, now: datetime) -> RiskState:
    positions: dict[str, Optional[Position]] = {}
    day_pnl = 0.0
    for entry in journal.entries():
        if entry.kind != "fill" or entry.payload.get("venue", "paper") == "replay":
            continue
        fill_symbol = entry.payload.get("symbol")
        price = entry.payload.get("price")
        size = entry.payload.get("size")
        if not fill_symbol or price is None or size is None:
            continue
        intent = OrderIntent(
            symbol=str(fill_symbol),
            side=str(entry.payload.get("side", "buy")),
            size=float(size),
            risk_numbers={},
            signal_id="journal-replay",
            legs=(),
        )
        fill = Fill(price=float(price), size=float(size), timestamp_utc=entry.timestamp_utc, venue="paper")
        old = positions.get(str(fill_symbol))
        if entry.timestamp_utc.date() == now.date():
            day_pnl += realized_pnl(old, intent, fill)
            day_pnl -= float(entry.payload.get("fees") or 0.0)
        positions[str(fill_symbol)] = update_position(old, intent, fill, float(price))
    current = positions.get(symbol)
    if current is not None and current.net_size != 0:
        day_pnl += (mark - current.average_price) * current.net_size
    return replace(PAPER_TRADE_STATE, day_pnl=day_pnl)


def paper_trade_step(
    symbol: str,
    signal: Signal,
    bar: Optional[Bar],
    journal: Journal,
    now: datetime,
) -> str:
    if bar is None:
        journal.append(now, "trade_skipped", {"underlying": symbol, "reason": "no bar"})
        return ""
    if open_trade_size(journal, symbol) != 0:
        journal.append(now, "trade_skipped", {"underlying": symbol, "reason": "position open"})
        return ""
    state = paper_state_from_journal(journal, symbol, bar.close, now)
    intent = build_intent(signal, f"live-{signal.snapshot_id}", state, journal, now)
    if intent is None:
        return ""
    if review_intent(intent, state, PAPER_TRADE_LIMITS, journal, now) != "approved":
        return ""
    fill, _, _ = submit_paper(intent, bar.close, state, None, PAPER_TRADE_LIMITS, journal, now)
    return f"paper trade: {intent.side} {intent.size:.0f} {symbol} @ {fill.price:.2f}"


def take_card_trade(
    index: int,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
) -> str:
    from bot.demo import demo_cards

    if store.read_profile() is None:
        return PROFILE_REQUIRED + " -- next: POST /profile with risk_tolerance, portfolio_size, knowledge, options_ok"
    if snapshot_id is None:
        latest = store.read_latest(underlying)
        if latest is None:
            raise ValueError(need_snapshot_text(underlying, store))
        snapshot_id = latest[0]
    _, cards = demo_cards(underlying, store, journal, now, snapshot_id)
    if not cards:
        raise ValueError(
            f"no cards for {underlying} snapshot {snapshot_id} "
            f"-- next: GET /cards?underlying={underlying} to see what is stored"
        )
    if index < 0 or index >= len(cards):
        raise ValueError(
            f"index {index} out of range (0-{len(cards) - 1}) "
            f"-- next: pick an index from GET /cards?underlying={underlying}"
        )
    card = cards[index]
    if card.direction not in ("long", "short"):
        return (
            f"card {index} for {card.symbol} is flat "
            f"-- next: pick another index from GET /cards?underlying={underlying}"
        )
    bar = store.read_latest_bar(card.symbol)
    if bar is None:
        return f"no stored bar for {card.symbol} -- next: say load {card.symbol} and I'll fetch it"
    signal = Signal(
        direction=card.direction, thesis=card.reason,
        confidence=card.confidence, snapshot_id=card.snapshot_id,
    )
    note = paper_trade_step(card.symbol, signal, bar, journal, now)
    if note:
        for entry in reversed(journal.entries()):
            if entry.kind == "fill" and entry.payload.get("symbol") == card.symbol:
                entry.payload["card_snapshot"] = card.snapshot_id
                entry.payload["card_index"] = index
                break
        return note
    reason = journal.entries()[-1].payload.get("reason", "vetoed") if journal.entries() else "vetoed"
    if reason == "position open":
        return (
            f"already holding {card.symbol} "
            f"-- next: close it via portfolio_command 'close {card.symbol}' or take another card"
        )
    return f"paper trade for {card.symbol} skipped ({reason}) -- next: check paper_pnl and retry"


def _fill_books(journal: Journal) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    live_net: dict[str, float] = {}
    live_cost: dict[str, float] = {}
    replay_net: dict[str, float] = {}
    replay_cost: dict[str, float] = {}
    for entry in journal.entries():
        if entry.kind != "fill":
            continue
        symbol = entry.payload.get("symbol")
        price = entry.payload.get("price")
        size = entry.payload.get("size")
        if not symbol or price is None or size is None:
            continue
        side = entry.payload.get("side", "buy")
        signed = float(size) if side == "buy" else -float(size)
        if entry.payload.get("venue", "paper") == "replay":
            book_net, book_cost = replay_net, replay_cost
        else:
            book_net, book_cost = live_net, live_cost
        old_net = book_net.get(symbol, 0.0)
        old_cost = book_cost.get(symbol, 0.0)
        new_net = old_net + signed
        if old_net == 0.0 or (old_net > 0) == (signed > 0):
            new_cost = old_cost + signed * float(price)
        elif new_net == 0.0:
            new_cost = 0.0
        elif abs(signed) < abs(old_net):
            new_cost = old_cost * (new_net / old_net)
        else:
            new_cost = new_net * float(price)
        book_net[symbol] = new_net
        book_cost[symbol] = new_cost
    return live_net, live_cost, replay_net, replay_cost


def _pnl_lines(
    net: dict[str, float], cost: dict[str, float], store: SnapshotStore, prefix: str
) -> tuple[list[str], float]:
    lines = []
    total = 0.0
    for symbol in sorted(net):
        bar = store.read_latest_bar(symbol)
        if bar is None or net[symbol] == 0:
            continue
        average = cost[symbol] / net[symbol]
        pnl = (bar.close - average) * net[symbol] + 0.0
        total += pnl
        lines.append(f"{prefix}{symbol}: size {net[symbol]:.0f} avg {average:.2f} mark {bar.close:.2f} pnl {pnl:+.2f}")
    return lines, total


def paper_pnl_text(journal: Journal, store: SnapshotStore) -> str:
    live_net, live_cost, replay_net, replay_cost = _fill_books(journal)
    if not live_net and not replay_net:
        return "paper pnl: no fills journaled"
    lines: list[str] = []
    if live_net:
        live_lines, live_total = _pnl_lines(live_net, live_cost, store, "")
        lines.extend(live_lines)
        lines.append(f"total paper pnl: {live_total:+.2f}")
    if replay_net:
        replay_lines, replay_total = _pnl_lines(replay_net, replay_cost, store, "replay ")
        lines.extend(replay_lines)
        lines.append(f"total replay pnl: {replay_total:+.2f}")
    return "\n".join(lines)
