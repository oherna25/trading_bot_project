# Provenance

This file draws the line between event work and everything else. Judges read this first.

## Event window

Building period is Oct 27 to Nov 4, 2026. This repo started Oct 27 with commit
`22b5a9e` and grew through Oct 31. The Meta central work on Nov 1 is the
submission slice.

Check it with `git log --reverse --format='%h %ad %s' --date=short | head -5`.

## What is new for this submission

These paths are the hackathon slice. Everything else is supporting context.

- `bot/spark.py` is new. Spark proposes a direction and confidence. Local code
  checks the claim against computed GEX and journals both sides.
- `bot/muse_tools.py` adds one tool, `get_spark_audit`. It calls the same audit
  path as chat.
- `bot/research.py` adds one chat route for spark audit and appends the audit
  block to `live_brief` when live models are on. Offline output is unchanged.
- `tests/test_spark.py` covers the pure check and the offline audit path.
- `PROVENANCE.md` and `submission.md` are new docs for this entry.

Run `git status --short` before you archive the repo. Commit the slice above so
the history shows it.

Nov 1 UI work sits outside the graded slice. Commit `8a1f07d` gives trade
cards badges, a confidence meter, a payoff-at-expiry diagram, and a
spot-vs-flip gauge (`bot/tradecards.py`, `bot/dashboard.py`, `bot/theme.py`,
plus README tour text and screenshots). It is presentation over the existing
cards engine. It makes no model claim and changes no gate.

## What existed before Nov 1

The options engine predates the Meta slice. It ingests chains and bars,
computes GEX, VEX, DEX, charm, vanna, ranks ideas, paper trades under risk
gates, replays weeks, and journals every step. Files like `bot/exposure.py`,
`bot/feeds.py`, `bot/store.py`, `bot/risk.py`, and `bot/replay.py` carry that
engine. They are not claimed as new model work. They are the base the audit
checks against.

Model plumbing also existed. `bot/models.py` already called Muse Spark for the
analyst role and Muse Wonder for review, with a day budget and a deterministic
fallback. `bot/localmodels.py` already ran Muse Glimmer and Muse Spark Open
weights locally. The Nov 1 change makes that plumbing visible and graded
instead of hidden behind a flag.

## Data and keys

Demo data is canned. `ingest_chain` and `ingest_bars` use vendor shaped
fixtures when called without arguments. No test needs network or keys.

`.env` is never committed. Copy names from `.env.example`. The Spark audit
runs in `OFF` mode without `META_MODEL_API_KEY` and journals that fact. With
the key set and a day budget, it calls `muse-spark-1.3` and journals receipts.
Keys never land in the journal or in error text.

## Limits

The free prose from Spark stays unverified. Only the direction check counts,
and the check value is one of `MATCH`, `CONTRADICTION`, `UNVERIFIED`, or `OFF`.
A match does not mean the trade is safe. Risk review still gates every submit
through `review_intent`. Live ordering ships disabled behind a signed action.

Repo is public at `https://github.com/oherna25/trading_bot_project.git`.
One representative submits once on the Cerebral Valley platform.
