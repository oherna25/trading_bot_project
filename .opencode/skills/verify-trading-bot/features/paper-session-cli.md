# Paper session CLI

## Sub-features

- `run_session.py --underlying SPY --store-dir <dir>` runs one scheduled
  paper-trading session: ingest, brief, one paper trade per symbol
  (`--paper-trade/--no-paper-trade`, default on).
- `run_scheduled_session` (`bot/schedule.py`) loads the bundle, runs
  `run_session_core`, appends session journals, saves the bundle.
- In-app equivalent: `POST /session` with `{"underlying": "SPY"}` when the
  server was started with `--session-schedule`; without it the route
  returns 503 `sessions not configured`.

## How to get to it (user POV)

The operator (or cron/launchd, weekdays 07:30 machine-local per the
`run_session.py` docstring) runs one command and gets a printed session
brief; paper fills land in the journal and bundle for `/pnl` and
`/trackrecord`.

## Driving it with plain HTTP + terminal

`run_session.py` defaults to `live_context` (real network + keys), so the
offline drive injects canned contexts instead of running the CLI directly:

```bash
.venv/bin/python -c "
from datetime import datetime, timezone
from bot.feeds import default_context
from bot.schedule import run_scheduled_session
print(run_scheduled_session('SPY', 'data/.verify-scratch-8123',
      datetime.now(timezone.utc),
      context_factory=lambda s: default_context(s))[:400])
"
```

Expect: a printed brief; the scratch bundle gains session journal entries.
The direct CLI (`run_session.py` with no overrides) requires network/keys
and is out of scope for offline verification.

## Gotchas

- Never point `--store-dir` at `data/trading-bot` (the real bundle);
  always use the isolated scratch dir.
- `POST /session` returns 503 unless `run_app.py` got
  `--session-schedule 'M H * * DOW'`; offline verification starts without
  it, so expect 503 there and drive the session via `run_scheduled_session`
  with `default_context` instead.
- Cron example in the `run_session.py` docstring appends to
  `data/session.log`; offline runs must redirect logs into the scratch or
  evidence dir, never the real log path.
