# Improvements Log And Roadmap

This file records the October 2026 logic and speed pass.
It also lists what was deliberately left alone and what to build next.
Every shipped item below has a regression test in the suite.

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

* Exposure profiles are computed in one pass and cached per snapshot. Repeat GEX, VEX, and DEX calls on the same chain are dict lookups. Cache hits verify the contracts tuple by identity, so a rebuilt chain never reads a stale profile.
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
* Replay still fills at the signal bar close, and shock still reprices with static vendor gamma. Bar open-versus-close semantics are not provable from the stored data. Both are listed as features below instead of silent behavior changes.
* The HTTP server still serializes requests behind one lock. Splitting it needs a snapshot-read design and its own test pass.
* Test for pushed-on-failure changed on purpose. `test_push_alerts_failure_journals_without_raising` now expects an empty pushed list, because pushed should mean delivered.

## Suggested features, ranked by value

1. **Pre-trade gate as one service.** Enforce halt, kill, schema, size, day targets, and options profile inside both submit paths for every caller. Add `Signal.symbol` and confidence-scaled sizing. Today the gate still depends on each caller remembering to review first.
2. **Persistent paper account.** Store one RiskState per paper account in the bundle instead of rebuilding it from fills. Add equity curve, per-day PnL, and drawdown tracking with a maintained peak. The drawdown pause exists but nothing updates its peak today.
3. **Honest replay.** Fill on the next bar, mark open positions every bar, force-close at the end, and stamp each replay run with an id so reruns do not double-journal fills. Report daily equity deltas instead of cumulative PnL.
4. **Repriced shock.** Reprice held positions with Black-Scholes at shocked spot and IV, with quantity from taken trades. Label static-gamma output as an approximation until then.
5. **HTTP response caching.** Cache cards, board, and demo output keyed by snapshot id with ETag support. Add a small read-through cache in front of `query_trade_cards`.
6. **Diversified screeners.** Cap ideas and wheel candidates per symbol, expiry, and strike cluster with round-robin fill. One liquid symbol can still take every slot today.
7. **Reservation ledger for quota and model budget.** Reserve Alpha Vantage calls and model spend before the call and settle on receipt. Check-then-act still races across processes.
8. **Persistent local model server.** Replace per-call llama-cli spawns with one capped server process. Prompts move off argv, which also removes them from `ps` output.
9. **Feed health page.** Show per-feed latency, last success, quota use, and fallback depth from journal data. Operators currently infer feed health from brief failures.
10. **Async card reasons.** Generate trade-card reasons off the request path and cache by card stats. A cold dashboard view still waits on a model call today.
11. **Options analytics pack.** Add charm and vanna exposure beside GEX, VEX, and DEX from the same single pass. The cache makes each new profile nearly free on repeat reads.
12. **Backtest export.** Dump replay steps, fills, and benchmark to CSV or Parquet for external analysis.

## Known remaining risks

* `submit_live` cannot gate when a caller passes no state or limits. The new review covers the fully supplied path only.
* Paper day PnL rebuilt from fills omits unrealized moves on symbols without a current bar.
* The exposure cache keys on snapshot id plus contracts identity. Direct callers who mutate a contracts tuple in place would still see the old profile.
