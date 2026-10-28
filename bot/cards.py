from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Any, Mapping

from bot.dashboard import build_options
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


def card_prompt(regime: str, underlying: str) -> str:
    return (
        f"minimal dark abstract background card for a {regime} options regime on {underlying}, "
        "subtle grid, no text, no numbers, no letters, no watermark"
    )


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
    regime = build_options(snapshot, profile).regime
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
