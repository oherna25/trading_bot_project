from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Mapping, Sequence

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.models import (
    MODEL_REGISTRY,
    HttpPost,
    model_api_key,
    receipt_payload,
    run_analyst,
    select_backend,
)
from bot.store import SnapshotStore

CHART_MAX_BYTES = 5 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

CHART_SYSTEM = (
    "Read the attached trading chart screenshot. Reply with one short paragraph: "
    "where price sits versus the GEX walls and flip given in the prompt, and whether "
    "the image agrees or disagrees with the engine read. Ground every number in the "
    "image or the prompt; invent nothing."
)

CHART_USAGE = (
    "send a chart screenshot to POST /chartread?underlying=SYM as raw png or jpeg bytes, "
    "or call the read_chart_image tool with base64 image bytes"
)


def image_kind(data: bytes) -> str | None:
    if data[:8] == PNG_MAGIC:
        return "png"
    if data[:3] == JPEG_MAGIC:
        return "jpeg"
    return None


def chart_prompt(
    underlying: str,
    snapshot_id: str,
    total_exposure: float | None,
    flip_point: float | None,
    walls: Sequence[tuple[float, float]],
    spot: float | None,
    question: str = "",
    regions: Sequence[Mapping[str, Any]] = (),
) -> str:
    if total_exposure is None:
        context = f"{underlying}: no stored snapshot; read only what the image shows."
    else:
        regime = "long-gamma" if total_exposure >= 0.0 else "short-gamma"
        wall_text = ", ".join(f"{strike:g} ({value:,.0f})" for strike, value in walls[:5]) or "none"
        context = (
            f"{underlying} engine read from snapshot {snapshot_id}: regime {regime}, "
            f"total {total_exposure:,.0f}, flip {flip_point}, spot {spot}, top walls {wall_text}."
        )
    bits = [context]
    if regions:
        focus = "; ".join(
            f"{r.get('label', 'region')} at x={r.get('x')},y={r.get('y')},w={r.get('w')},h={r.get('h')}"
            for r in regions
        )
        bits.append(f"Focus on these image regions (0-1000 scale): {focus}.")
    bits.append(question or "Where is price versus the walls and flip?")
    return " ".join(bits)


def read_chart_image(
    image: bytes,
    underlying: str,
    store: SnapshotStore,
    journal: Journal,
    now: datetime,
    live_models: bool = False,
    question: str = "",
    regions: Sequence[Mapping[str, Any]] = (),
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
) -> str:
    if not live_models:
        journal.append(now, "chart_skipped", {"reason": "live models off"})
        return "chart reading needs live models"
    kind = image_kind(image)
    if kind is None:
        journal.append(now, "chart_skipped", {"reason": "not an image"})
        return "chart image rejected: not a png or jpeg"
    if len(image) > CHART_MAX_BYTES:
        journal.append(now, "chart_skipped", {"reason": "image too large"})
        return "chart image rejected: image too large"
    backend, reason = select_backend("analyst", journal, now, env)
    if backend != "model":
        journal.append(now, "chart_skipped", {"reason": reason})
        return f"chart reading unavailable: {reason}"
    found = store.read_latest(underlying)
    if found is None:
        snapshot_id, prompt = "", chart_prompt(underlying, "", None, None, [], None, question, regions)
    else:
        snapshot_id, snapshot = found
        bar = store.read_latest_bar(underlying)
        spot = bar.close if bar is not None else None
        profile = compute_gex(snapshot, snapshot_id, spot=spot)
        walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))
        prompt = chart_prompt(
            underlying, snapshot_id, profile.total_exposure, profile.flip_point,
            walls, spot, question, regions,
        )
    body = base64.b64encode(image).decode("ascii")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": CHART_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/{kind};base64,{body}"}},
            ],
        },
    ]
    try:
        text, receipts = run_analyst(
            MODEL_REGISTRY["analyst"], model_api_key(env), messages, [], {}, prompt, http_post
        )
    except Exception as exc:
        journal.append(now, "chart_skipped", {"reason": type(exc).__name__})
        return f"chart reading failed: {type(exc).__name__}"
    for receipt in receipts:
        journal.append(now, "model_receipt", receipt_payload(receipt, snapshot_id))
    reading = " ".join(text.split())
    if not reading:
        journal.append(now, "chart_skipped", {"reason": "empty chart read"})
        return "chart reading returned no text"
    journal.append(
        now, "chart_read",
        {"underlying": underlying, "bytes": len(image), "kind": kind, "regions": len(regions)},
    )
    return reading
