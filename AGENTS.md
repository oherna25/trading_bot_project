# AGENTS.md — Trading Bot

Research-first options trading system. It ingests chains and bars from
free-tier market APIs, computes its own GEX/VEX/DEX exposure from stored
snapshots, answers research questions in chat, and journals every signal,
intent, and fill. Paper trading works. Live ordering ships disabled behind
a signed action.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

CI runs `python -m pytest tests/ -q` on Python 3.11 and 3.12
(`.github/workflows/pytest.yml`). `make test` runs the same suite.

## Operating rules

- Tests are fully offline. `ingest_chain` and `ingest_bars` use canned
  vendor-shaped fixtures when called without arguments. Never add a test
  that needs network or keys.
- Never read `.env`. Copy key names from `.env.example`.
- Secrets never appear in code, journals, or error messages.
- `data/` holds the local JSON bundle and is gitignored. Every scheduled
  run persists there.
- Every signal, intent, fill, and model call lands in the journal.
  A missing journal write is a bug.

## Architecture

One-way data flow, everything journaled:

```text
market APIs -> feeds -> store -> exposure -> research -> risk -> paper venue
```

| Layer | Module | Entry points |
|---|---|---|
| Types | `bot/shapes.py` | Frozen dataclasses. Start here for any data shape |
| Feeds | `bot/feeds.py` | `FEED_REGISTRY`, `ingest_chain`, `ingest_bars`, `live_context` |
| Store | `bot/store.py` | `SnapshotStore`, `read_latest`, `prune`, `restore` |
| Exposure | `bot/exposure.py` | `compute_gex`, `compute_vex`, `compute_dex` |
| Research | `bot/research.py` (~1200 lines) | `route_chat`, `handle_chat_query`, `live_brief`, `scheduled_brief` |
| Risk | `bot/risk.py` | `build_intent`, `review_intent`, `submit_paper` |
| Autonomy | `bot/autonomy.py` | `check_promotion`, `enable_live`, `kill_switch` |
| HTTP/MCP | `bot/muse.py` (~2100 lines) | `TOOL_REGISTRY`, `route_http`, `serve`, `Harness` |
| Ideas/cards | `bot/ideas.py`, `bot/tradecards.py`, `bot/wheel.py` | `rank_ideas`, `query_trade_cards`, `screen_premium` |
| Replay | `bot/replay.py` | `replay_week`, `paper_evidence_from_journal` |
| Persist/job | `bot/persist.py`, `bot/schedule.py` | `save_bundle`, `load_bundle`, `run_scheduled_session` |
| Models | `bot/models.py`, `bot/tiers.py`, `bot/localmodels.py` | `MODEL_REGISTRY`, `select_model_tier`, `run_local` |

Single-purpose surfaces: `charts`, `alerts`, `customalerts`, `dashboard`,
`debate`, `coach`, `commands`, `voice`, `cards`, `news`, `holdings`, `shock`,
`diff`, `video`, `profile`, `slash`, `venue`, `config`, `journal`,
`metareason`, `morning`, `earnings`, `explain`, `onboarding`, `trackrecord`,
`theme`. `baseten` is dormant.

Program entry points: `run_app.py` (brief once, then serve HTTP),
`run_session.py` (one scheduled session). Library server: `bot.muse.serve`.

## Conventions

- Shared types are frozen dataclasses in `bot/shapes.py`. Add new shared
  types there, never inline.
- Registries over branching: `FEED_REGISTRY`, `MODEL_REGISTRY`,
  `TOOL_REGISTRY`, `SLASH_COMMANDS`.
- Chat and tools take `live_models`/`grounding`/`cards` flags, all default
  `False`. The offline deterministic path must always work.
- Trade cards need a saved operator profile. Without one every card
  surface asks for profile setup.
- `review_intent` gates every submit path. Options ideas and intents also
  need `options_ok` in the profile.
- Every model call journals a `ModelReceipt` and honors the day budget.
  Missing keys, budget, or weights select the deterministic fallback.

## Tests

- Mirror module names: `tests/test_<module>.py`.
- Focused run: `.venv/bin/python -m pytest tests/test_risk.py -q`.
- Full suite is about 800 tests in about 20 seconds.

## Docs map

- `README.md` is the full reference. It is long, so search it.
- `init.md` holds the plan and phase evidence.
- `overview.md` holds the design pseudocode.
- `diagrams/` holds Mermaid sources.
- `IMPROVEMENTS.md` logs what changed and what is next.

## Gotchas

- `live_brief` raises `FeedUnavailableError` when every chain feed fails.
  Bars are best effort.
- Sandbox Tradier chains are delayed about 15 minutes and carry no Greeks.
- Ideas expire at least one day out. No 0DTE.
- Naming a ticker with no stored snapshot errors instead of falling back.
- Feed fallback order is fixed. Chains try Tradier then Market Data. Bars
  try Alpaca, Finnhub, Yahoo, then Alpha Vantage.
