#!/usr/bin/env python3
"""Doctor + drive the trading-bot HTTP app, writing evidence to a dir.

Usage:
    .venv/bin/python scripts/drive.py <base-url> <evidence-dir> [UNDERLYING]

Doctor:  GET /healthz, GET /openapi.json, GET /health
Drive:   POST /chat {"text": "gex brief", ...} (offline defaults) and
         GET /dashboard?underlying=SYM

Writes healthz.json, openapi.json, health.json, chat.json,
dashboard.html, and RESULT.txt into the evidence dir. Exits nonzero
if any check fails. Fully offline: no keys, no network beyond localhost.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def _get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base + path, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(base: str, path: str, payload: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        print("usage: drive.py <base-url> <evidence-dir> [UNDERLYING]", file=sys.stderr)
        raise SystemExit(2)
    base, evdir = argv[1].rstrip("/"), argv[2]
    symbol = (argv[3] if len(argv) > 3 else "SPY").strip().upper() or "SPY"

    import os
    os.makedirs(evdir, exist_ok=True)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip())
        if not ok:
            failures.append(name)

    status, body = _get(base, "/healthz")
    open(f"{evdir}/healthz.json", "wb").write(body)
    check("healthz", status == 200 and json.loads(body) == {"ok": True}, f"status={status}")

    status, body = _get(base, "/openapi.json")
    open(f"{evdir}/openapi.json", "wb").write(body)
    try:
        paths = json.loads(body).get("paths", {})
    except ValueError:
        paths = {}
    check("openapi", status == 200 and "/chat" in paths and "/healthz" in paths, f"status={status}")

    status, body = _get(base, "/health")
    open(f"{evdir}/health.json", "wb").write(body)
    try:
        health = json.loads(body)
    except ValueError:
        health = {}
    check("health", status == 200 and symbol in health.get("underlyings", []),
          f"status={status} underlyings={health.get('underlyings')}")

    status, body = _post(base, "/chat", {"text": "gex brief", "underlying": symbol})
    open(f"{evdir}/chat.json", "wb").write(body)
    try:
        chat = json.loads(body)
    except ValueError:
        chat = {}
    check("chat-gex-brief", status == 200 and chat.get("ok") is True
          and "total exposure:" in chat.get("reply", ""), f"status={status}")

    status, body = _get(base, f"/dashboard?underlying={symbol}")
    open(f"{evdir}/dashboard.html", "wb").write(body)
    check("dashboard", status == 200 and b"<!doctype html>" in body[:200].lower(),
          f"status={status} bytes={len(body)}")

    summary = "ALL CHECKS PASSED" if not failures else f"FAILURES: {', '.join(failures)}"
    open(f"{evdir}/RESULT.txt", "w").write(summary + "\n")
    print(summary)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main(sys.argv)
