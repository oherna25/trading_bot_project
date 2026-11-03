# Submission

## Title

TradingWingManBot: a premium-selling options agent, graded by its own engine

## Track and statement

Track: Indie Developer. Statement: Build for Your World.

I trade options from free tier feeds and journal everything. A model thesis
alone never told me when it disagreed with my own walls and flip. This entry
fixes that. Spark proposes. The engine grades the claim. Both land in the
journal.

The second half of the entry is the agent that acts on the graded research.
Pick a registered premium-selling strategy and a capital cap, and every
scheduled session manages open short options to a profit target, stop, or
expiry settlement, then sells new premium sized by a collateral ledger.
Closed trades feed back into sizing, and a losing deployment pauses itself.

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
- Deployments stay deterministic on purpose. Models write theses, reviews,
  and reasons around the agent; the selling, sizing, and exit rules in
  `bot/deployments.py` are local code that a model cannot override.

Without a key the app still runs. The audit reports `OFF` and the
deterministic engine answers. That is by design.

## New work in the event window

Everything in this repo was built Oct 27 to Nov 4, 2026, starting from
commit `22b5a9e` on Oct 27. The graded slice is the Nov 1 work:

- Spark audit (`8b9512f`): `bot/spark.py`, `get_spark_audit` in
  `bot/muse_tools.py`, spark route and `live_brief` block in
  `bot/research.py`, `tests/test_spark.py`.
- Visual cards (`8a1f07d`): badges, confidence meter, payoff diagram, and
  spot-vs-flip gauge over the existing cards engine.
- Premium-selling deployments (`21dd691`): `STRATEGY_REGISTRY` in
  `bot/deployments.py` (cash-secured put, covered call, put and call credit
  spreads), the collateral ledger in `bot/capital.py`, the contract book in
  `bot/optionbook.py`, persisted `StrategyDeployment`s, the scheduled-session
  runner, lifecycle management with expiry settlement and assignment, and
  deterministic feedback that resizes or pauses a deployment. Covered by
  `tests/test_deployments.py`.
- Documentation (`f9b12d3` onward): `GET /deployments/page`, the README
  visual tour with nine screenshots captured from the running app, this
  file, and `PROVENANCE.md`.

The engine underneath (exposure math, feeds, store, risk gates, replay) was
built Oct 27 to Oct 31, in the same window. See `PROVENANCE.md` for the
path-by-path split.

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

Link: ACTION FOR THE REPRESENTATIVE. Upload `docs/submission-video/submission.mp4`
to YouTube or Loom and paste the public link here before Nov 4, 9:00 PM ET.
The file is rendered and compliant at 115 seconds; only the upload and this
line remain. Do not submit with this action line still in place.

Rendered artifact (115s, 1920x1080, narrated, burned-in captions):
`docs/submission-video/submission.mp4`, silent cut `submission-silent.mp4`,
contact sheet `docs/submission-video/contact.jpg`.
Regenerate with all features on: `.venv/bin/python scripts/make_submission_video.py`
(write-only project: add `--write-only`; deterministic OFF cut: add `--offline`).
Generation runs a live grounded Spark audit first (`live_models=True, grounding=True`)
and refuses to render if no model receipt was journaled. It persists the bundle
journal, so `spark_claim` + `spark_verdict` + `model_receipt` are on record.
Composition source: `build_submission_brief` in `bot/video.py`.
Scenes: hook 6s · SPY gamma 12s · Spark audit duel 16s (this run: MATCH, model short
confidence 0.78 vs engine short) · top card 14s · safety gates 11s · replay vs
buy-and-hold 13s · shock -2% 11s · MCP tools 12s (50 tools, one registry, live
`tools/call` reply in-scene) · wheel screen 13s (broad scan, 5 ranked candidates
led by NBIS SELL_PUT 4.52%) · outro 7s. Live ordering is the one feature that stays
off — by design; the safety scene shows the gate that enforces that.

Narration: timed script in `docs/submission-video/narration.txt` (one spoken line per scene,
in `VideoScene.narration`). `narration-muse.txt` is the same script drafted live by
`muse-spark-1.3` through `bot.models.run_analyst`. Muse cannot voice it: `MODEL_REGISTRY`
has no text-to-speech role — `muse-voice-transcribe-1.0` is speech-to-text only.
The shipped `submission.mp4` voices the Muse draft with the macOS system voice
(muxed via `--narration-audio`); record `narration.txt` yourself and re-mux for a human track.

Plan for the recording (3:00, one case, SPY, live models on).
0:00 Open `/demo`: hero regime pill, spot vs flip, Spark audit panel. Open
with the disclosure: everything in this repo was built Oct 27 to Nov 4.
0:25 Click "Ask the desk" → `spark audit`: MATCH/CONTRADICTION verdict pill,
model vs engine duel, journal tail with `spark_claim` + `spark_verdict`.
1:00 Show one mismatch on purpose so `CONTRADICTION` holds; take the top
card as a paper trade; show the gated fill in the journal.
1:35 The agent: in chat, `deploy sell puts on SPY paper capital 40000`, then
`run deployments`. Open `/deployments/page` and show the short put, its
collateral, and the utilization bar. Advance the book and show a position
closing at its profit target, with realized PnL per strategy.
2:15 Replay vs buy-and-hold, then a -2% shock repricing broken walls.
2:35 MCP: `tools/list` then `tools/call get_gex_brief` over `POST /mcp`.
2:50 Offline run: audit reads `OFF`, deterministic engine still answers.
Only ship what is in this repo. That keeps the video honest.

## Why this fits the rubric

Impact: a retail trader sees when the model and the tape disagree before
sizing, and a premium seller stops hand-managing every short contract. The
audit check runs on every brief, and the deployment agent runs on every
session. Both address work the operator used to do by hand, daily.

Use of Meta tooling: Spark writes the thesis, Wonder writes the risk note,
local Spark Open and Glimmer cover the no key case on device. The checks
themselves are local code, so no model can move a gate, size a position, or
place an order.

Depth: one pass exposure calc, bisect indexed store, gated paper venue with
halt drills, no lookahead replay, a collateral ledger, and a contract book
rebuilt from journaled fills. 952 offline tests. Journal or it did not
happen.

Demo: one ticker, one audit verdict a judge can read from the back row, then
one chat command that deploys a selling strategy and a book page that proves
it acted.

## Live evidence (2026-11-03, from the running server)

- Deployment `d1` is live on paper: `put_credit_spread` on SPY, opened by
  `run_deployments` — 30 SPY 745/740 put spreads expiring 2026-11-10,
  credit 0.96, $12,120 collateral against the $50,000 cap. It closes on
  the journaled lifecycle rules (50% profit target, 2x credit stop,
  close at 5 DTE, expiry settlement); closed trades land in
  `get_deployment_trackrecord`. Check it with `list_deployments`.
- The earnings radar lights up on its own now: QCOM 2026-11-04,
  RKLB 2026-11-09, NBIS 2026-11-12, NVDA 2026-11-17, each with regime,
  spot, and nearest wall. It fetches and persists print dates instead of
  waiting for a trade-card request to cache them as a side effect.
- The track record page splits realized from unrealized: win rate counts
  closed trades only, and equity is labeled realized + unrealized marks
  on open positions. Deployment option fills no longer pollute the stock
  paper book (they were counted as thousands of shares at the spread
  credit before this fix).
- The Tradier chain now spans DTE bands (nearest, >=7, >=21 DTE) instead
  of the nearest two expiries, so SPY carries 7-30 DTE contracts and the
  deployment band can actually form. Before this, SPY chains were 0-1
  DTE only and no spread could ever open.

## Human actions before submitting (not code)

1. Claim the $150 model credits per person by Nov 4, 9:00 PM ET.
2. Upload `docs/submission-video/submission.mp4` to YouTube or Loom and
   replace the demo-video action line above with the public link.
3. If the submission includes a live link: set `MUSE_AUTH_TOKEN` in
   `.env`, restart `run_app.py`, and put the server behind Tailscale
   Funnel (the server currently binds 0.0.0.0 without a token and prints
   that warning at startup). Otherwise remove the live link.

## Local models: the next advantage

Local Glimmer and Spark Open already run on device through a persistent
`llama-server`, at zero API cost with journaled receipts. Ranked next uses,
none of which a single cloud model matches on cost or privacy:

1. Dual audit. Local Spark Open proposes first, hosted Spark challenges, the
   engine grades both, and disagreement journals. Two independent claims per
   brief for the price of one cloud call.
2. Deployment diary. A local narration per deployment per session. Runs for
   every deployment every day because there is no per-call bill, and
   holdings never leave the machine.
3. Continuous triage. The local alert judge already runs offline; extend it
   to open deployment positions on every refresh tick, a cadence no cloud
   budget survives.
4. Strategy mining. A local model proposes guardrail variants from closed
   trades each night; replay scores them; only verified variants reach the
   operator. Private journal data stays on the device.
5. A fine-tuned Glimmer that writes card reasons in the operator's style,
   trained on journaled reasons. That is an open-weights entry of its own.

## Rules checklist

- Public repo. Team size 1. New work Oct 27 to Nov 4.
- Uses Meta Model API and Meta open weights. No medical advice, no generic
  wrapper, no banned content.
- Video shows only code in this repo. Contributors and new versus old work
  are listed here and in `PROVENANCE.md`.
- One representative submits once on Cerebral Valley by Nov 4, 9:00 PM ET.

## Contributors

Solo builder. Representative: the repo owner.
