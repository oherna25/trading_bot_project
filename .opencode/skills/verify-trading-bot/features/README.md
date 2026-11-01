# Feature map

Top user-facing surfaces of the trading-bot HTTP app (`run_app.py` +
`bot/muse_http.py:route_http`) and the `run_session.py` CLI. Each file
covers one feature with H2s: Sub-features, How to get to it (user POV),
Driving it with plain HTTP + terminal, Gotchas.

- `health-openapi.md` — liveness probes and the API contract.
- `dashboard-brief.md` — dashboard page (positioning brief from stored snapshots).
- `offline-chat.md` — deterministic offline chat (default flags off).
- `paper-session-cli.md` — scheduled paper-trading session (`run_session.py`).
- `trackrecord-pnl.md` — paper P&L and track record pages.

Harness everywhere: `curl` or Python `urllib` against the local HTTP app
plus terminal transcripts. No browser needed. All drives assume the
Launch steps in `../SKILL.md` (isolated scratch store dir, `--refresh-seconds 0`).
