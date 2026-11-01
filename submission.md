# Submission

## Title

Options desk with a Muse Spark audit block

## Track and statement

Track: Indie Developer. Statement: Build for Your World.

I trade options from free tier feeds and journal everything. A model thesis
alone never told me when it disagreed with my own walls and flip. This entry
fixes that. Spark proposes. The engine grades the claim. Both land in the
journal.

## What Meta does here

Spark is not decoration. It sits in the main brief path.

- Analyst calls `muse-spark-1.3` with one tool, `get_gex_profile`. Code in
  `bot/research_analyst.py` forces the call, parses `DIRECTION` and
  `CONFIDENCE`, and journals each receipt.
- The new audit in `bot/spark.py` checks the model direction against the
  engine direction from `resolve_direction`. Result is `MATCH`,
  `CONTRADICTION`, `UNVERIFIED`, or `OFF`. Free prose never counts.
- `live_brief` in `bot/research.py` appends the audit block when live models
  are on. Ask for a spark audit any time with `get_spark_audit` or by typing
  spark audit in chat.
- Reviewer calls `muse-wonder-1.0` for a go live risk note. It advises only.
  Voice transcription and image generation stay on the built in key and are
  out of the audit path.
- Local path runs `muse-glimmer-3b` and `muse-spark-open-8b` weights through
  `bot/localmodels.py` when GGUF paths are set. No key needed for that tier.

Without a key the app still runs. The audit reports `OFF` and the
deterministic engine answers. That is by design.

## New work in the event window

Built Oct 27 to Nov 4. The Nov 1 slice is the Meta center.

- `bot/spark.py`, `get_spark_audit` in `bot/muse_tools.py`, spark route and
  `live_brief` block in `bot/research.py`, `tests/test_spark.py`.
- `PROVENANCE.md` and this file.

The engine underneath, exposure math, feeds, store, risk gates, replay, came
first in the same window and is supporting context. See `PROVENANCE.md` for
the split.

## How to run

```bash
python -m venv .venv
.venv/bin/pip install pytest ruff mypy
.venv/bin/python -m pytest tests/ -q
```

Try the audit offline. It journals `OFF` and still answers.

```bash
.venv/bin/python -c "from datetime import datetime, timezone; from bot.journal import Journal; from bot.store import SnapshotStore; print('store ok', SnapshotStore() is not None)"
```

With keys, copy `.env.example` to `.env`, set `META_MODEL_API_KEY` and
`MODEL_DAY_BUDGET_USD`, then run the app once and read the spark audit lines
in the brief.

## Demo video

Link: TODO add the 3 minute link before Nov 4, 9:00 PM ET.

Plan for the recording. One case, SPY, live models on. Show the brief, then
the spark audit block, then the journal tail with `spark_claim` and
`spark_verdict`. Show one mismatch on purpose so judges see `CONTRADICTION`
hold. Show the offline run second so they see `OFF` still works. Only ship
what is in this repo. That keeps the video honest.

## Why this fits the rubric

Impact: a retail trader sees when the model and the tape disagree before
sizing. That check runs on every brief, not in a side tab.

Use of Meta tooling: Spark writes the thesis, Wonder writes the risk note,
local Spark Open covers the no key case. The check itself is local code, so
the model cannot move the gate.

Depth: one pass exposure calc, bisect indexed store, gated paper venue with
halt drills, no lookahead replay. Journal or it did not happen.

Demo: one command, one ticker, one audit block. The verdict is a single word
a judge can read from the back row.

## Rules checklist

- Public repo. Team size 1. New work Oct 27 to Nov 4.
- Uses Meta Model API and Meta open weights. No medical advice, no generic
  wrapper, no banned content.
- Video shows only code in this repo. Contributors and new versus old work
  are listed here and in `PROVENANCE.md`.
- One representative submits once on Cerebral Valley by Nov 4, 9:00 PM ET.

## Contributors

Solo builder. Representative: the repo owner.
