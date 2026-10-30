from __future__ import annotations

import base64
import hashlib
import html
from datetime import datetime
from typing import Any, Mapping

from bot.journal import Journal
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    generate_image,
    model_api_key,
    prompt_hash,
    receipt_payload,
    select_backend,
)
from bot.shapes import ChainSnapshot, GexProfile, ModelReceipt, Signal
from bot.store import SnapshotStore
from bot.tradecards import PROFILE_REQUIRED, TradeCard

SHARE_CACHE_MAX = 100

_share_cache: dict[str, dict[str, Any]] = {}


def clear_share_cache() -> None:
    _share_cache.clear()


def _share_cache_store(key: str, rendered: dict[str, Any]) -> None:
    if len(_share_cache) >= SHARE_CACHE_MAX:
        _share_cache.pop(next(iter(_share_cache)), None)
    _share_cache[key] = rendered


def card_prompt(regime: str, underlying: str) -> str:
    return (
        f"minimal dark abstract background card for a {regime} options regime on {underlying}, "
        "subtle grid, no text, no numbers, no letters, no watermark"
    )


def share_prompt(card: TradeCard) -> str:
    return (
        f"bold shareable trading card background for a {card.direction} {card.symbol} "
        f"{card.spread} idea, {card.regime} regime, vivid gradient, "
        "no text, no numbers, no letters, no watermark"
    )


def share_numbers(card: TradeCard) -> dict[str, Any]:
    return {
        "symbol": card.symbol,
        "direction": card.direction,
        "spread": card.spread,
        "expiry": card.expiry,
        "confidence": card.confidence,
        "max_loss": card.max_loss,
        "breakeven": card.breakeven,
        "wall_exposure": card.wall_exposure,
        "regime": card.regime,
        "spot": card.spot,
        "flip_point": card.flip_point,
    }


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def share_text(card: TradeCard, art_hash: str | None = None) -> str:
    wall = _money(card.wall_exposure) if card.wall_exposure is not None else "n/a"
    spot = f"{card.spot:g}" if card.spot is not None else "n/a"
    flip = f"{card.flip_point:g}" if card.flip_point is not None else "none"
    loss = f"max loss {_money(card.max_loss)}" if card.max_loss is not None else "loss uncapped"
    lines = [
        f"{card.symbol} {card.direction} {card.spread} {card.expiry} (confidence {card.confidence:.2f})",
        f"wall {wall} | regime {card.regime} | spot {spot} vs flip {flip} | {loss}",
        f"why: {card.reason}",
    ]
    if art_hash:
        lines.append(f"art {art_hash[:12]}")
    else:
        lines.append("art: needs live models")
    return "\n".join(lines)


def share_html(
    card: TradeCard,
    art: bytes | None = None,
    url: str | None = None,
    art_hash: str | None = None,
) -> str:
    if art is not None:
        body = base64.b64encode(art).decode("ascii")
        media = f'<img alt="trade card art" src="data:image/png;base64,{body}">'
        caption_hash = art_hash or hashlib.sha256(art).hexdigest()
    elif url:
        media = f'<img alt="trade card art" src="{html.escape(url, quote=True)}">'
        caption_hash = art_hash or hashlib.sha256(url.encode("utf-8")).hexdigest()
    else:
        media = "<p>art: needs live models</p>"
        caption_hash = None
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(card.symbol)} trade card</title></head><body>"
        f"<h1>{html.escape(card.symbol)} {html.escape(card.direction)}</h1>"
        f"{media}<pre>{html.escape(share_text(card, caption_hash), quote=False)}</pre></body></html>"
    )


def milestone_text(day_pnl: float) -> str:
    if day_pnl > 0:
        return f"up {_money(day_pnl)} today"
    if day_pnl < 0:
        return f"down {_money(-day_pnl)} today"
    return "flat today"


def _art_from_body(body: Any) -> tuple[bytes | None, str | None]:
    if not isinstance(body, dict):
        return None, None
    items = body.get("data") or []
    if not items or not isinstance(items[0], dict):
        return None, None
    first = items[0]
    if first.get("b64_json"):
        try:
            return base64.b64decode(first["b64_json"]), None
        except ValueError:
            return None, None
    if first.get("url"):
        return None, str(first["url"])
    return None, None


def render_brief_card(
    signal: Signal,
    snapshot: ChainSnapshot,
    profile: GexProfile,
    journal: Journal,
    now: datetime,
    snapshot_id: str = "",
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any] | None:
    backend, reason = select_backend("artist", journal, now, env)
    if backend != "model":
        journal.append(now, "card_skipped", {"reason": reason})
        return None
    spec = MODEL_REGISTRY["artist"]
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    prompt = card_prompt(regime, snapshot.underlying)
    try:
        body, latency_ms = generate_image(spec, model_api_key(env), prompt, http_post)
    except Exception as exc:
        journal.append(now, "card_skipped", {"reason": type(exc).__name__})
        return None
    art, url = _art_from_body(body)
    if art is None and url is None:
        journal.append(now, "card_skipped", {"reason": "empty image result"})
        return None
    numbers = {
        "total_exposure": profile.total_exposure,
        "flip_point": profile.flip_point,
        "direction": signal.direction,
        "confidence": signal.confidence,
        "trigger": signal.trigger,
    }
    art_hash = hashlib.sha256(art).hexdigest() if art is not None else ""
    receipt = ModelReceipt(
        model_id=spec.model_id,
        role_name="artist",
        prompt_hash=prompt_hash(prompt),
        tokens_in=0,
        tokens_out=0,
        cost_usd=spec.price_in,
        latency_ms=latency_ms,
    )
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    journal.append(
        now,
        "brief_card",
        {
            "snapshot_id": snapshot_id,
            "regime": regime,
            "numbers": numbers,
            "art_hash": art_hash,
            "art_bytes": len(art) if art is not None else 0,
            "art_url": url,
        },
    )
    return {"art": art, "url": url, "regime": regime, "numbers": numbers, "art_hash": art_hash}


def _render_art(
    prompt: str,
    kind: str,
    numbers: dict[str, Any],
    journal: Journal,
    now: datetime,
    snapshot_id: str = "",
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any] | None:
    key = prompt_hash(prompt)
    hit = _share_cache.get(key)
    if hit is not None:
        journal.append(
            now, kind,
            {"snapshot_id": snapshot_id, "numbers": numbers,
             "art_hash": hit["art_hash"], "cached": True},
        )
        return dict(hit)
    backend, reason = select_backend("artist", journal, now, env)
    if backend != "model":
        journal.append(now, f"{kind}_skipped", {"reason": reason})
        return None
    spec = MODEL_REGISTRY["artist"]
    try:
        body, latency_ms = generate_image(spec, model_api_key(env), prompt, http_post)
    except Exception as exc:
        journal.append(now, f"{kind}_skipped", {"reason": type(exc).__name__})
        return None
    art, url = _art_from_body(body)
    if art is None and url is None:
        journal.append(now, f"{kind}_skipped", {"reason": "empty image result"})
        return None
    if art is not None:
        art_hash = hashlib.sha256(art).hexdigest()
    elif url:
        art_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    else:
        art_hash = ""
    receipt = ModelReceipt(
        model_id=spec.model_id,
        role_name="artist",
        prompt_hash=key,
        tokens_in=0,
        tokens_out=0,
        cost_usd=spec.price_in,
        latency_ms=latency_ms,
    )
    journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    journal.append(
        now, kind,
        {
            "snapshot_id": snapshot_id,
            "numbers": numbers,
            "art_hash": art_hash,
            "art_bytes": len(art) if art is not None else 0,
            "art_url": url,
        },
    )
    rendered = {"art": art, "url": url, "numbers": numbers, "art_hash": art_hash}
    _share_cache_store(key, rendered)
    return dict(rendered)


def render_signal_art(
    card: TradeCard,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any] | None:
    return _render_art(
        share_prompt(card), "signal_card", share_numbers(card),
        journal, now, card.snapshot_id, env, http_post,
    )


def render_milestone_art(
    title: str,
    journal: Journal,
    now: datetime,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> dict[str, Any] | None:
    prompt = (
        f"celebratory trading milestone card background, {title}, "
        "clean gradient, no text, no numbers, no letters, no watermark"
    )
    return _render_art(prompt, "milestone_card", {"title": title}, journal, now, "", env, http_post)


def share_card(
    index: int,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    snapshot_id: str | None = None,
    live_models: bool = False,
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    from bot.demo import demo_cards

    if store.read_profile() is None:
        return PROFILE_REQUIRED
    if snapshot_id is None:
        latest = store.read_latest(underlying)
        if latest is None:
            raise ValueError(f"no snapshot stored for {underlying}")
        snapshot_id = latest[0]
    _, cards = demo_cards(underlying, store, journal, now, snapshot_id, env, http_post)
    if not cards:
        raise ValueError(f"no cards for {underlying} snapshot {snapshot_id}")
    if index < 0 or index >= len(cards):
        raise ValueError(f"index {index} out of range (0-{len(cards) - 1})")
    card = cards[index]
    if not live_models:
        journal.append(now, "share_skipped", {"reason": "live models off"})
        return share_text(card)
    art = render_signal_art(card, journal, now, env, http_post)
    return share_text(card, art["art_hash"] if art is not None else None)
