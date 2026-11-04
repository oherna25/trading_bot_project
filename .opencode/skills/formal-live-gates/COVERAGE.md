# Coverage record: formal-live-gates

## Covered files and functions

| File | Functions / behavior covered |
|---|---|
| `bot/autonomy.py` | `enable_live` (toggle: arms iff no halt and no kill), `submit_live` (all blocking gates + venue reachability), `kill_switch` (disarm + flags + journal), `reset_autonomy` (clears flags, does NOT re-arm) |
| `bot/risk.py` | `review_intent` (halt/kill veto, notional, options-profile veto), `fire_halt`, `fire_kill`, `operator_reset` (flag transitions used inside ordering sequences) |
| `bot/shapes.py` | `RiskState`, `OrderIntent`, `RiskLimits`, `PaperEvidence` shapes as constructed by the check |
| `bot/journal.py` | In-memory `Journal` append/entry kinds asserted (`live_blocked`, `live_enabled`, `kill_fired`) |
| `bot/venue.py` | NOT covered (see below); only the stub stand-in is exercised |

## Bounds (complete inside this scope -- every case runs)

- Arming is a plain toggle (no signature, evidence, or checklist gate); `halt_active`, `kill_active`, `live_enabled` exhaustive booleans.
- Intents: good-shaped (schema-valid, notional 1000 <= 5000) / oversized (999999 > 5000) / single options leg.
- `submit_live` args: all present / each of state, limits, journal, now missing.
- Event sequences: ALL 9,330 over {arm-ok, arm-bad, kill, halt, reset, submit}, lengths 1..5, each step asserting venue-reachability IFF gates open.
- Fixed parameters: `reference_price=100.0`, `LIMITS=(5000, 500, 1000)`, `day_pnl=0.0`, stub fill price 150.0. Day-pnl drift is nil by construction (mark == fill); the harness fails loud if a good intent ever stops being approved on clean flags.

## Assumptions

- `autonomy.live_enabled` is a process-global boolean; the check resets it between cases. Multi-account aliasing of the global is out of scope.
- The venue stub faithfully represents "money moves" as `place_order` being called. Broker-side behavior is out of scope.
- `review_intent` dimensions other than halt/kill/notional/options/schema/day-targets are exercised only via the constructed intents (view=None, no close-intent path).

## NOT covered

- `TradierVenue` HTTP behavior, `from_env` secret handling, order sizing/`occ_symbol` mapping, combo-leg partial-fill handling.
- `submit_paper` and paper-account math (`update_position`, `update_day_pnl`, drawdown/position/sector caps beyond the veto spot-checks).
- `check_promotion` (advisory only; it no longer gates arming).
- Concurrency / thread-safety of the `live_enabled` global.
- Timing: delayed-chain staleness, clock skew, journal persistence.
- Secrets handling (never read `.env`; key names from `.env.example` only -- this check reads neither).
