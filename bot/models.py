from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from bot.exposure import compute_gex
from bot.journal import Journal
from bot.shapes import ChainSnapshot, ModelReceipt

MODEL_API_BASE_URL = "https://api.meta.ai/v1"
MODEL_TIMEOUT_SECONDS = 60.0
MODEL_MAX_TOKENS = 1200
MODEL_MAX_ROUNDS = 2
MODEL_REASONING_EFFORT = "low"

HttpPost = Callable[..., Any]


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    endpoint: str
    unit: str
    price_in: float
    price_out: float
    fallback: str


MODEL_REGISTRY = {
    "analyst": ModelSpec("muse-spark-1.3", "/chat/completions", "mtok", 1.25, 4.25, "deterministic flip rule"),
    "grounding": ModelSpec("muse-spark-1.3", "/responses", "mtok", 1.25, 4.25, "skip grounding"),
    "reviewer": ModelSpec("muse-wonder-1.0", "/chat/completions", "mtok", 4.00, 20.00, "skip review"),
    "transcriber": ModelSpec("muse-voice-transcribe-1.0", "/asr/transcribe", "minute", 0.003, 0.0, "reject voice"),
    "artist": ModelSpec("muse-image-1.0", "/images/generations", "image", 0.01, 0.0, "skip card"),
}


def _read_dotenv(path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path) as handle:
            lines = handle.readlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            values.setdefault(key, value)
    return values


def _read_model_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    file_values = _read_dotenv()
    out: dict[str, str] = {}
    for name in ("META_MODEL_API_KEY", "MODEL_DAY_BUDGET_USD"):
        value = os.environ.get(name) or file_values.get(name)
        if value:
            out[name] = value
    return out


def model_api_key(env: Mapping[str, str] | None = None) -> str:
    return _read_model_env(env).get("META_MODEL_API_KEY", "")


def _day_budget_usd(env: Mapping[str, str]) -> float | None:
    try:
        return float(env.get("MODEL_DAY_BUDGET_USD", ""))
    except (TypeError, ValueError):
        return None


def spend_today(journal: Journal, now: datetime) -> float:
    total = 0.0
    for entry in journal.entries():
        if entry.kind != "model_receipt":
            continue
        if entry.timestamp_utc.date() != now.date():
            continue
        try:
            total += float(entry.payload.get("cost_usd", 0.0))
        except (TypeError, ValueError):
            continue
    return total


def select_backend(
    role: str, journal: Journal, now: datetime, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    spec = MODEL_REGISTRY.get(role)
    if spec is None:
        return "fallback", f"unknown role {role}"
    values = _read_model_env(env)
    if not values.get("META_MODEL_API_KEY"):
        return "fallback", "missing META_MODEL_API_KEY"
    budget = _day_budget_usd(values)
    if budget is None or budget <= 0:
        return "fallback", "missing MODEL_DAY_BUDGET_USD"
    if spend_today(journal, now) >= budget:
        return "fallback", "day budget spent"
    return "model", ""


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def usage_cost(spec: ModelSpec, tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * spec.price_in + tokens_out / 1e6 * spec.price_out


def chat_cost(spec: ModelSpec, usage: Mapping[str, Any]) -> float:
    tokens_in = usage.get("prompt_tokens", 0) or 0
    tokens_out = usage.get("completion_tokens", 0) or 0
    return usage_cost(spec, tokens_in, tokens_out)


def receipt_payload(receipt: ModelReceipt, snapshot_id: str) -> dict[str, Any]:
    return {
        "model_id": receipt.model_id,
        "role_name": receipt.role_name,
        "prompt_hash": receipt.prompt_hash,
        "tokens_in": receipt.tokens_in,
        "tokens_out": receipt.tokens_out,
        "cost_usd": receipt.cost_usd,
        "latency_ms": receipt.latency_ms,
        "snapshot_id": snapshot_id,
    }


def _urllib_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


HttpPostMultipart = Callable[..., Any]


def _urllib_post_multipart(
    url: str, headers: dict[str, str], parts: list[tuple[str, str, str | None, bytes]], timeout: float
) -> Any:
    boundary = "----bot" + uuid.uuid4().hex
    buf = b""
    for name, content_type, filename, data in parts:
        buf += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'.encode()
        if filename is not None:
            buf += f'; filename="{filename}"'.encode()
        buf += f"\r\nContent-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    merged = dict(headers)
    merged["Content-Type"] = "multipart/form-data; boundary=" + boundary
    request = urllib.request.Request(url, data=buf, headers=merged)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def transcribe_audio(
    spec: ModelSpec,
    api_key: str,
    audio_wav: bytes,
    http_post_multipart: HttpPostMultipart | None = None,
    timeout: float = MODEL_TIMEOUT_SECONDS,
) -> tuple[Any, float]:
    post = http_post_multipart or _urllib_post_multipart
    request_json = json.dumps({"mode": "DIARIZATION", "model": spec.model_id, "audioEncoding": "WAV"})
    parts = [
        ("request", "application/json", None, request_json.encode("utf-8")),
        ("audio", "audio/wav", "voice.wav", audio_wav),
    ]
    started = time.monotonic()
    body = post(
        MODEL_API_BASE_URL + spec.endpoint,
        {"Authorization": f"Bearer {api_key}"},
        parts,
        timeout,
    )
    return body, (time.monotonic() - started) * 1000.0


def generate_image(
    spec: ModelSpec,
    api_key: str,
    prompt: str,
    http_post: HttpPost | None = None,
    timeout: float = MODEL_TIMEOUT_SECONDS,
) -> tuple[Any, float]:
    post = http_post or _urllib_post
    started = time.monotonic()
    body = post(
        MODEL_API_BASE_URL + spec.endpoint,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {"model": spec.model_id, "prompt": prompt, "n": 1},
        timeout,
    )
    return body, (time.monotonic() - started) * 1000.0


def run_responses(
    spec: ModelSpec,
    api_key: str,
    input_text: str,
    tools: list[dict[str, Any]],
    prompt_text: str,
    role_name: str = "grounding",
    http_post: HttpPost | None = None,
    timeout: float = MODEL_TIMEOUT_SECONDS,
) -> tuple[str, ModelReceipt]:
    post = http_post or _urllib_post
    payload: dict[str, Any] = {
        "model": spec.model_id,
        "input": input_text,
        "reasoning": {"effort": MODEL_REASONING_EFFORT},
    }
    if tools:
        payload["tools"] = tools
    started = time.monotonic()
    body = post(
        MODEL_API_BASE_URL + spec.endpoint,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload,
        timeout,
    )
    latency_ms = (time.monotonic() - started) * 1000.0
    usage = body.get("usage", {}) or {}
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    messages_text: list[str] = []
    for item in body.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        parts = [
            chunk.get("text", "")
            for chunk in item.get("content", []) or []
            if isinstance(chunk, dict) and chunk.get("type") == "output_text"
        ]
        if parts:
            messages_text.append("".join(parts))
    text = messages_text[-1] if messages_text else ""
    receipt = ModelReceipt(
        model_id=spec.model_id,
        role_name=role_name,
        prompt_hash=prompt_hash(prompt_text),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=usage_cost(spec, tokens_in, tokens_out),
        latency_ms=latency_ms,
    )
    return text, receipt


def gex_tool_result(snapshot: ChainSnapshot, snapshot_id: str, args: Mapping[str, Any]) -> str:
    if args.get("underlying") != snapshot.underlying:
        raise ValueError("tool called for wrong underlying")
    profile = compute_gex(snapshot, snapshot_id)
    return json.dumps(
        {
            "total_exposure": profile.total_exposure,
            "flip_point": profile.flip_point,
            "per_strike_exposure": profile.per_strike_exposure,
        }
    )


def run_analyst(
    spec: ModelSpec,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_impls: Mapping[str, Callable[[Mapping[str, Any]], str]],
    prompt_text: str,
    http_post: HttpPost | None = None,
    timeout: float = MODEL_TIMEOUT_SECONDS,
    reasoning_effort: str = MODEL_REASONING_EFFORT,
) -> tuple[str, list[ModelReceipt]]:
    post = http_post or _urllib_post
    current = list(messages)
    receipts: list[ModelReceipt] = []
    text = ""
    for _ in range(MODEL_MAX_ROUNDS):
        payload: dict[str, Any] = {
            "model": spec.model_id,
            "messages": current,
            "max_tokens": MODEL_MAX_TOKENS,
            "temperature": 0,
            "reasoning_effort": reasoning_effort,
        }
        if tools:
            payload["tools"] = tools
        started = time.monotonic()
        body = post(
            MODEL_API_BASE_URL + spec.endpoint,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload,
            timeout,
        )
        latency_ms = (time.monotonic() - started) * 1000.0
        usage = body.get("usage", {}) or {}
        receipts.append(
            ModelReceipt(
                model_id=spec.model_id,
                role_name="analyst",
                prompt_hash=prompt_hash(prompt_text),
                tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                tokens_out=int(usage.get("completion_tokens", 0) or 0),
                cost_usd=chat_cost(spec, usage),
                latency_ms=latency_ms,
            )
        )
        message = body.get("choices", [{}])[0].get("message", {}) or {}
        text = message.get("content") or ""
        calls = message.get("tool_calls") or []
        if not calls:
            break
        current.append(message)
        for call in calls:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            impl = tool_impls.get(function.get("name", ""))
            if impl is None:
                raise ValueError(f"unknown tool {function.get('name', '')}")
            current.append({"role": "tool", "tool_call_id": call.get("id"), "content": impl(args)})
    return text, receipts
