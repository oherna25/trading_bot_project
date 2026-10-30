## Goal

**Reader.** You are the operator and sole user of this system (assumption from your ideas list).
**Outcome.** You get a research-first trading system that grows into gated autonomy one verified phase at a time (plan decision).
**Maintainer.** The next engineer inherits one phase per box with named evidence per box (plan decision).
**Scope.** The system covers data plus signals plus paper trading plus a live gate (user idea).
**Non-goal.** The plan excludes high-frequency execution and multi-broker support in v1 (assumption).

## Success Criteria

- Research mode returns a GEX profile in chat with per-strike numbers and a total (user idea).
- Paper mode journals every OrderIntent with its risk numbers before submission (user idea).
- The daily loss halt triggers on a simulated breach and blocks further intents that day (user idea).
- The live adapter ships default off and needs an explicit operator action to enable (plan decision).
- The backtest replay reproduces paper results within a stated tolerance on the same snapshots (assumption).
- The research loop runs end to end on free data tiers before any paid feed is considered (user idea).

## Approach

**Sequence.** Research-only first, then paper trading, then a gated path to live (plan decision).
**Data.** Tradier is the primary feed and yfinance is the backup feed (user idea).
**Feeds.** Free live and delayed feeds layer around the Tradier core per Data Sources (plan decision).
**Edge.** GEX and VEX profiles come from stored chain snapshots, not from vendor Greeks alone (plan decision).
**LLM.** The model acts as a tool-using analyst over structured market state, not as a RAG chatbot (user idea).
**Compute.** Scheduled jobs and persistent storage run on the operator machine via cron or launchd, and chat runs first with trade cards after the research loop (plan decision 2026-10-28, replaces the Modal plan because the operator has no Modal access).
**Money.** No live order exists before paper evidence plus an operator approval plus a kill switch (plan decision).
**Reuse.** No existing code is reused because the workspace holds no trading project (observed this session).

**Data shapes.** The plan names shapes before code (plan decision).

- A Bar holds one interval of open high low close and volume (plan decision).
- An OptionContract holds strike plus expiry plus call-or-put plus open interest (plan decision).
- A ChainSnapshot holds all contracts for one underlying at one timestamp (plan decision).
- A GexProfile holds per-strike exposure plus the total plus the flip point (plan decision).
- A Signal holds direction plus thesis plus confidence plus the snapshot id (plan decision).
- An OrderIntent holds symbol plus side plus size plus risk numbers plus the signal id (plan decision).
- A Fill holds the executed price plus size plus timestamp plus the venue paper or live (plan decision).
- A Position holds net size plus average price plus unrealized pnl (plan decision).
- A RiskState holds account size plus risk per trade plus day pnl plus halt flags (plan decision).
- A JournalEntry holds one timestamped event across signals plus intents plus fills (plan decision).

**Decisions.**

- Chat first wins over cards first because the research loop needs conversation before trade cards (plan decision).
- A custom replay engine wins over an unnamed library because stored snapshots define the harness shape (plan decision).
- Paper-first wins over live-first because money movement is irreversible (plan decision).
- Computed GEX wins over vendor GEX because no inspected free source streams it live (synthesis of Sources).

## Data Sources

**Finding.** No inspected source combines free plus live plus full options chains (synthesis of Sources).
**Policy.** Research and paper phases run on free tiers only, and paid feeds stay out of v1 (plan decision).

**Live and free stocks.**

- Alpaca states free real-time market data for building and trading over its API (Sources, Alpaca data page).
- Alpaca streams over websockets while its REST API carries a 15 minute delay (Sources, Alpaca data page).
- Alpaca covers US options and offers a paper trading API without real money (Sources, Alpaca data page).
- Alpaca moves data over IEX or Polygon rails per its own introduction (Sources, Alpaca learn page).
- Exact Alpaca free feed scope and options pricing stay unconfirmed, so a spike verifies both before adoption (evidence gap).
- Finnhub offers stock APIs with real-time prices per its homepage (Sources, Finnhub homepage).
- Finnhub free tier scope stays unconfirmed, so it is a fallback candidate behind a verification spike (evidence gap).
- Tradier real-time data needs a brokerage account, so it is not a free live source (Sources, Tradier pages).

**Delayed and free.**

- Tradier sandbox serves delayed chains for paper integration work (Sources, Tradier pages).
- Market Data serves a free plan with 100 API credits per day and data delayed at least 24 hours (Sources, Market Data free page).
- The Market Data free plan excludes real-time data and 15 minute delayed data and holds 1 year of history (Sources, Market Data free page).
- Market Data options data rolls on the next trading morning, so it seeds backtests and never live signals (Sources, Market Data free page).
- The exchange delayed-quotes dashboard exists as a browser reference per symbol (Sources, CBOE quotes page).
- Programmatic reuse of that dashboard stays unconfirmed, so the spike checks terms first (evidence gap).
- yfinance stays the emergency backup within its confirmed coverage (Sources, yfinance README).

**GEX and VEX.**

- The engine computes its own GEX and VEX from stored chains, so no vendor feed is required (plan decision).
- SqueezeMetrics publishes public DIX and GEX indicator pages with an Info CSV toggle (Sources, SqueezeMetrics indicator page).
- The public pages serve as a cross-check reference for engine totals, not as a feed (plan decision).
- SqueezeMetrics API access sits inside a $720 per month invitation subscription, so it is not adopted (Sources, SqueezeMetrics plans page).
- SqueezeMetrics terms grant no IP license by mere use and disclaim accuracy, so the spike confirms CSV reuse terms (Sources, SqueezeMetrics terms page).
- SqueezeMetrics methodology papers inform the engine design (Sources, SqueezeMetrics indicator page).

**Adoption.**

- Phase 0 adopts Tradier sandbox plus Market Data free plus one live stock feed (plan decision).
- The live stock feed defaults to Alpaca with Finnhub as fallback, pending spike verdicts (plan decision).
- SqueezeMetrics CSV stays a reference cross-check and never a primary input (plan decision).
- The dashboard page stays a browser reference until terms allow more (plan decision).

## Steps

**Phase 0. Spikes and walking skeleton.**

- The Tradier spike pulls one delayed chain through the sandbox with a paper token (Sources, Tradier pages).
- The yfinance spike records exactly which fields return for the watched symbols (Sources, yfinance README).
- The scheduler spike runs one scheduled job that writes one snapshot to persistent storage; it was written against Modal and now runs locally via cron or launchd (Sources, Modal example).
- The Telegram spike sends one brief and receives one command through HTTPS calls (Sources, Telegram tutorial).
- The LLM spike runs one tool call over a sample snapshot with the operator key (assumption).
- The free stock spike registers Alpaca first and records latency plus limits plus feed scope (Sources, Alpaca data page).
- The Finnhub fallback runs only if Alpaca fails the spike, and records the same table (evidence gap).
- The EOD spike pulls history from Market Data inside the daily credit budget and stores the seed (Sources, Market Data free page).
- The reference spike checks reuse terms for the CSV and the dashboard page, then pulls one CSV sample (Sources, SqueezeMetrics terms page).
- Each spike has a kill rule, and a failed spike stops its dependent phase (plan decision).

**Phase 1. Data core.**

- The Tradier adapter normalizes chains into ChainSnapshot records with UTC timestamps (plan decision).
- The yfinance adapter fills only the fields the Phase 0 spike confirmed (plan decision).
- The GEX engine computes per-strike exposure from gamma times open interest times multiplier with call-put sign (plan decision).
- The VEX engine follows the same shape with vega in place of gamma (plan decision).
- Vendor Greeks are hourly in brokerage and absent in sandbox, so the engine computes its own Greeks or consumes live data per the spike verdict (Sources, Tradier pages).
- The replay harness replays stored snapshots in order with a strict no-lookahead rule (plan decision).
- Backtest history seeds from the free EOD pull within its daily credit budget (Sources, Market Data free page).
- The store records the source and latency of every snapshot for audit (plan decision).
- Phase 1 starts after the Tradier and Modal spikes pass (plan decision).

**Phase 2. Research loop.**

- Chat answers chain queries and posts a scheduled GEX brief every session (user idea).
- The analyst exposes tools for chain reads plus profile computation plus journaling (plan decision).
- Every signal lands in the journal with its snapshot id and its thesis (plan decision).
- Research-only mode is complete and usable at the end of this phase (plan decision).
- Phase 2 starts after Phase 1 evidence exists (plan decision).

**Phase 3. Risk engine and paper trading.**

- Position size derives from account size and risk per trade with a one percent default (assumption).
- Profit targets apply per trade and per day from operator parameters (user idea).
- A daily loss halt blocks new intents for the rest of the session (user idea).
- A risk reviewer validates every OrderIntent schema and can veto it (plan decision).
- Vetoed and submitted intents all land in the journal with reasons (plan decision).
- Orders route only to the paper venue in this phase (plan decision).
- Phase 3 starts after Phase 2 evidence exists (plan decision).

**Phase 4. Autonomy gate.**

- Promotion needs a run of clean paper days plus a bounded drawdown plus passed halt drills (assumption).
- The live adapter ships disabled and the operator enables it with an explicit action (plan decision).
- A kill switch flattens auto-ordering immediately and needs an operator reset (plan decision).
- Go-live follows a written checklist with account size and max daily loss restated (plan decision).
- Phase 4 starts after Phase 3 evidence exists (plan decision).

**Phase 5. Trade cards.**

- Phase 5 ships trade cards: one card per recommended trade with stats plus a one-line reason (plan decision).
- Cards surface in chat text and on the /cards page, both built from the same ranked ideas (plan decision).
- Social features stay conditional on the open question below (open question).
- Phase 5 starts after Phase 3 evidence exists and runs parallel to Phase 4 (plan decision).

## Validation Plan

- Phase 0 passes when each spike shows its artifact, namely a chain file plus a scheduled run log plus a chat transcript plus a tool-call log plus one coverage table per free source (plan decision).
- Phase 0 passes when each free feed row shows latency plus limits plus a terms verdict (plan decision).
- Phase 1 passes when `pytest tests/test_gex_engine.py -q` is green on fixed snapshots with hand-checked totals (planned path, assumption).
- Phase 1 passes when the replay harness reruns a week of snapshots twice with identical journals (plan decision).
- Phase 1 passes when engine GEX totals match the reference CSV within a stated tolerance on the same date (assumption).
- Phase 2 passes when the chat transcript shows a GEX brief with numbers matching the stored profile (plan decision).
- Phase 3 passes when a simulated breach halts the day and the journal shows the block (plan decision).
- Phase 3 passes when paper fills reconcile with intents one by one (plan decision).
- Phase 4 passes when the kill switch drill stops ordering within the stated limit and needs a reset (assumption).
- Phase 4 passes when the operator signs the go-live checklist in the journal (assumption).
- Phase 5 passes when card stats match engine numbers on the same snapshot (plan decision).
- The highest-risk validation is GEX correctness in Phase 1, because wrong exposure corrupts every later signal (plan decision).
- The second highest-risk validation is the Phase 4 live gate, because it guards real money (plan decision).

## Risks / Open Questions

- Sandbox data runs delayed by the industry standard fifteen minutes, so paper fills will differ from live fills (Sources, Tradier pages).
- Sandbox Greeks are unavailable, so GEX needs live data or computed Greeks before it can be trusted (Sources, Tradier pages).
- yfinance is unofficial and intended for research and personal use, so it stays a backup and never the primary feed (Sources, yfinance README).
- An LLM can hallucinate an order, so schema validation plus the risk veto stand in front of every intent (plan decision).
- Free tiers carry credit budgets, so the EOD seed design respects the daily cap (Sources, Market Data free page).
- Dashboard reuse terms are unverified, so no scraper ships before the terms verdict (evidence gap).
- The paid GEX API is excluded from v1 on price, so engine accuracy rests on the cross-check (Sources, SqueezeMetrics plans page).
- Finnhub free scope is unverified beyond its homepage, so adoption waits on the spike (evidence gap).
- Live paper fills will lag where feeds delay, so promotion criteria discount delayed-fill performance (plan decision).
- The operator picks research-only or full autonomy for v1, and the default is research-only first (open question).
- The operator picks chat-first or cards-first, and the default is chat-first (open question).
- The operator clarifies whether Grok bot style means a model choice or a conversational tone, and both branches stay feasible (open question).
- The operator defines social interaction as publishing outward or ingesting sentiment or both (open question).
- The operator supplies one LLM key and the account size plus risk numbers before Phase 3 (open question).
- The operator approves opening one funded brokerage account for live data if free tiers prove insufficient (open question).

## Notes

- The how, technical-writing, unslop, and principle skills are disabled, so no leaf is cited and inline rules apply (observed this session).
- The plan is saved to init.md because a file path was requested (observed this session).
- No PR slices are named because no repo workflow exists yet (observed this session).
- A single-owner repo with Python services is assumed and stays reversible (assumption).

## Sources

- https://docs.tradier.com/docs/endpoints.md
- https://docs.tradier.com/docs/market-data
- https://raw.githubusercontent.com/ranaroussi/yfinance/main/README.md
- https://modal.com/docs/examples/cron_datasette
- https://core.telegram.org/bots/tutorial
- https://www.cboe.com/delayed_quotes/AEL/quote_table/
- https://squeezemetrics.com/monitor/dix
- https://squeezemetrics.com/monitor/plans
- https://squeezemetrics.com/monitor/terms
- https://alpaca.markets/data
- https://alpaca.markets/learn/market-data-api-intro/
- https://finnhub.io
- https://www.marketdata.app/docs/account/plans/free-forever/

## Muse integration program

**Entry.** The project enters Build for Your World with Meta models inside the operator trading loop (plan decision 2026-10-27).
**Status.** M0, M1, M2, M3, M4, and M5 complete.
**Blocked on.** Nothing. Entry is Build for Your World with a 5 USD day budget (answered 2026-10-27). M0 spent 0.02 USD.

**Phase M0. Model spikes.** Five spikes run with kill rules: Spark tool call, grounding comparison, voice sample, image sample, open-weights run verdict. Evidence is per-call transcripts plus a cost table under /tmp. Result: 5 of 5 pass. Grounding needs the Responses API because chat completions rejects web_search. Transcribe needs 16 kHz mono WAV with request plus audio parts. Spark tool calls are slow at 25 seconds, so M1 needs a timeout plus fallback. Glimmer runs locally via llama.cpp with small GGUF quants, and this machine fits, so weights download waits for M1.

**Phase M1. Spark analyst.** A role registry maps the analyst role to muse-spark-1.3 with the deterministic flip rule as fallback. New files are bot/models.py plus tests/test_models.py. Evidence is brief-number parity plus a journaled ModelReceipt on one live brief. Result: implemented. Registry plus Spark tool loop plus ModelReceipt journaling plus offline default. 17 focused tests pass. A live brief journaled 2 receipts at 0.004 USD with reasoning_effort low.

**Phase M2. Voice plus grounding.** Voice notes transcribe into chat commands. A grounding toggle enriches theses. New file is bot/voice.py. Evidence is a voice brief matching its typed brief. Result: implemented. 16 kHz WAV only, Telegram ogg conversion waits for the transport. 15 focused tests pass. A live voice brief matched typed numbers and a grounded brief carried a searched note at 0.011 USD.

**Phase M3. Cards plus review.** Regime card art renders per brief with code-drawn numbers. Wonder appends an advisory review note that approves nothing. New file is bot/cards.py. Evidence is card numbers matching engine output plus operator eyeball of three cards. Result: implemented. Review runs standalone on explicit call, not per chat, because per-brief Wonder reads would waste the premium tier. 12 focused tests pass. Three live cards eyeballed clean with no rendered text. One live review returned not-ready with cited evidence at 0.047 USD total.

**Phase M4. Demo.** One transcript covers voice command plus grounded brief plus card plus offline fallback plus a kill drill with models in the loop. Evidence is the transcript plus a cost table under budget. Result: transcript at /tmp/m4_transcript.txt. Voice brief plus grounded note plus card line plus deterministic fallback plus kill drill approved/rejected/approved. Cost 0.046 USD across analyst, artist, grounding, transcriber.

**Phase M5. Standout features.** Ten Muse-powered features land behind the same receipt plus budget plus fallback discipline: local Glimmer/Spark Open analyst over llama.cpp (bot/localmodels.py), cost-aware tier router with draft/deep/premium classes (bot/tiers.py), Spark chart screenshot reader (bot/chartread.py, POST /chartread), SAM plus Spark document reader (bot/docread.py, POST /docread), shock news explainer (bot/news.py), spoken holdings plus two-step voice kill (bot/holdings.py, bot/voice.py), Wonder journal coach (bot/coach.py), alert narration plus local noise judge (bot/alerts.py), natural-language paper portfolio commands with Spark tool assist (bot/commands.py), shareable signal art with cache (bot/cards.py, GET /share). New files are localmodels, tiers, chartread, docread, news, coach, commands plus tests/test_tiers.py, test_localmodels.py, test_chartread.py, test_news.py, test_voice_commands.py, test_coach.py, test_narrate.py, test_commands.py, test_share.py, test_docread.py, test_surfaces.py. Surfaces extend by 8 MCP tools, 4 slash commands, and 3 HTTP endpoints; the two surface-pinning tests were extended for the new names. Evidence is the focused suites plus one full `pytest tests/ -q` green run.

**Decisions.**
- The default tier is muse-spark-1.3, because the contributor tier trains on submitted data (inspected Meta docs).
- SAM 3.1 stays out of v1, because Spark reads screenshots natively and no segmentation job exists in the loop (plan decision).
- Transcribe is input only, so spoken briefs stay out of scope (plan decision).
- The autonomy gate keeps its deterministic checks with or without Wonder (plan decision).
