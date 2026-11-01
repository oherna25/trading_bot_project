---
name: verify-trading-bot
description: Verify the trading-bot HTTP app and paper CLI fully offline (canned fixtures, isolated scratch bundle). Use when asked to verify, smoke-test, or prove trading-bot behavior end to end.
---

# Verify trading-bot (offline)

App: local options research/paper-trading bot. Primary surface: HTTP app
(`run_app.py` prints one brief, then blocks serving `bot.muse.serve`).
Secondary surface: `run_session.py` one-shot paper session CLI.
Harness: plain HTTP (`curl` / Python `urllib`) + terminal transcripts.
No browser needed. Everything below is fully offline: no network, no keys.

Hard rules (repo law, `AGENTS.md`):

- NEVER read `.env`. Copy key names from `.env.example` only.
- Secrets never appear in code, journals, or error messages.
- Never touch the real bundle `data/trading-bot` or `data/session.log`.
- Chat/tool flags `live_models`/`grounding`/`cards` stay `False` (default).
- Two instances MUST NOT share a store-dir or port. One verification run
  owns exactly one scratch dir and one port; refuse to double-drive a
  shared instance (results would interleave through the bundle reload and
  the response cache).

## Launch

Pick a port you own (default `8123`) and an isolated scratch dir. The
`<PORT>` below must match in all three values.

```bash
PORT=8123
SCRATCH=data/.verify-scratch-$PORT
EVIDENCE=/tmp/verify-trading-bot-$PORT
mkdir -p "$EVIDENCE"
rm -rf "$SCRATCH" && mkdir -p "$SCRATCH"
# 1. Seed scratch bundle with canned fixtures (no network, no keys).
.venv/bin/python .opencode/skills/verify-trading-bot/scripts/seed_offline.py "$SCRATCH" SPY
# 2. Serve it. Startup prints one brief, then blocks serving. With keys or
#    network the brief is live; without them it prints
#    "live brief failed ..." (feeds fail fast on missing credentials, no
#    network attempted) and the bundle is still saved. Either way the
#    server starts and serves the seeded snapshots.
.venv/bin/python run_app.py --underlying SPY --store-dir "$SCRATCH" \
  --host 127.0.0.1 --port $PORT --refresh-seconds 0 \
  > "$EVIDENCE/server.log" 2>&1 &
echo $! > "$SCRATCH/server.pid"
sleep 3
curl -s http://127.0.0.1:$PORT/healthz; echo
```

`--refresh-seconds 0` disables the re-ingest loop so the run is
deterministic. Bind `127.0.0.1` (loopback avoids the unauthenticated
server warning). The server PID file lives inside the scratch dir so
cleanup kills exactly what this run started.

## Doctor

```bash
BASE=http://127.0.0.1:$PORT
curl -s $BASE/healthz
curl -s $BASE/health | .venv/bin/python -m json.tool | head -20
curl -s $BASE/openapi.json | .venv/bin/python -c "import json,sys; print(sorted(json.load(sys.stdin)['paths']))"
```

Pass criteria: `/healthz` is `{"ok": true}`; `/health` lists `SPY` under
`underlyings`; the openapi paths include `/chat` and `/healthz`.
See `features/health-openapi.md`.

## Drive

Drive ONE mapped feature per run (see `features/README.md`). The default
proof drive is offline chat plus the dashboard fetch:

```bash
.venv/bin/python .opencode/skills/verify-trading-bot/scripts/drive.py \
  http://127.0.0.1:$PORT "$EVIDENCE" SPY
```

Pass criteria: `RESULT.txt` reads `ALL CHECKS PASSED`; `chat.json` has
`"ok": true` with `reply` containing `total exposure:`. For any other
feature, follow its `features/<name>.md` page and save raw response
bodies under `$EVIDENCE/`.

## Evidence

Evidence lives OUTSIDE the scratch store dir so cleanup preserves it:
`/tmp/verify-trading-bot-<PORT>/` containing `server.log`, `healthz.json`,
`openapi.json`, `health.json`, `chat.json`, `dashboard.html`, `RESULT.txt`.
Confirm it after cleanup with `ls` + `cat RESULT.txt`.

## Cleanup

Run after every iteration, pass or fail. Kill only the PID this run
started (from `$SCRATCH/server.pid`); never kill by process name.

```bash
kill "$(cat "$SCRATCH/server.pid")" 2>/dev/null
sleep 2
kill -9 "$(cat "$SCRATCH/server.pid")" 2>/dev/null
rm -rf "$SCRATCH"
curl -s -m 3 http://127.0.0.1:$PORT/healthz || echo "port $PORT free"
ls "$EVIDENCE" && cat "$EVIDENCE/RESULT.txt"
```

The last line is the proof that evidence survived cleanup.

## Helpers

Both scripts are executable (`chmod +x`) and their invocations are shown
above/in `features/`.

- `scripts/seed_offline.py <scratch-dir> [UNDERLYING]` — seeds the scratch
  bundle via `default_context` (canned `_canned_get`) +
  `ingest_chain("tradier_sandbox")` + `ingest_bars("alpaca")` +
  `save_bundle`. Verifies `bot/feeds.py` offline path.
- `scripts/drive.py <base-url> <evidence-dir> [UNDERLYING]` — doctor
  (`/healthz`, `/openapi.json`, `/health`) plus drive (`POST /chat`
  `gex brief`, `GET /dashboard`); writes evidence files and `RESULT.txt`;
  exits nonzero on any failure.
