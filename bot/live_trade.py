"""Confidence-gated live execution plus the /live UI helpers.

Safety shape: arming still flows only through ``bot.autonomy.enable_live``
(full evidence plus checklist gates) and every fill still flows through
``bot.autonomy.submit_live`` (armed flag, halt/kill, risk review, journal).
This module adds no new gate bypass: it builds the intent, checks the
operator-selected confidence threshold from ``DashboardSettings``, and keeps
the resulting live ``RiskState`` in process memory next to the memory-only
``live_enabled`` flag (a restart disarms and resets both).

The auto path (``maybe_auto_live``) never raises: gate misses become
``live_skipped``/``live_blocked`` journal receipts. The explicit path
(``take_live_trade``) raises ``ValueError`` with the broker/review reason so
tool and HTTP callers can surface it as a failed reply.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from bot.autonomy import LiveDisabledError, enable_live, kill_switch
from bot.journal import Journal
from bot.shapes import OperatorConfig, OrderIntent, OrderLeg, RiskLimits, RiskState, Signal
from bot.store import SnapshotStore
from bot.venue import LiveConfigError, LiveOrderError

MAX_AUTO_LIVE_SENDS = 3


_lock = threading.RLock()
_live_state: RiskState | None = None
_live_limits: RiskLimits | None = None


def reset_live_memory() -> None:
    """Clear the in-memory live state (tests only; a restart does this)."""
    global _live_state, _live_limits
    with _lock:
        _live_state = None
        _live_limits = None


def live_state_and_limits(
    store: SnapshotStore, config: OperatorConfig
) -> tuple[RiskState, RiskLimits]:
    """Return the process-memory live state, initializing it once per process."""
    global _live_state, _live_limits
    with _lock:
        if _live_state is None or _live_limits is None:
            profile = store.read_profile()
            if profile is not None:
                from bot.profile import suggest_params

                params = suggest_params(profile)
                _live_state = RiskState(
                    account_size=params["account_size"],
                    risk_per_trade=params["risk_per_trade"],
                    day_pnl=0.0,
                    halt_active=False,
                    kill_active=False,
                )
            else:
                _live_state = RiskState(
                    account_size=100000.0,
                    risk_per_trade=0.01,
                    day_pnl=0.0,
                    halt_active=False,
                    kill_active=False,
                )
            _live_limits = RiskLimits(
                max_trade_size=config.max_trade_size,
                day_profit_target=config.day_profit_target,
                max_day_loss=config.max_day_loss,
            )
        return _live_state, _live_limits


def _store_live_result(updated: RiskState, limits: RiskLimits) -> None:
    global _live_state, _live_limits
    with _lock:
        _live_state = updated
        _live_limits = limits


def live_status_text(store: SnapshotStore, journal: Journal, now: datetime) -> str:
    import bot.autonomy as autonomy

    settings = store.read_settings()
    with _lock:
        state = _live_state
    flag = "armed" if autonomy.live_enabled else "off"
    state_bits = (
        f"halt={state.halt_active} kill={state.kill_active} day_pnl={state.day_pnl:.2f}"
        if state is not None
        else "no live session yet this process"
    )
    return (
        f"live ordering is {flag} ({state_bits}) -- "
        f"auto_send={settings.live_auto_send} "
        f"min_confidence={settings.live_min_confidence:.2f}"
    )


def enable_live_ui(
    signature: str,
    checklist: Mapping[str, bool],
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
) -> str:
    """Arm live ordering from validated UI input. Raises ValueError when gated."""
    from bot.replay import paper_evidence_from_journal

    clean_signature = (signature or "").strip()
    if not clean_signature:
        raise ValueError("live enable needs a non-empty operator signature")
    checks = {
        "paper_evidence_reviewed": bool(checklist.get("paper_evidence_reviewed", False)),
        "risk_limits_set": bool(checklist.get("risk_limits_set", False)),
        "kill_switch_tested": bool(checklist.get("kill_switch_tested", False)),
    }
    kinds = {entry.kind for entry in journal.entries()}
    evidence = paper_evidence_from_journal(
        journal,
        halt_drills_passed=("halt" in kinds or "kill_fired" in kinds),
    )
    state, _ = live_state_and_limits(store, config)
    if state.halt_active or state.kill_active:
        raise ValueError("live session is halted; kill-switch reset is required before re-arming")
    armed = enable_live(clean_signature, state, journal, now, evidence, checks)
    if not armed:
        reason = journal.entries()[-1].payload.get("reason", "gated") if journal.entries() else "gated"
        raise ValueError(f"live enable blocked ({reason})")
    with _lock:
        global _live_state
        _live_state = RiskState(
            account_size=state.account_size,
            risk_per_trade=state.risk_per_trade,
            day_pnl=0.0,
            halt_active=False,
            kill_active=False,
        )
    return f"live ordering armed by {clean_signature}"


def kill_live_ui(store: SnapshotStore, journal: Journal, now: datetime) -> str:
    """Disarm live ordering. Never raises: the flag going off is the effect."""
    global _live_state
    with _lock:
        state = _live_state or RiskState(
            account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0,
            halt_active=False, kill_active=False,
        )
    killed = kill_switch(state, journal, now)
    with _lock:
        _live_state = killed
    return f"live ordering off (halt={killed.halt_active} kill={killed.kill_active})"


def card_reference_price(card: Any, bar: Any) -> Optional[float]:
    """Per-share option premium for sizing, or None when unknown."""
    net = getattr(card, "net_debit", None)
    if net is not None and net > 0:
        return float(net) / 100.0
    return None


def build_live_intent(
    card: Any,
    index: int,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    config: Optional[OperatorConfig] = None,
) -> Optional[OrderIntent]:
    """Threshold-checked intent for one card, or None with a live_skipped receipt."""
    from bot.risk import build_intent
    from bot.tradecards import risk_cap_for, size_for_card

    settings = store.read_settings()
    if card.direction not in ("long", "short"):
        journal.append(now, "live_skipped", {"reason": "flat card", "index": index})
        return None
    if card.confidence < settings.live_min_confidence:
        journal.append(
            now, "live_skipped",
            {
                "reason": "below confidence threshold",
                "index": index,
                "confidence": card.confidence,
                "threshold": settings.live_min_confidence,
            },
        )
        return None
    profile = store.read_profile()
    if profile is None:
        journal.append(now, "live_skipped", {"reason": "profile required", "index": index})
        return None
    bar = store.read_latest_bar(card.symbol)
    reference = card_reference_price(card, bar)
    if reference is None or reference <= 0:
        journal.append(now, "live_skipped", {"reason": "no option premium to size", "index": index})
        return None
    contracts, _, _ = size_for_card(card.max_loss, risk_cap_for(profile))
    if contracts < 1:
        journal.append(now, "live_skipped", {"reason": "no affordable contracts", "index": index})
        return None
    if card.hedge_strike is None:
        legs: tuple[OrderLeg, ...] = (
            OrderLeg(
                symbol=card.symbol, side="buy", size=contracts * reference * 100.0,
                strike=card.strike, expiry=card.expiry, call_or_put=card.call_or_put,
            ),
        )
    else:
        leg_size = contracts * reference * 100.0
        legs = (
            OrderLeg(
                symbol=card.symbol, side="buy", size=leg_size,
                strike=card.strike, expiry=card.expiry, call_or_put=card.call_or_put,
            ),
            OrderLeg(
                symbol=card.symbol, side="sell", size=leg_size,
                strike=card.hedge_strike, expiry=card.expiry, call_or_put=card.call_or_put,
            ),
        )
    signal = Signal(
        direction=card.direction,
        thesis=f"live {card.spread} {card.expiry} conf {card.confidence:.2f}",
        confidence=card.confidence,
        snapshot_id=card.snapshot_id,
        symbol=card.symbol,
    )
    state, _ = live_state_and_limits(
        store,
        config or OperatorConfig(max_day_loss=1000.0, day_profit_target=500.0, max_trade_size=5000.0),
    )
    return build_intent(signal, f"live-{card.snapshot_id}-{index}", state, journal, now, legs=legs)


def take_live_trade(
    index: int,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    snapshot_id: Optional[str] = None,
    venue: Any = None,
) -> str:
    """Send one qualifying trade card to the broker. Raises ValueError when gated."""
    from bot.demo import demo_cards

    if store.read_profile() is None:
        raise ValueError("live trading needs a saved operator profile")
    _, cards = demo_cards(underlying, store, journal, now, snapshot_id)
    if not cards:
        raise ValueError(f"no cards for {underlying}")
    if index < 0 or index >= len(cards):
        raise ValueError(f"index {index} out of range (0-{len(cards) - 1})")
    card = cards[index]
    return _submit_card(card, index, store, journal, config, now, venue=venue)


def _submit_card(
    card: Any,
    index: int,
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    venue: Any = None,
) -> str:
    """Intent plus submit for an already-resolved card. Raises ValueError when gated."""
    from bot.autonomy import submit_live

    intent = build_live_intent(card, index, store, journal, now, config)
    if intent is None:
        reason = journal.entries()[-1].payload.get("reason", "skipped") if journal.entries() else "skipped"
        return f"live order for card {index} skipped ({reason})"
    bar = store.read_latest_bar(card.symbol)
    reference = card_reference_price(card, bar)
    assert reference is not None and reference > 0
    state, limits = live_state_and_limits(store, config)
    try:
        fill, _, updated = submit_live(
            intent, state, None, limits, journal, now,
            venue=venue, reference_price=reference,
            profile=store.read_profile(), view=None,
        )
    except (LiveDisabledError, LiveOrderError, LiveConfigError, ValueError) as exc:
        raise ValueError(f"live order rejected: {exc}") from exc
    _store_live_result(updated if updated is not None else state, limits)
    return (
        f"live order filled: {intent.side} {fill.size:.0f} {card.symbol} "
        f"@{fill.price:.2f} (card {index} conf {card.confidence:.2f})"
    )


def maybe_auto_live(
    cards: Sequence[Any],
    store: SnapshotStore,
    journal: Journal,
    config: OperatorConfig,
    now: datetime,
    venue: Any = None,
) -> str:
    """Send qualifying cards to the broker when auto-send is on. Never raises."""
    settings = store.read_settings()
    if not settings.live_auto_send:
        return ""
    sent = 0
    notes: list[str] = []
    for offset, card in enumerate(cards):
        if sent >= MAX_AUTO_LIVE_SENDS:
            notes.append(f"auto-live cap reached ({MAX_AUTO_LIVE_SENDS} sends this run)")
            break
        if card.direction not in ("long", "short"):
            continue
        if card.confidence < settings.live_min_confidence:
            continue
        try:
            text = _submit_card(card, offset, store, journal, config, now, venue=venue)
        except ValueError as exc:
            journal.append(now, "live_blocked", {"reason": str(exc)[:160], "index": offset})
            notes.append(f"card {offset} blocked: {exc}")
            continue
        except Exception as exc:  # fail closed, journaled, never raised
            journal.append(now, "live_blocked", {"reason": type(exc).__name__, "index": offset})
            notes.append(f"card {offset} blocked: {type(exc).__name__}")
            continue
        if "filled" in text:
            sent += 1
            notes.append(text)
    if not notes:
        return ""
    summary = f"auto-live: {sent} fill(s) -- " + "; ".join(notes[:4])
    journal.append(now, "live_auto", {"sent": sent, "notes": notes[:4]})
    return summary
