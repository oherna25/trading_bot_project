# Improvements Log And Roadmap

This file records the October 2026 logic and speed pass.
It also lists what was deliberately left alone and what to build next.
Every shipped item below has a regression test in the suite.

## Modelled Greeks at ingest (2026-11-19)

* The default Tradier sandbox chain has bid/ask but no Greeks, so every exposure chart read flat and the wheel dropped every contract. `ingest_chain` now calls `fill_greeks` in `bot/greeks.py` before saving. Spot comes from put-call parity at the nearest expiry with a call and put mid (`implied_spot`), else the latest stored bar. Each contract's IV is solved from its bid/ask mid with `ivrank.implied_volatility`, then only the missing Greeks are computed: delta and gamma per $1, vega and vanna per vol point, charm as the delta change per calendar day, put delta negative. Vendor values are never overwritten.
* Time to expiry runs to 16:00 US/Eastern on the expiry date, so a same-day expiry before the close still gets Greeks. A bare last price is not a quote. A contract with no usable quote, a mid below intrinsic, or no time left stays unchanged.
* `OptionContract.greeks_modelled` marks contracts with at least one local Greek and round-trips through the bundle (old bundles thaw to False). `OptionContract.iv` is untouched, so IV history and the Alpaca/Yahoo backfill behave as before. The `snapshot_ingested` journal entry carries `modelled_greeks`. The chart page subtitle and the brief coverage lines say the Greeks are modelled, and the empty-state text now says no Greeks and no usable quotes were available.
* Covered by `tests/test_greeks.py` (finite-difference checks on every Greek's units and sign, round trip on a synthetic chain, ingest path). `test_live_brief_tolerates_bar_failure` now asserts `spot unknown` instead of `direction: flat`, since its fixture has quotes and is no longer flat.

## Demo page removed, chart views (2026-11-19)

* `/demo` and its nav entry are gone. The chat box ("Ask the desk") moved to the symbol dashboard (`/dashboard?underlying=SPY`), which no longer auto-refreshes so a typed question or chat reply is not wiped by the meta refresh; `/` still refreshes. `run_demo.py`, `bot/demo_seed.py`, the `marketwingman-demo` script, and the Docker and Make demo targets stay as the zero-key way to run the app, and now point at the dashboard. Shock, share, and deployment links that pointed at `/demo` point at `/dashboard?underlying=SYM`.
* `/chart?underlying=SYM&view=` serves a `CHART_VIEWS` registry in `bot/charts.py`: `gex` (default), `heatmap`, `vex`, `dex`, `charm`, `vanna`. An unknown `view` serves `gex`. The page shows stat tiles (spot, net GEX, zero gamma with its distance from spot, call and put walls, regime), pill tabs, and the chart. The ladder draws one row per strike, highest on top, from the contiguous window of up to `CHART_MAX_STRIKES` nearest spot (largest by magnitude when spot is unknown), with value labels, spot and flip markers interpolated between rows, and the largest walls outlined. The heatmap is a table of signed GEX by strike and expiry (nearest eight expiries, later ones folded into one column) from `compute_gex_grid`, which sums over expiries to `GexProfile.per_strike_exposure`.
* No chart carries a hard-coded color. SVG classes in `bot/theme.py` bind to the theme variables, so the ladder, trade-card payoff and spot-vs-flip charts, and the track-record equity chart read on both themes. `bot/video.py` inlines `chart_stylesheet()` and drops `CALL_COLOR` and `PUT_COLOR`. The symbol dashboard shows the GEX ladder above the digest.
* Covered by `tests/test_charts.py`, `tests/test_gex_engine.py` (grid invariant), `tests/test_dashboard.py`, and `tests/test_video.py`.
* The seeded zero-key launcher is gone too: `run_demo.py`, `bot/demo_seed.py`, the `marketwingman-demo` script, the compose `demo` service, and the `make demo` targets. The Docker image now starts the full app. Without a feed key the app serves an empty store until a snapshot is loaded.

## Claude backend and terminal UI (2026-11-19)

* Claude replaces Muse as the built-in model. `bot/claude.py` translates the chat-shaped payload every caller already builds into a Messages API request and sends it through the official `anthropic` SDK (optional `claude` extra, imported lazily; the base install stays standard library only). `MODEL_REGISTRY` pins `claude-opus-5-5` for the analyst, grounding, and reviewer roles at $4/$20 per MTok, with the reviewer at high effort. The key pair is now `ANTHROPIC_API_KEY` + `MODEL_DAY_BUDGET_USD`.
* Grounding uses the `web_search_20260209` server tool and resumes `pause_turn`. Requests opt into server-side refusal fallbacks, and receipts record the model that actually answered. Tool rounds replay the raw content blocks so thinking blocks stay valid.
* Card reasons label the backend `claude`, and `agent.propose_order` falls to Claude for the candidate pick when no Baseten key is set. The audit is now `claude audit` in chat and on pages; `spark audit` still routes, and the journal kinds (`spark_claim`, `spark_verdict`) and the `get_spark_audit` tool name are unchanged so stored journals and clients keep working.
* Removed: voice notes (`bot/voice.py`, `POST /voice`, spoken holdings by audio, the voice kill switch), generated card art (the `cards` chat/session flag, share and milestone art, `ImageOptions`), and SAM document segmentation. Claude has no endpoint for them and `META_MODEL_API_KEY` is no longer read. Share cards are caption-only and `/docread` is a single Claude vision read. Left alone: the Baseten path, the bring-your-own OpenAI-compatible path, and the local GGUF tiers are unchanged.
* The page shell in `bot/theme.py` is now a trading-terminal layout: a persistent left rail with grouped sections (Dashboard, Scanners, Options & flow, Trade, Account), a slim top bar with symbol search and status, a near-black palette, dense mono tables, and flat panels. Every page goes through `page()`, so all of them picked it up. Light theme, the skip link, `aria-current`, and a phone layout (the rail becomes a scrolling strip) are kept.
* Covered by `tests/test_claude.py` (request and reply translation, tool-round replay, refusal, paused grounding turn, missing SDK) plus the existing suite, which stubs the transport above the adapter.

## Spark audit names the serving model (2026-11-04)

* The audit no longer labels every claim `muse-spark-1.3`. Since the Wonder-first change, the analyst task runs on the premium tier, so the audited call is usually served by `muse-wonder-1.0`, or by `meta-models/muse-spark-open-1026` when a Baseten key routes the portable roles. `Signal` in `bot/shapes.py` now carries `model_id`; `spark_signal` stamps the serving spec and `local_signal` stamps the local receipt in `bot/research_analyst.py`. `bot/spark.py` labels the claim, the `spark_claim` journal entry, and the dashboard panel from that value, and shows "model not recorded" for journal entries written before the fix. Covered by `tests/test_spark.py`, including wire-level tests that assert the model in the outgoing request matches the label on the claim.

## Wonder-first premium tier (2026-11-04)

* `muse-wonder-1.0` now leads the premium tier for the hardest chat work. `bot/tiers.py` classifies `analyst`, `portfolio`, and `chart` as premium (with `review`/`coach`), so the tier order reviewer (Wonder) -> analyst (Spark) -> local applies to briefs, portfolio commands, and chart reads. `bot/research_analyst.py: analyst_backend` now honors `TierDecision.role` (it previously always called the Spark analyst spec), and `bot/chartread.py` routes through `select_model_tier("chart")` instead of a direct Spark backend check.
* Reasoning effort is a registry property: `ModelSpec.reasoning_effort` in `bot/models.py` is `high` for the reviewer/Wonder spec and `low` elsewhere, and `run_analyst` defaults to the spec effort (an explicit caller value still wins). Receipts now journal the real `role_name` at the tier-routed call sites. Draft tasks (narration, routing, card reasons) stay on Spark/local, debate keeps Spark bull vs Wonder bear, and grounding stays on Spark/Responses. Covered by updated tests in `tests/test_tiers.py`, `tests/test_models.py`, `tests/test_chartread.py`, and `tests/test_commands.py`.

## Docker full-app deploy (2026-11-04)

* The whole app now deploys via Docker, not only the demo. `docker-compose.yml` adds an `app` service (full `run_app` server on `/data/trading-bot`, 900s refresh, optional in-app `SESSION_SCHEDULE`), a `demo` profile on its own volume, and a one-shot `session` service (`jobs` profile) sharing the app volume; the server reloads bundle writes on mtime change, so session output lands without a restart. Keys pass through an optional `.env` file; `LEDGER_DIR` is pinned to `/data/ledger` and `TZ` is exposed because session schedules fire on local time.
* The image now runs as a non-root `app` user, sets `PYTHONUNBUFFERED`, copies `docs/icons` (with `PYTHONPATH=/app` so `bot/muse_http.py` finds the icon beside the copied source, not site-packages), health-checks `/healthz`, and `exec`s the demo default so signals reach Python. `.dockerignore` drops video/docs bulk from the build context.
* Verified live: image build, demo `/demo` + `/icon.png` + healthy status, compose `app` brief/chat/restart persistence, `session` one-shot against the shared volume. Contract pinned by `tests/test_docker_deploy.py`.

## Agentic use-case and Baseten (2026-11-03)

* Baseten is wired in. `bot/metareason.py` selects Baseten Muse Spark Open (`meta-models/muse-spark-open-1026`) after a user model and before the Meta key for card reasons; `bot/tradecards.py` treats `baseten` as a live reason backend.
* Graded candidate proposal. New `AgentProposal` in `bot/shapes.py` and `bot/agent.py: propose_order`: with `BASETEN_API_KEY` set and more than one deterministic candidate, `bot/baseten.py: generate_pick` asks Spark Open for one index or a skip. Local code validates index and rationale, journals `agent_proposal`/`agent_proposal_rejected`/`agent_fallback`, and falls back to annualized-return order. Sizing, collateral, profile, and `review_intent` stay deterministic.
* Bounded agent cycle. `run_agent_cycle` (MCP tool, `agent cycle` chat) runs observe, propose+act, verify as journaled `agent_step` phases plus one `agent_run` summary over the deployment book and track record.
* Covered by `tests/test_agent.py` plus pick/reason tests in `tests/test_baseten.py` and `tests/test_metareason.py`, including a deployment that opens the Baseten pick (not the top annualized candidate) through the paper risk gate.

## Grade fixes (2026-11-03)

* Earnings radar fetches for itself. `refresh_earnings` in `bot/earnings.py` pulls print dates via `cached_earnings` for symbols with no upcoming stored date and persists them; `earnings_text` and the `/earnings` page call it, while `earnings_rows` stays pure (pinned by test). The old radar only read dates cached as a side effect of trade-card requests, so it stayed dark with a valid FINNHUB_API_KEY. The empty state now names the next dated print instead of telling the operator to set a key that is already set. Live proof: QCOM 2026-11-04, RKLB 2026-11-09, NBIS 2026-11-12, NVDA 2026-11-17.
* Tradier chains span DTE bands. `_select_expiries` in `bot/feeds.py` picks the first expiry at or past 0, 7, and 21 DTE instead of the nearest two; SPY went from 0-1 DTE only to 0/7/24 DTE live. The deployment band (7-30 DTE) could never form a spread before this.
* Deployment fills stay out of the stock books. Fills tagged `option_position` are now skipped by `trackrecord._paper_fills`, `open_trade_size`, `paper_state_from_journal`, `_fill_books` (paper PnL), and `commands.open_book`. The first live deployment otherwise read as 5,760 SPY shares short at the 0.96 spread credit and a -$4M mark. Regression test in `tests/test_deployments.py`.
* Track record splits realized from unrealized (`pnl_breakdown` in `bot/trackrecord.py`): text and page now show closed-trade win rate, realized, unrealized on N open, and equity as their sum. The split matches `equity_curve` to the cent on the live journal.
* `explain_position` prose rewritten: one regime clause with spot-vs-flip side, total GEX labeled, biggest walls, then separate wheel and debate sentences. The old paragraph repeated the regime in a colon clause and stapled the debate verdict to the wheel line.
* First live deployment: `d1` put_credit_spread on SPY paper, 30x 745/740 exp 2026-11-10, credit 0.96, $12,120 collateral. It closes on the existing lifecycle rules; the stock track record's zero closed trades is unchanged by design (signal positions were not mass-closed to manufacture a win rate).

## Muse Image options (2026-11-03)

* `ImageOptions` in `bot/shapes.py` tunes one-off generation: `n`, `size` aspect hint, `response_format`, `output_format`, `reasoning_strength`, `enable_image_search`, `enable_web_search`, `enable_shell`, `user`. `generate_image` validates at the boundary and keeps the old `{model, prompt, n:1}` payload when options are None.
* `edit_image` posts JSON to `/images/edits` with one to ten `image_url` or `file_id` entries for retouch and compose. `refine_image` posts to `/responses` with `previous_response_id` chaining or `input_text` plus `input_image` parts, mapping tune knobs onto the `image_generation` tool. `parse_responses_image` reads the `image_generation_call` result plus response and call ids.
* `bot/cards.py` threads options through brief, signal, and milestone art with cost from `image_cost_usd`. New helpers: `render_infographic_art` with grounded defaults, `retouch_art` for edits, `refine_art` for continuations. Journal kinds: `infographic_card`, `retouch_card`, `refine_card`, plus `*_skipped` misses. Cache key includes options. `share_html` needed no change.

## Live toggle and confidence-gated broker sends (2026-11-01)

* `/live` is a plain toggle for live ordering: `enable_live(state, journal, now)` arms unless a halt/kill flag is active (then it journals `live_blocked`), under a small real-money warning, and `kill_switch` disarms. No signature, evidence, or checklist gate. `submit_live` still enforces the armed flag, halt/kill, and risk review; the formal gate check passes 7,600 cases on this property. See `enable_live_ui` and `kill_live_ui` in `bot/live_trade.py`.
* `DashboardSettings` carries `live_auto_send` (default off) and `live_min_confidence` (default 0.80), edited on `/settings` and `/live`, persisted in `settings.json`, validated 0-1 at the boundary. Below-threshold cards journal `live_skipped` and never reach the venue.
* `take_live_trade` sends one qualifying card through `submit_live` with option legs built from the card and sized by `size_for_card`; `maybe_auto_live` repeats the check across cards when auto-send is on, caps at 3 fills per run, and never raises. The formal gate check still exits `PASS` on this tree.
* Strategy feedback (`bot/strategy.py`: `parse_strategy`, `evaluate_strategy`, `POST /strategy`) and the visual builder (`bot/builder.py`, `GET /builder`) backtest guardrails over stored snapshots and bars through `replay_week` with a custom `signal_fn`.
* Feeds use a 10s timeout with a 60s GET cache, multi-symbol refresh runs concurrently, `SCAN_BROAD=1` widens the universe, and ideas rank top-15 with quote-aware quality adjustments that leave quoteless fixtures scoring as before.

## Strategy deployments: premium-selling agent (2026-11-01)

* `STRATEGY_REGISTRY` in `bot/deployments.py` registers four deployable premium-selling strategies: cash-secured put, covered call, put credit spread, and call credit spread. `StrategyDeployment` records persist in the store and in `deployments.json` in the bundle.
* `run_deployments` runs inside every scheduled session. Each run applies feedback from closed trades, manages open short options (50% profit target, 2x credit stop, close-DTE exit, expiry settlement with assignment journaling), then opens new sales ranked by annualized return on collateral.
* `bot/capital.py` keeps the capital ledger (deployed collateral, idle capital, utilization against a 70% target, 25% cap per position), and `bot/optionbook.py` rebuilds the contract book from tagged journal fills for per-strategy track records. Feedback resizes deployments between 0.5 and 1.25 and auto-pauses after three losing closes down 5% of the capital cap.
* Surfaces: chat deploy commands including plain English (`deploy sell puts on SPY paper capital 40000`), MCP tools (`list_strategies`, `deploy_strategy`, `list_deployments`, `run_deployments`, `get_deployment_trackrecord`), and HTTP routes (`GET/POST /deployments`, `POST /deployments/run`). Live deployments submit only through the armed `submit_live` path when a venue is supplied. Covered by `tests/test_deployments.py`.

## Bring-your-own model key (2026-11-01)

* `USER_MODEL_API_KEY` plus optional `USER_MODEL_BASE_URL` (default OpenAI) and `USER_MODEL_ID` (default gpt-4o-mini) routes the analyst and reviewer roles to any OpenAI-compatible chat API. The operator's key pays their bill, so no day budget is needed; receipts journal at zero cost. Grounding, voice, and image roles stay on the built-in key. See `read_user_model` and `select_backend` in `bot/models.py`.
* The same key can be supplied at runtime via the `set_user_model` MCP tool or the dashboard `/settings` model-key form (`clear_user_model` removes it). Runtime keys live in server memory only, clear on restart, take precedence over the env key, and are never persisted, journaled, or echoed back. Key-bearing tools are POST-only so keys never reach GET query strings or access logs.
* `llm_route` now goes through `select_backend`, so the day-budget gate applies to the chat router like every other model call. Previously it only checked key presence.
* Tier and settings readouts name the user model and its source (`user_model_summary`); `/healthz` reports `user_key_set` presence only.

## Agent report fixes (2026-10-31)

* Holdings and explain_position print flip and spot rounded to cents. Raw floats like 6.00294 no longer leak. See `_flip_bit` in `bot/holdings.py`.
* explain_position reports the saved holding and the paper book in separate sentences with a long/short side word. The old paragraph joined both books into one clause.
* Card IV rank and the get_iv_rank tool share one computation (`iv_rank` over `histories_for`). A live mismatch between two reads is temporal drift on the accruing intraday series, not a second formula. A lock-step regression test pins both surfaces to the same value on one store.
* Earnings radar and alert push need no code change. Both are env-gated and already report honestly: the radar empty state names FINNHUB_API_KEY plus the card-caching step, and /health reports push_providers.
* Wheel IV recordings carry a `wheel_screen` source like the ingest writer, so every `iv_recorded` entry shares one payload shape. The day-granularity store key stays: it keeps one sample per day instead of one per screen render. Mixed day/timestamp keys sort chronologically under ISO ordering, now pinned by a test.

## Shipped in this pass

**Correctness**

* Position average cost no longer corrupts on partial closes or flips. Day PnL counts each leg once. See `update_position` in `bot/risk.py` and the mirror fix in `_fill_books` in `bot/research.py`.
* Options sizing never exceeds the risk budget. A budget below one contract max loss now skips the intent instead of forcing one contract.
* Idea verticals always hedge on the payoff side. A bought call hedges at a higher strike and a bought put at a lower strike. The old nearest-strike rule could report a $20 max loss on a spread worth minus $520 at expiry.
* Option side strings are normalized in exposure math. `CALL`, `Put`, and `c`/`p` no longer fall into the put bucket. Unknown sides contribute nothing.
* IV backfill never prices a snapshot from a bar dated after it. The old fallback used a future bar and wrote lookahead IV into history.
* The replay SPY benchmark only uses bars inside the replay window. It reports n/a instead of importing out-of-window prices. The old code showed +100% from bars a month outside one test window.
* Alert dedup keys on symbol plus a stable fingerprint plus provider plus day. Stale alerts still dedup when the age text drifts by a minute. One symbol can no longer suppress another symbol first stale alert. A failed provider retries on the next run without resending the delivered one. Undelivered alerts no longer report as pushed.
* Paper trade steps now rebuild day PnL from journaled fills. A day-loss breach blocks the next paper trade. The old path reviewed every trade against a constant zero-PnL state.
* Live orders run the full risk review when state, limits, journal, and clock are supplied. Oversized or day-target-breaching intents raise before the venue call.
* Alert rule restore no longer collides generated ids with explicit ids.

**Speed**

* Exposure profiles are computed in one pass and cached per snapshot. Repeat GEX, VEX, DEX, CHARM, and VANNA calls on the same chain are dict lookups. Cache hits verify a content fingerprint of the contracts, so a mutated or rebuilt chain never reads a stale profile.
* Idea ranking precomputes strike indexes once per snapshot. The old code rebuilt a sorted strike set and rescanned the chain for every candidate. IV rank computes once per symbol instead of once per idea.
* Store bars stay sorted on insert. Latest-bar reads are O(1). Bar reads return the stored order without resorting. Snapshots stay chronological, previous-snapshot lookup uses a per-underlying index, and prune now caps bars too.
* IV inversion stops on bracket tolerance instead of a fixed 200 iterations.
* Journal merge dedups with a hashed key set. It was O(N times M) before.
* Bundle writes use per-process temp files with fsync. Concurrent writers no longer collide on one shared temp path. The server records its own saves, so the next request no longer reloads the whole bundle and drops unsaved settings. A failed reload no longer retries on every request.
* Request bodies are capped before reading. A bad or oversized Content-Length returns 400 or 413 instead of an unbounded read or a dropped connection.
* Feed GETs retry transient 429 and 5xx responses three times with backoff. Failures surface as `FeedUnavailableError` with no URL or key in the message.
* The `.env` file parses once per modification instead of on every lookup. The wheel screener parses each expiry once and tallies candidates in one pass.
* Tests no longer make a real network call from the dashboard test, and lock tests sleep 0.3s per feed attempt instead of 1.0s.

**Clarity**

* Walls render in dollars on every surface. The card facts grid, card stat list, and fallback card reason used raw share counts (`wall exposure -2352664864`); they now go through `compact_dollars` like the brief and share text already did (`wall exposure -$2.4B`).
* Confidence renders as a percent, never a decimal. One helper (`pct` in `bot/diff.py`) feeds the card meter and stat list, chat brief, spark audit, debate verdicts, builder, video brief, share text, and live status. Stored values, JSON payloads, and journal entries keep the raw 0-1 float. The settings and live pages take the auto-send threshold as a percent (`live_min_confidence_pct`, translated to 0-1 in `save_settings_merge`); the JSON settings API is unchanged.
* Trade cards explain themselves. The meter adds a plain word next to the percent (strong, moderate, weak), each card carries a one-line "in plain terms" summary, every fact has a hover explanation, and the cards page opens with a collapsed "new here?" glossary. Expert surfaces keep the full stat list and raw JSON.

**Baseten utilization**

* `BASETEN_API_KEY` now serves the portable chat roles, not just card reasons and agent picks. `portable_config` in `bot/models.py` resolves your own model first, then Baseten Spark Open through the inference chat API (`baseten_chat_config`, slug model ids only), then the built-in Meta key. Briefs, chat routing, spark claims, debate, the reviewer, coach, alert narrations, portfolio commands, and chart reads all flow through it with zero-cost journaled receipts, and the six role gates plus the tier selector accept the `baseten` backend. A deployed (non-slug) model id keeps the predict-URL reason/pick path and stays off chat. Grounding, voice, and image roles stay on the built-in key (provider-specific endpoints). `/health` reports `baseten_key_set`, the tier readout and settings summary name the Baseten model, and no surface echoes the key.

**Demo and deploy**

* The demo chat box can finally show the hero feature. It never sent `live_models`, so `spark audit` read OFF even with a key set. It now has a live-models toggle, pre-checked when a chat key is configured, and posts the flag with each question. Zero-key behavior is unchanged (toggle off, deterministic answers).
* Card reasons no longer hide behind the HTTP cache forever. The `/cards` response key did not change when the async reason worker landed a model reason, so the first-paint fallback page was served until the snapshot changed. The key now includes `reason_cache_version`, bumped on every reason store; a regression test pins cache-hit then refresh-after-reason behavior.
* Deployment is one command on both paths. A `Dockerfile` (`python:3.12-slim`, stdlib-only runtime, `/data` volume, `$PORT` honored) plus `make docker-demo`, and the non-Docker `pip install .` + `marketwingman-demo` path; both verified end to end (container healthz + `/demo` 200, clean-venv console script). The README judges section gained the one-key spark-audit step and a Deploy subsection covering both.

**Measured results**

Benchmark script lives at `/tmp/bench_bot.py`. It uses a 480-contract synthetic chain, 500 stored bars, and 50 snapshots. Numbers are medians of 5 runs on this machine.

| Path | Before | After |
|---|---|---|
| GEX x20 on one snapshot | 2.00 ms | 0.00 ms |
| GEX+VEX+DEX x20 | 3.72 ms | 0.01 ms |
| rank_ideas x5 | 14.92 ms | 3.49 ms |
| latest bar x2000 | 22.52 ms | 0.11 ms |
| bar reads x500 | 5.51 ms | 0.24 ms |
| previous snapshot x2000 | 3.06 ms | 0.75 ms |
| IV inversion x200 | 13.10 ms | 1.81 ms |

The exposure rows measure repeat calls on one snapshot. That is the production pattern for briefs, alerts, and cards. First-call cost is unchanged except when all three profiles are requested together.
Full suite went from 45.0s to 17.2s on this machine. Test count went from 776 passed to 793 passed with the same 3 sandbox skips.

## Deliberately left alone

* Idea direction still follows the regime and flip rule for every idea. Two existing tests codify that contract (`test_rank_ideas_direction_follows_flip`, `test_brief_direction_matches_ideas_and_regime_without_flip`). Changing it is a product decision, not a bug fix.
* Replay still fills at the signal bar close, and shock still reprices with static vendor gamma. Bar open-versus-close semantics are not provable from the stored data. Both are listed as features below instead of silent behavior changes. Both shipped in the roadmap pass: replay fills on the next bar (item 3) and shock reprices positions with Black-Scholes (item 4).
* The HTTP server still serializes requests behind one lock. Splitting it needs a snapshot-read design and its own test pass.
* Test for pushed-on-failure changed on purpose. `test_push_alerts_failure_journals_without_raising` now expects an empty pushed list, because pushed should mean delivered.

## Shipped from the roadmap

All 12 items below are done. Final state: 858 tests pass with 3 skipped, plus clean `ruff check` and `mypy` runs.

* **Pre-trade gate as one service (item 1).** `review_intent` now runs inside `submit_paper` and `submit_live`, so no caller can skip the gate. `submit_paper` raises `IntentRejectedError` on rejection. `submit_live` fails closed when state, limits, journal, or clock is missing. Both accept `profile` and `view`. All five paper call sites (replay open and close, paper trade step, portfolio close, roll reopen) were migrated to the single call. `Signal` gained `symbol`, set by every bot constructor, with the snapshot prefix kept as fallback. Sizing is confidence-scaled: budget times clamped confidence, with a skip when the scaled budget is zero. Intended contract changes: sizing tests now expect scaled sizes, `test_paper_fill_journals_combo_legs` passes an options-ok profile, and the live-config test supplies full gate inputs.
* **Persistent paper account (item 2).** New frozen `PaperAccount` holds one `RiskState` plus total PnL, per-day PnL, an equity curve, peak equity, peak day PnL, and the day the state belongs to. Pure functions seed, roll, and record it: roll resets day PnL and halt on a new day while keeping kill and totals, and every record maintains the peaks and the one-entry-per-day curve. Accounts persist in the bundle as `paper_accounts.json` and survive `restore`. `paper_trade_step` takes an optional store, seeds from the journal once, then reuses the stored state and feeds the maintained peak into the submit view so the drawdown pause can fire. Both bot callers pass the store. `paper_drawdown` reports dollars and fraction off the peak.
* **Honest replay (item 3).** Signals now stage a pending order that fills at the next bar open instead of the signal bar close. Open positions mark to every bar close with halt checks on the marked move. Day PnL and halt reset on each new day with a journaled roll. Anything still open force-closes at the last bar close, and pending orders with no later bar expire journaled. Closes are exempt from the day-target veto so exits and the force close always proceed, while halt, kill, schema, size, and options checks still apply. Every run carries a deterministic run id stamped on fills and steps, and `replay_response` refuses to double-journal a rerun. Steps report daily PnL, cumulative total, and equity. Evidence counts clean days off daily totals and drawdown off the equity curve. The report shows an equity-by-day line plus total PnL. Intended contract changes: step actions gained pending, force-close, vetoed, and expire outcomes, and the evidence test now expects per-day values.
* **Repriced shock (item 4).** New public `bs_price` in `ivrank.py` reuses the existing Black-Scholes core with expiry and input edge handling, plus a public `expiry_years` helper. `shock_text` reprices every open position for the shocked underlying with Black-Scholes at the shocked spot and shocked IV, taking quantity from journaled fills via `open_book`. IV falls back from stored contract IV to inversion from mid. The default IV rule lifts IV as spot falls and accepts an explicit `iv_shock_pct` override. Stock legs reprice linearly. Positions without IV or expiry and positions in other symbols are counted as skipped. The flip and walls lines are labeled a static-gamma approximation.
* **HTTP response caching (item 5).** `query_trade_cards` gained a read-through cache keyed by query text, snapshot ids plus chain fingerprints, bars, profile, IV history, earnings, and day. Only the deterministic fallback path caches, so live model calls always recompute. Hits journal `ideas_cached` and return a copy. `/cards`, `/cards.json`, `/board`, and `/demo` responses cache under keys built from the same input fingerprints plus settings, profile, and a minute bucket, and serve ETag headers with 304 on `If-None-Match`, which the server threads through from request headers. `/board/live` stays uncached. A `tests/conftest.py` autouse fixture clears both caches between tests.
* **Diversified screeners (item 6).** New shared `diversify` helper in `ideas.py` fills quality-sorted candidates round-robin across symbols in first-seen order, with caps of 5 per symbol, 5 per expiry, and 2 per strike cluster, plus the existing per-direction cap. Strike clusters group strikes to the nearest point within a symbol. Blocked heads drop because caps only tighten. `rank_ideas` and `screen_premium` both select through it. Intended contract changes: multi-symbol idea order now interleaves instead of sorting purely by score, and the two cap tests spread fixtures across expiries and symbols so the direction cap still binds.
* **Reservation ledger for quota and model budget (item 7).** New `bot/ledger.py` keeps one file per fact in a ledger directory. Quota claims are atomic O_EXCL files per feed, symbol, and day. Budget holds and settled spend are per-day files with accept and settle decisions serialized on an advisory lock file, so concurrent processes cannot over-accept. Holds expire by TTL to bound crash leaks, other-day files prune lazily, and settling the same receipt twice is a no-op via deterministic spent names. `ingest_bars` reserves before quota-feed calls and releases plus journals on failure, keyed off a new `FeedContext.ledger_dir` that defaults to the legacy journal check. `select_backend` reads `LEDGER_DIR` from env, sweeps the journal unsettled receipts into settled spend, then reserves a conservative per-role estimate and journals the hold. `LEDGER_DIR` is documented in `.env.example`.
* **Persistent local model server (item 8).** Per-call `llama-cli` spawns are gone. `LocalServer` starts one `llama-server` per weights file on 127.0.0.1 with capped context and threads, waits for health, and serves every local call over HTTP with prompts in the POST body, one request at a time. A registry reuses the process across calls and restarts dead ones. `run_local` keeps its shape and error contract, reading `LLAMA_SERVER_BIN` plus optional context and thread tunables. `.env.example` and the README name the new binary. Verified end to end against a stub server over real localhost HTTP: reuse, body transport, prompt-free process args, and stop.
* **Feed health page (item 9).** New `feed_health` in `feeds.py` derives per-feed status from journaled outcomes: success and failure counts, last success time, last and average latency, fallback depth as consecutive same-kind failures before each success, today quota reservations and releases, and the last error. `ingest_chain` now journals latency like bars already did. `/health` carries the map under `feeds`, and a new `/feeds` route renders it as an HTML table with an OpenAPI entry.
* **Async card reasons (item 10).** Card builds no longer block on live reasons. On a reason-cache miss the request path returns the fallback reason immediately and enqueues one job per card-stats key, journaling the queueing. `drain_reason_queue` generates reasons inline for scheduled sessions, which run it after every session core, and a daemon worker started by `serve` drains in the background. Completions land in the existing stats-keyed reason cache, invalidate the card response cache, and journal `trade_card_reason`, while failures journal `reason_failed` without raising. Intended contract changes: meta-backed builds serve fallback reasons until the first drain, and the reason tests now build, drain, and rebuild.
* **Options analytics pack (item 11).** `OptionContract` carries optional charm and vanna, and `compute_exposures` accumulates `CharmProfile` and `VannaProfile` in the same single pass with the same cache, exposed via `compute_charm` and `compute_vanna`. Tradier and Market Data adapters pass the Greeks through, and the bundle persists them. Chat routes `charm` and `vanna` briefs with units and so-what lines matching the VEX and DEX briefs.
* **Backtest export (item 12).** New `replay_export` dumps one CSV section per call: steps with daily and cumulative PnL plus equity, fills with prices and legs, and a per-step benchmark joining SPY closes and equity against strategy equity. `GET /replay.csv?underlying=X&section=` serves each as a download with an OpenAPI entry. The report and export share one scratch-run preamble.

## Suggested features, ranked by value

1. **DONE: Pre-trade gate as one service.** See Shipped from the roadmap.
2. **DONE: Persistent paper account.** See Shipped from the roadmap.
3. **DONE: Honest replay.** See Shipped from the roadmap.
4. **DONE: Repriced shock.** See Shipped from the roadmap.
5. **DONE: HTTP response caching.** See Shipped from the roadmap.
6. **DONE: Diversified screeners.** See Shipped from the roadmap.
7. **DONE: Reservation ledger for quota and model budget.** See Shipped from the roadmap.
8. **DONE: Persistent local model server.** See Shipped from the roadmap.
9. **DONE: Feed health page.** See Shipped from the roadmap.
10. **DONE: Async card reasons.** See Shipped from the roadmap.
11. **DONE: Options analytics pack.** See Shipped from the roadmap.
12. **DONE: Backtest export.** See Shipped from the roadmap.

## Known remaining risks

* `submit_live` cannot gate when a caller passes no state or limits. The new review covers the fully supplied path only. Retired by roadmap item 1, which fails closed without gate inputs.
* Paper day PnL rebuilt from fills omits unrealized moves on symbols without a current bar. Retired by roadmap item 2, which persists the account instead.
* The exposure cache keys on snapshot id plus contracts identity. Direct callers who mutate a contracts tuple in place would still see the old profile. Retired: cache hits now verify a content fingerprint (length plus fresh per-contract hashes, since tuples cache their own hash), so any mutation misses and recomputes.
