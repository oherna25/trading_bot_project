from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Mapping

from bot.config import read_dotenv as _read_dotenv
from bot.journal import Journal
from bot.models import MODEL_REGISTRY, HttpPost, run_analyst

API_KEY_ENV = "META_MODEL_API_KEY"
REASON_TIMEOUT_SECONDS = 60.0
REASON_SYSTEM_PROMPT = (
    "Write one sentence stating why this options trade is a good trade. "
    "Reply with that sentence only."
)


def _read_meta_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    file_values = _read_dotenv()
    out: dict[str, str] = {}
    value = os.environ.get(API_KEY_ENV) or file_values.get(API_KEY_ENV)
    if value:
        out[API_KEY_ENV] = value
    return out


def select_reason_backend(
    journal: Journal, now: datetime, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    _ = (journal, now)
    values = _read_meta_env(env)
    if not values.get(API_KEY_ENV):
        return "fallback", f"missing {API_KEY_ENV}"
    return "meta", ""


def generate_reason(
    stats: dict[str, Any],
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    timeout: float = REASON_TIMEOUT_SECONDS,
) -> tuple[str, float]:
    values = _read_meta_env(env)
    key = values.get(API_KEY_ENV, "")
    if not key:
        raise ValueError(f"missing {API_KEY_ENV}")
    prompt = json.dumps(stats)
    messages = [
        {"role": "system", "content": REASON_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    text, receipts = run_analyst(
        MODEL_REGISTRY["analyst"], key, messages, [], {}, prompt, http_post, timeout
    )
    reason = text.strip() if isinstance(text, str) else ""
    if not reason:
        raise ValueError("empty reason")
    latency_ms = sum(receipt.latency_ms for receipt in receipts)
    return reason, latency_ms
