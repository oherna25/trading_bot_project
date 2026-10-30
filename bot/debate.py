from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Mapping

from bot.diff import compact_dollars
from bot.journal import Journal
from bot.models import MODEL_REGISTRY, HttpPost, model_api_key, receipt_payload, run_analyst, select_backend
from bot.tradecards import TradeCard

BULL_SYSTEM = "Argue for this options trade in one short paragraph. Reply with that paragraph only."
BEAR_SYSTEM = "Argue against this options trade in one short paragraph. Reply with that paragraph only."


def _stats(card: TradeCard) -> dict[str, Any]:
    return {
        "symbol": card.symbol,
        "direction": card.direction,
        "spread": card.spread,
        "strike": card.strike,
        "expiry": card.expiry,
        "confidence": round(card.confidence, 2),
        "wall_exposure": card.wall_exposure,
        "regime": card.regime,
        "spot": card.spot,
        "flip_point": card.flip_point,
        "max_loss": card.max_loss,
        "breakeven": card.breakeven,
        "spread_pct": card.spread_pct,
        "earnings_date": card.earnings_date,
        "iv_rank": card.iv_rank,
    }


def _fallback_bull(card: TradeCard) -> str:
    parts = [f"Take the {card.direction}: {card.spread} {card.expiry} screens at {card.confidence:.2f} confidence."]
    if card.wall_exposure is not None:
        parts.append(f"It keys off the {compact_dollars(card.wall_exposure)} wall at {card.strike:g}.")
    if card.spot is not None and card.flip_point is not None:
        parts.append(f"Spot {card.spot:g} vs flip {card.flip_point:g} confirms the {card.regime} read.")
    if card.iv_rank is not None:
        parts.append(f"IV rank {card.iv_rank:.0f} pays for the premium.")
    return " ".join(parts)


def _fallback_bear(card: TradeCard) -> str:
    risks = []
    if card.max_loss is not None:
        risks.append(f"max loss ${card.max_loss:.0f}")
    if card.breakeven is not None:
        risks.append(f"needs {card.breakeven} to breakeven")
    if card.earnings_date is not None:
        risks.append(f"earnings {card.earnings_date} lands inside the window")
    if card.spread_pct is not None and card.spread_pct > 5.0:
        risks.append(f"a {card.spread_pct:.1f}% wide market bleeds edge")
    if card.confidence < 0.5:
        risks.append("the screen itself scores below 0.50")
    if not risks:
        risks.append(f"edge is thin at {card.confidence:.2f} with no hard catalyst")
    return f"Fade the {card.direction}: " + "; ".join(risks) + "."


def _model_case(
    role: str,
    system: str,
    stats_text: str,
    card: TradeCard,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None,
    http_post: HttpPost | None,
) -> str | None:
    backend, reason = select_backend(role, journal, now, env)
    if backend != "model":
        journal.append(now, "model_fallback", {"reason": reason, "debate": role})
        return None
    try:
        text, receipts = run_analyst(
            MODEL_REGISTRY[role],
            model_api_key(env),
            [{"role": "system", "content": system}, {"role": "user", "content": stats_text}],
            [],
            {},
            stats_text,
            http_post,
        )
    except Exception as exc:
        journal.append(now, "model_fallback", {"reason": type(exc).__name__, "debate": role})
        return None
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, card.snapshot_id))
    text = text.strip()
    return text or None


def debate_card(
    card: TradeCard,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    stats_text = json.dumps(_stats(card))
    bull_source = "rules"
    bear_source = "rules"
    bull = bear = None
    if live_models:
        bull = _model_case("analyst", BULL_SYSTEM, stats_text, card, journal, now, env, http_post)
        bear = _model_case("reviewer", BEAR_SYSTEM, stats_text, card, journal, now, env, http_post)
        if bull is not None:
            bull_source = MODEL_REGISTRY["analyst"].model_id
        if bear is not None:
            bear_source = MODEL_REGISTRY["reviewer"].model_id
    bull_text = bull if bull is not None else _fallback_bull(card)
    bear_text = bear if bear is not None else _fallback_bear(card)
    if card.confidence >= 0.5:
        verdict = f"verdict: take the {card.direction} ({card.confidence:.2f})"
    else:
        verdict = f"verdict: pass ({card.confidence:.2f} below 0.50)"
    return f"BULL [{bull_source}]: {bull_text}\nBEAR [{bear_source}]: {bear_text}\n{verdict}"


def card_debate(
    underlying: str,
    store: Any,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
    index: int = 0,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    from bot.demo import demo_cards

    if snapshot_id is None:
        latest = store.read_latest(underlying)
        if latest is None:
            raise ValueError(f"no snapshot stored for {underlying}")
        snapshot_id = latest[0]
    found_id, cards = demo_cards(underlying, store, journal, now, snapshot_id)
    if found_id is None or not cards:
        raise ValueError(f"no cards for {underlying} snapshot {snapshot_id}")
    if index < 0 or index >= len(cards):
        raise ValueError(f"index {index} out of range (0-{len(cards) - 1})")
    return debate_card(cards[index], journal, now, live_models, env, http_post)


def attach_debate(page: str, debate_text: str) -> str:
    section = (
        "<section><h2>bull vs bear</h2>"
        f"<pre>{html.escape(debate_text)}</pre></section></body></html>"
    )
    return page.replace("</body></html>", section)
