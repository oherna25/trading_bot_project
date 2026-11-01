# Health / OpenAPI

## Sub-features

- `GET /healthz` returns `{"ok": true}` with no auth, no store state.
- `GET /health` returns detailed JSON: `last_ingest_utc`, `feed_used`,
  `snapshot_id`, `snapshot_age_seconds`, `quote_age_seconds`,
  `underlyings`, `push_providers`, `model_key_set`, `feeds` (per-feed
  health from `bot/feeds.py:feed_health`).
- `GET /openapi.json` returns the API contract (`openapi_spec()` in
  `bot/muse_http.py`); lists `/chat`, `/session`, `/mcp`, `/dashboard`,
  `/board`, `/cards`, `/pnl`, `/trackrecord`, `/settings`, `/feeds`,
  `/replay.csv`, and more.

## How to get to it (user POV)

The operator (or a load balancer / launcher) hits `/healthz` to confirm
the process is up, then `/health` to confirm the bundle has fresh
snapshots and which feed produced them.

## Driving it with plain HTTP + terminal

```bash
BASE=http://127.0.0.1:8123
curl -s $BASE/healthz; echo
curl -s $BASE/openapi.json | .venv/bin/python -c "import json,sys; print(sorted(json.load(sys.stdin)['paths']))"
curl -s $BASE/health | .venv/bin/python -m json.tool | head -30
```

Expect: `{"ok": true}`; a paths list containing `/chat` and `/healthz`;
`underlyings` containing the seeded symbol (e.g. `SPY`).

## Gotchas

- `/healthz`, `/health`, `/openapi.json` are in `OPEN_PATHS`: they skip
  bearer auth even when `MUSE_AUTH_TOKEN` is set. Every other route
  returns 401 without `Authorization: Bearer <token>` when it is set.
- `/health` `underlyings` is empty until the scratch bundle holds a
  snapshot; seed first (`scripts/seed_offline.py`).
- `model_key_set` reflects whether `META_MODEL_API_KEY` is visible; it
  is `false` in offline verification. Never export real keys to flip it.
