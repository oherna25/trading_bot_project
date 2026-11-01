from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from bot.config import read_dotenv as _read_dotenv
from bot.models import prompt_hash
from bot.shapes import ModelReceipt

LOCAL_TIMEOUT_SECONDS = 180.0
LOCAL_MAX_TOKENS = 512
LOCAL_SERVER_ENV = "LLAMA_SERVER_BIN"
LOCAL_SERVER_DEFAULT = "llama-server"
LOCAL_SERVER_CTX_ENV = "LOCAL_SERVER_CTX"
LOCAL_SERVER_THREADS_ENV = "LOCAL_SERVER_THREADS"
LOCAL_SERVER_CTX_DEFAULT = 4096
LOCAL_SERVER_THREADS_DEFAULT = 4
LOCAL_SERVER_START_TIMEOUT = 30.0
LOCAL_SERVER_HOST = "127.0.0.1"

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
    names = [spec.env_var for spec in LOCAL_REGISTRY.values()]
    names += [LOCAL_SERVER_ENV, LOCAL_SERVER_CTX_ENV, LOCAL_SERVER_THREADS_ENV]
    for name in names:
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


def server_argv(
    binary: str, weights_path: str, port: int, ctx_size: int, threads: int
) -> list[str]:
    return [
        binary, "-m", weights_path, "--host", LOCAL_SERVER_HOST, "--port", str(port),
        "-c", str(ctx_size), "-t", str(threads),
    ]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOCAL_SERVER_HOST, 0))
        return int(sock.getsockname()[1])


class LocalServer:
    """One persistent llama-server process shared by every local call.

    Prompts travel in the POST body, never argv. One request flies at a time.
    """

    def __init__(
        self,
        binary: str,
        weights_path: str,
        port: int = 0,
        ctx_size: int = LOCAL_SERVER_CTX_DEFAULT,
        threads: int = LOCAL_SERVER_THREADS_DEFAULT,
        start_timeout: float = LOCAL_SERVER_START_TIMEOUT,
    ) -> None:
        self._binary = binary
        self._weights_path = weights_path
        self._port = port or _free_port()
        self._ctx_size = ctx_size
        self._threads = threads
        self._start_timeout = start_timeout
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._attached = False
        self._send_lock = threading.Lock()

    @classmethod
    def attach(cls, port: int) -> LocalServer:
        server = cls(binary="", weights_path="", port=port)
        server._attached = True
        return server

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{LOCAL_SERVER_HOST}:{self._port}"

    @property
    def running(self) -> bool:
        if self._attached:
            return True
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        if self._process is None:
            return None
        return self._process.pid

    def _argv(self) -> list[str]:
        return server_argv(self._binary, self._weights_path, self._port, self._ctx_size, self._threads)

    def start(self) -> None:
        if self.running:
            return
        self._process = subprocess.Popen(
            self._argv(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._process = None
                raise ValueError("local server did not start")
            if self._ready():
                return
            time.sleep(0.05)
        self.stop()
        raise ValueError("local server did not start")

    def _ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/health", timeout=2.0
            ) as response:
                response.read()
            return True
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def complete(
        self, prompt: str, n_predict: int = LOCAL_MAX_TOKENS, timeout: float = LOCAL_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        payload = json.dumps(
            {"prompt": prompt, "n_predict": n_predict, "cache_prompt": True}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/completion",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        with self._send_lock:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except TimeoutError as exc:
                raise ValueError("local run timed out") from exc
            except (OSError, ValueError, http.client.HTTPException) as exc:
                raise ValueError("local run failed") from exc
        latency_ms = (time.monotonic() - started) * 1000.0
        text = str(body.get("content", "")) if isinstance(body, dict) else ""
        return {"text": text, "latency_ms": latency_ms}

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


_servers: dict[tuple[str, str], LocalServer] = {}
_servers_lock = threading.Lock()


def get_local_server(
    binary: str,
    weights_path: str,
    ctx_size: int = LOCAL_SERVER_CTX_DEFAULT,
    threads: int = LOCAL_SERVER_THREADS_DEFAULT,
) -> LocalServer:
    key = (binary, weights_path)
    with _servers_lock:
        server = _servers.get(key)
        if server is None or not server.running:
            server = LocalServer(binary, weights_path, ctx_size=ctx_size, threads=threads)
            server.start()
            _servers[key] = server
        return server


def stop_local_servers() -> None:
    with _servers_lock:
        servers = list(_servers.values())
        _servers.clear()
    for server in servers:
        server.stop()


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(raw or "")
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


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
        binary = values.get(LOCAL_SERVER_ENV) or os.environ.get(LOCAL_SERVER_ENV) or LOCAL_SERVER_DEFAULT
        resolved = shutil.which(binary)
        if resolved is None:
            raise ValueError("local runner missing")
        server = get_local_server(
            resolved,
            weights_path,
            ctx_size=_positive_int(
                values.get(LOCAL_SERVER_CTX_ENV) or os.environ.get(LOCAL_SERVER_CTX_ENV),
                LOCAL_SERVER_CTX_DEFAULT,
            ),
            threads=_positive_int(
                values.get(LOCAL_SERVER_THREADS_ENV) or os.environ.get(LOCAL_SERVER_THREADS_ENV),
                LOCAL_SERVER_THREADS_DEFAULT,
            ),
        )
        body = server.complete(prompt, timeout=timeout)
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
