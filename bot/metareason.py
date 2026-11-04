from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Mapping

from bot import baseten
from bot.config import read_dotenv as _read_dotenv
from bot.journal import Journal
from bot.models import MODEL_API_BASE_URL, MODEL_REGISTRY, HttpPost, chat_spec, effective_user_model, meta_budget_reason, run_analyst

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
    user, _, error = effective_user_model(env)
    if user is not None:
        return "user", ""
    if error:
        return "fallback", error
    baseten_backend, _ = baseten.select_reason_backend(journal, now, env)
    if baseten_backend == "baseten":
        return "baseten", ""
    values = _read_meta_env(env)
    if not values.get(API_KEY_ENV):
        return "fallback", f"missing {API_KEY_ENV}"
    reason = meta_budget_reason(journal, now, env)
    if reason:
        return "fallback", reason
    return "meta", ""


def generate_reason(
    stats: dict[str, Any],
    env: Mapping[str, str] | None = None,
    http_post: HttpPost | None = None,
    timeout: float = REASON_TIMEOUT_SECONDS,
) -> tuple[str, float]:
    user, _, _ = effective_user_model(env)
    if user is None and baseten._read_baseten_env(env).get(baseten.API_KEY_ENV):
        return baseten.generate_reason(stats, env, http_post, timeout)
    if user is not None:
        spec, key, base_url = chat_spec("analyst", env), user.api_key, user.base_url
    else:
        values = _read_meta_env(env)
        key = values.get(API_KEY_ENV, "")
        if not key:
            raise ValueError(f"missing {API_KEY_ENV}")
        spec, base_url = MODEL_REGISTRY["analyst"], MODEL_API_BASE_URL
    prompt = json.dumps(stats)
    messages = [
        {"role": "system", "content": REASON_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    text, receipts = run_analyst(
        spec, key, messages, [], {}, prompt, http_post, timeout, base_url=base_url
    )
    reason = text.strip() if isinstance(text, str) else ""
    if not reason:
        raise ValueError("empty reason")
    latency_ms = sum(receipt.latency_ms for receipt in receipts)
    return reason, latency_ms
