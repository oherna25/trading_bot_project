# Provenance

This file draws the line between event work and everything else. Judges read this first.

## Event window

Building period is Oct 27 to Nov 4, 2026. This repo started Oct 27 with commit
`22b5a9e` and grew through Nov 1. Nothing in this repo predates the event
window. The graded slice is the Nov 1 work.

Check it with `git log --reverse --format='%h %ad %s' --date=short | head -5`.

## What is new for this submission

The graded slice, all committed Nov 1, 2026:

- Spark audit (`8b9512f`). `bot/spark.py` is new. Spark proposes a direction
  and confidence. Local code checks the claim against computed GEX and
  journals both sides. `bot/muse_tools.py` adds `get_spark_audit`, and
  `bot/research.py` adds the chat route and the `live_brief` audit block.
  `tests/test_spark.py` covers the pure check and the offline audit path.
- Visual cards (`8a1f07d`). Trade cards carry badges, a confidence meter, a
  payoff-at-expiry diagram, and a spot-vs-flip gauge (`bot/tradecards.py`,
  `bot/dashboard.py`, `bot/theme.py`). Presentation over the cards engine; it
  makes no model claim and changes no gate.
- Premium-selling deployments (`21dd691`). `bot/deployments.py` registers
  four deployable selling strategies behind `STRATEGY_REGISTRY`;
  `bot/capital.py` keeps the collateral ledger; `bot/optionbook.py` rebuilds
  the contract book from tagged fills. `StrategyDeployment` persists through
  `bot/shapes.py`, `bot/store.py`, and `bot/persist.py` (`deployments.json`).
  Scheduled sessions run deployments through `bot/schedule.py`; chat, tools,
  and HTTP surfaces ship in `bot/research.py`, `bot/muse_tools.py`, and
  `bot/muse_http.py`. `tests/test_deployments.py` covers sizing, collateral,
  idempotency, lifecycle exits, expiry settlement, assignment, and feedback.
- Documentation and screenshots (`f9b12d3` onward). `PROVENANCE.md`,
  `submission.md`, the README tour, `GET /deployments/page`, and the nine
  screenshots in `docs/screenshots/`, captured from the running app.

## What was built Oct 27 to Oct 31, in window

The supporting engine was also built during the event, before the Nov 1
slice. It ingests chains and bars, computes GEX, VEX, DEX, charm, vanna,
ranks ideas, paper trades under risk gates, replays weeks, and journals
every step. Files like `bot/exposure.py`, `bot/feeds.py`, `bot/store.py`,
`bot/risk.py`, and `bot/replay.py` carry that engine. It is in-window work;
it is listed separately only because the Nov 1 slice is what this submission
asks judges to grade first.

Model plumbing was built in the same Oct 27 to Oct 31 span. `bot/models.py`
calls Muse Spark for the analyst role and Muse Wonder for review, with a
day budget and a deterministic fallback. `bot/localmodels.py` runs Muse
Glimmer and Muse Spark Open weights locally. The Nov 1 audit makes that
plumbing visible and graded instead of hidden behind a flag.

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
