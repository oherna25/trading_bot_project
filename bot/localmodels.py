from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from bot.config import read_dotenv as _read_dotenv
from bot.models import prompt_hash
from bot.shapes import ModelReceipt

LOCAL_TIMEOUT_SECONDS = 180.0
LOCAL_MAX_TOKENS = 512
LOCAL_BINARY_ENV = "LLAMA_BIN"
LOCAL_BINARY_DEFAULT = "llama-cli"

LocalRunner = Callable[[str, str, float], dict[str, Any]]


@dataclass(frozen=True)
class LocalSpec:
    name: str
    model_id: str
    env_var: str


LOCAL_REGISTRY = {
    "glimmer": LocalSpec("glimmer", "muse-glimmer-3b", "GLIMMER_GGUF_PATH"),
    "spark-open": LocalSpec("spark-open", "muse-spark-open-8b", "SPARK_OPEN_GGUF_PATH"),
}


def _read_local_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    file_values = _read_dotenv()
    out: dict[str, str] = {}
    for name in [spec.env_var for spec in LOCAL_REGISTRY.values()] + [LOCAL_BINARY_ENV]:
        value = os.environ.get(name) or file_values.get(name)
        if value:
            out[name] = value
    return out


def local_weights(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    values = _read_local_env(env)
    for name, spec in LOCAL_REGISTRY.items():
        path = (values.get(spec.env_var) or "").strip()
        if path and os.path.exists(path):
            return name, path
    return None


def local_available(env: Mapping[str, str] | None = None) -> bool:
    return local_weights(env) is not None


def _llama_runner(prompt: str, weights_path: str, timeout: float, binary: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                binary, "-m", weights_path, "-p", prompt,
                "-n", str(LOCAL_MAX_TOKENS), "--no-display-prompt",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("local run timed out") from exc
    latency_ms = (time.monotonic() - started) * 1000.0
    if completed.returncode != 0:
        raise ValueError("local run failed")
    return {"text": completed.stdout.decode("utf-8", "replace"), "latency_ms": latency_ms}


def run_local(
    task: str,
    prompt: str,
    env: Mapping[str, str] | None = None,
    runner: LocalRunner | None = None,
    timeout: float = LOCAL_TIMEOUT_SECONDS,
) -> tuple[str, ModelReceipt]:
    found = local_weights(env)
    if found is None:
        raise ValueError("no local weights configured")
    name, weights_path = found
    spec = LOCAL_REGISTRY[name]
    if runner is None:
        values = _read_local_env(env)
        binary = values.get(LOCAL_BINARY_ENV) or os.environ.get(LOCAL_BINARY_ENV) or LOCAL_BINARY_DEFAULT
        resolved = shutil.which(binary)
        if resolved is None:
            raise ValueError("local runner missing")
        body = _llama_runner(prompt, weights_path, timeout, resolved)
    else:
        started = time.monotonic()
        body = runner(prompt, weights_path, timeout)
        if "latency_ms" not in body:
            body["latency_ms"] = (time.monotonic() - started) * 1000.0
    text = str(body.get("text", ""))
    if not text.strip():
        raise ValueError("empty local reply")
    try:
        latency_ms = float(body.get("latency_ms", 0.0) or 0.0)
    except (TypeError, ValueError):
        latency_ms = 0.0
    receipt = ModelReceipt(
        model_id=spec.model_id,
        role_name=f"local_{task}",
        prompt_hash=prompt_hash(prompt),
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=latency_ms,
    )
    return text, receipt
