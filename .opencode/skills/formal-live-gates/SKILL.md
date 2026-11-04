---
name: formal-live-gates
description: Exhaustive gate verification for live-ordering safety. Use when a change touches bot/autonomy.py, the halt/kill/review gates in bot/risk.py, or anything in the submit_live path — or when reviewing such a change — to prove live money still cannot move unless the toggle is armed and every submit gate passed, and kill_switch still always stops it.
---

# formal-live-gates

Area: live-ordering safety gates (`submit_live` / `enable_live` / `kill_switch` ordering).

## Property (one sentence)

Live money cannot move unless the toggle is armed and every submit gate passed, and `kill_switch` always stops it.

## Does a change touch verified code?

Yes if it edits any file/function listed in `COVERAGE.md`, or changes what
`review_intent` approves, what arms/disarms the `live_enabled` global, or what
reaches `TradierVenue.place_order` from `submit_live`. If yes, run the check
before landing. Docs-only or unrelated-module changes do not need a rerun
(but still confirm `COVERAGE.md` lists no touched function).

## Run the check (one command, ~1s, stdlib only, offline)

From repo root:

```bash
.venv/bin/python .opencode/skills/formal-live-gates/check_live_gates.py
```

Exit `0` + `PASS` = property holds on the current tree. Exit `1` = failure;
the output names the phase and prints the counterexample in the code's own
terms (flag values, what `submit_live` returned, how many times the venue
was reached). No network, no ports, no servers, no persistence: journals are
in-memory and the `live_enabled` global is reset between cases.

## What the check is

Exhaustive small-model enumeration executed against the REAL product code --
not a hand model. 7,600 cases: the `enable_live` toggle matrix over halt/kill (4),
`submit_live` (160), `submit_live`'s own gates with the review verdict forced
both ways (41, proves defense in depth), `review_intent` vetoes (8),
kill/reset ordering (2), plus ALL 9,330 event sequences over
`{arm-ok, arm-bad, kill, halt, reset, submit}` up to length 5. The only models
are documented in the script header: a venue stub that counts `place_order`
calls (each call = live money would move) and two forced review verdicts for
the own-gates phase.

Tool choice: TLA+/TLC is the default for event-ordering questions but needs
Java, which this host lacks; no solver CLI ships here either. Installing one
would add a network dependency to every run and still require hand-modeling
Python semantics -- weaker than executing the real functions over the whole
finite gate domain, which is already complete. Revisit only if the gate
domain stops being finite.

## Real bug vs modeling error

- Output lines starting `[counterexample` name real gate inputs and real
  product behavior (e.g. `kill=True ... filled=True venue_calls=1`). That is
  a product-code bug: fix the gate, never weaken the check to pass.
- Lines starting `[harness]` mean the check's own oracle disagrees with the
  product (e.g. a new `review_intent` veto dimension was added intentionally).
  Read the named code, update the oracle and `COVERAGE.md`, rerun.
- If the current tree already fails: report it as a finding with the pasted
  counterexample. Do not change product code to make it pass without an
  explicit user decision, and do not weaken the property.

## Updating verification on intended changes

1. Change product code.
2. Rerun the check. If it fails and the new behavior is intended, update the
   property statement, oracle, and `COVERAGE.md` together.
3. Only when the check passes on the new tree, move `SYNC_COMMIT` forward:
   record the new base commit plus fresh `sha256sum` of the covered files.
   Never move the sync marker on a red check.

## Proven able-to-fail

Removing `or state.kill_active` from `submit_live` (`bot/autonomy.py`) makes
the check exit 1 with:

```text
[counterexample C2: submit_live own gates] review forced approve,
live_enabled=True halt=False kill=True args=all -> filled=True
venue_calls=1; expected LiveDisabledError, no venue call
```

i.e. with kill active, `submit_live` returned a fill and reached the venue.
Reverting restores `PASS`.

## Coverage and sync

- `COVERAGE.md`: covered files/functions, bounds, assumptions, NOT covered.
- `SYNC_COMMIT`: base commit + file hashes the last green run describes.

## Second target (suggested)

`review_intent` cap enforcement for `submit_paper` -- positions / drawdown /
sector / day-target vetoes (paper-money correctness). Same method applies.

## Maintenance

For periodic review of this skill itself, see `/maintain-formal-verification-skill`.
