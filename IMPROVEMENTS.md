# Improvements Log And Roadmap

This file records the October 2026 logic and speed pass.
It also lists what was deliberately left alone and what to build next.
Every shipped item below has a regression test in the suite.

## Live toggle and confidence-gated broker sends (2026-11-01)

* `/live` arms and disarms live ordering through the unchanged `enable_live` gate (signature, full checklist, journaled paper evidence, clean halt/kill state) and the `kill_switch`. Misses journal `live_blocked` with the reason. See `enable_live_ui` and `kill_live_ui` in `bot/live_trade.py`.
* `DashboardSettings` carries `live_auto_send` (default off) and `live_min_confidence` (default 0.80), edited on `/settings` and `/live`, persisted in `settings.json`, validated 0-1 at the boundary. Below-threshold cards journal `live_skipped` and never reach the venue.
* `take_live_trade` sends one qualifying card through `submit_live` with option legs built from the card and sized by `size_for_card`; `maybe_auto_live` repeats the check across cards when auto-send is on, caps at 3 fills per run, and never raises. The formal gate check still exits `PASS` on this tree.
* Strategy feedback (`bot/strategy.py`: `parse_strategy`, `evaluate_strategy`, `POST /strategy`) and the visual builder (`bot/builder.py`, `GET /builder`) backtest guardrails over stored snapshots and bars through `replay_week` with a custom `signal_fn`.
* Feeds use a 10s timeout with a 60s GET cache, multi-symbol refresh runs concurrently, `SCAN_BROAD=1` widens the universe, and ideas rank top-15 with quote-aware quality adjustments that leave quoteless fixtures scoring as before.

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
