# Trading Bot

A research-first trading system that grows into gated autonomy one verified phase at a time. It ingests options chains and stock bars from free-tier market APIs, computes its own GEX/VEX/DEX exposure profiles from stored snapshots, answers research questions in chat, and journals every signal, intent, and fill. Paper trading is implemented. Live ordering is disabled by default and routes to a Tradier venue once the operator enables it with a signed action.

The plan lives in [init.md](init.md). The design pseudocode lives in [overview.md](overview.md). Diagrams live in [diagrams/](diagrams/) as Mermaid sources.

Meta model integration is planned as Phases M0-M4 in [init.md](init.md): a Muse Spark analyst behind the research loop, voice input, brief cards, and a Muse Wonder advisory review. M1, M2, M3, and M4 are implemented.

## How it works

Data flows one way, and every step writes to the journal:

```
market APIs ──▶ feeds ──▶ store ──▶ exposure ──▶ research ──▶ risk ──▶ paper venue
                  │          │          │             │           │
                  └──────────┴──────────┴─────────────┴───────────┴──▶ journal
```

Meta Model API serves a Spark analyst behind research, plus voice input, card art, and a Wonder advisory review; every model call journals a ModelReceipt (Phases M0-M4 in init.md, all implemented).

1. **Feeds** (`bot/feeds.py`) pull options chains and daily bars from five sources through a registry table keyed by source name. Each adapter normalizes vendor JSON into `ChainSnapshot` or `Bar` records stamped with source name and latency.
2. **Store** (`bot/store.py`) is an in-memory snapshot and bar store with bar dedupe, indexed latest-reads, per-underlying retention, and previous-snapshot lookup. `read_latest` serves the research loop.
3. **Exposure** (`bot/exposure.py`) computes GEX, VEX, and DEX from stored chains: signed Greek × open interest × multiplier per contract, summed per strike, plus the interpolated flip point where cumulative exposure crosses zero. With spot it reports $GEX per 1% move with a front-expiry split. No vendor GEX feed is required.
4. **Research** (`bot/research.py`) is the chat loop. It reads the latest snapshot, computes the profile, runs the analyst tool, derives display fields (status, trigger, entry band, checklist, coverage), and returns a compact text brief with snapshot age, top walls, and change versus the previous snapshot. Card requests return per-trade cards with a one-line reason each. VEX and DEX requests get their own briefs. The analyst tool routes to a Muse Spark backend behind a role registry when live_models=True; otherwise the deterministic rule runs as the offline fallback (Phase M1). Grounding appends a web-searched note to theses when grounding=True, and voice notes transcribe through bot/voice.py and route as chat queries (Phase M2). Brief cards append a card line when cards=True, and advisory_review gives a standalone Wonder pre-promotion read (Phase M3). Profile messages print a four-question risk questionnaire or save a one-shot OperatorProfile with suggested risk params. Idea requests return up to 10 ranked defined-risk verticals filtered by timeframe words and named stocks; naming a stock with no stored snapshot errors instead of falling back. Scheduled briefs append flip-cross, regime-flip, and stale-snapshot alerts.
5. **Risk** (`bot/risk.py`) sizes intents from account size × risk per trade (1% default), or by max loss per contract for options, and returns no intent for flat signals. It validates every intent against schema, halt/kill flags, and trade/day targets, and submits fills to the paper venue at the touch with slippage and fees. Day PnL accumulates unrealized moves plus realized closes. A daily-loss breach halts the day.
6. **Autonomy** (`bot/autonomy.py`) gates promotion: clean paper days plus bounded drawdown plus passed halt drills yields `ready_for_checklist`. Live ordering ships disabled and needs an explicit signed action. The kill switch flattens auto-ordering and needs an operator reset.
7. **Trade cards** (`bot/tradecards.py`) builds one card per recommended trade from the same screener as ideas: spread stats plus wall exposure plus regime plus spot versus flip, with a one-line reason written by the Meta analyst model (`bot/metareason.py`) or the deterministic offline fallback. Chat answers card queries with text cards; the HTTP server renders them at `/cards` and `/cards.json`.
8. **Replay** (`bot/replay.py`) replays stored snapshots in time order with no lookahead, either as journaled totals or as a full signal-to-paper-fill backtest that generates its own `PaperEvidence`. The `run_replay` tool and the `replay` chat keyword run it over the stored bundle into a one-screen demo summary.
9. **Surfaces** (`bot/charts.py`, `bot/alerts.py`) render the gamma-by-strike chart at `/chart` and the regime board at `/board`, and push flip, regime, and wall-proximity alerts to Discord or Telegram from every session run.

## Modules

| Module | Owns |
|---|---|
| `bot/shapes.py` | Frozen dataclasses: `Bar`, `OptionContract`, `ChainSnapshot`, `GexProfile`, `VexProfile`, `DexProfile`, `Signal`, `OrderIntent`, `OrderLeg`, `Fill`, `Position`, `RiskState`, `JournalEntry`, `OperatorConfig`, `RiskLimits`, `PaperEvidence`, `ModelReceipt`, `OperatorProfile`, `TradeIdea`, `PremiumCandidate`, `PortfolioView` |
| `bot/feeds.py` | `FeedContext`, five adapters, `FEED_REGISTRY`, kind/fallback tables, `ingest_chain`, `ingest_bars`, `live_context`, `default_context` |
| `bot/store.py` | `SnapshotStore`: snapshots plus bars plus saved profile, latest/previous reads, underlying listing, dedupe, `prune`, `restore` |
| `bot/journal.py` | `Journal`: append-only timestamped event log, `prune` for long runs |
| `bot/persist.py` | `save_bundle`, `load_bundle`: JSON snapshots plus bars plus journal plus profile, retention caps, `refresh_bundle` for live reload |
| `bot/schedule.py` | `run_scheduled_session`: multi-underlying ingest plus brief plus persist in one job; `refresh_store`: ingest-only timer refresh |
| `bot/exposure.py` | `compute_gex` ($GEX per 1% move with spot, interpolated flip), `compute_vex`, `compute_dex` |
| `bot/research.py` | `route_chat`, `handle_chat_query`, `scheduled_brief`, `live_brief`, trade cards, VEX/DEX briefs, change alerts, `journal_tail_text`, `paper_pnl_text`, profile questionnaire, trade ideas, `advisory_review` |
| `bot/risk.py` | `build_intent`, `even_legs`, `review_intent` with profile gating, `submit_paper`, `paper_fill_price`, `realized_pnl`, halt/kill transitions |
| `bot/autonomy.py` | `check_promotion`, `enable_live`, `kill_switch`, `reset_autonomy`, `submit_live` |
| `bot/venue.py` | Tradier live venue: `occ_symbol`, equity, single-option, and combo orders, `LiveAck` |
| `bot/tradecards.py` | `TradeCard`, card builder over `rank_ideas`, text plus JSON plus HTML renderers |
| `bot/replay.py` | `replay_week` (totals or signal-to-fill backtest), `paper_evidence_from_journal`, `replay_response` demo summary |
| `bot/charts.py` | Gamma-by-strike SVG chart page plus multi-ticker regime board, both auto-refreshing |
| `bot/alerts.py` | Wall-proximity plus flip/regime alerts with Discord/Telegram push and journal dedup |
| `bot/models.py` | `MODEL_REGISTRY`, Spark tool loop, `ModelReceipt`, day-budget gate |
| `bot/voice.py` | Ogg-to-WAV conversion plus 16kHz WAV transcription into chat commands, `voice_transcript` journaling |
| `bot/cards.py` | Regime card art with engine numbers, `brief_card` journaling |
| `bot/profile.py` | Risk questionnaire table, answer parsing, `OperatorProfile`, suggested params |
| `bot/ideas.py` | Timeframe and symbol parsing, shared `resolve_direction`, ranked top-10 `TradeIdea` screener (confidence order, 7/direction cap, 1-day expiry floor) with defined-risk verticals; naming a ticker with no snapshot errors instead of falling back |
| `bot/wheel.py` | Cash-secured-put plus covered-call premium screener: staged `screen_premium` over stored snapshots, Friday-preferring expiry picker, earnings skip, IV rank gate, top-10 `PremiumCandidate` ranking, `wheel_response` chat reply |
| `bot/ivrank.py` | IV rank and IV percentile math plus store-backed per-symbol history helpers |
| `bot/config.py` | Single dotenv plus environment reader, bearer token, profile-to-limits `operator_config` |
| `bot/metareason.py` | Meta reason-model client: `generate_reason`, backend selection |
| `bot/baseten.py` | Dormant Baseten client (`predict_url`, Inference API slugs) for later |
| `bot/muse.py` | Muse chat transport: `parse_chat_payload`, `handle_chat`, `TOOL_REGISTRY`, `dispatch_tool`, `openapi_spec`, `route_http`, `create_server`, `serve` with timer refresh, open-server warning, drill `Harness` |

## Feeds

| Feed | Kind | Credential | Role |
|---|---|---|---|
| `tradier_sandbox` | chain | `TRADIER_ACCESS_TOKEN` | Primary chain. Sandbox is delayed ~15 min without Greeks; production has Greeks |
| `marketdata_eod` | chain | `MARKETDATA_API_KEY` | EOD chain seed and fallback. 100 free credits/day |
| `alpaca` | bars | `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY` | Primary daily bars (IEX rail) |
| `finnhub` | bars | `FINNHUB_API_KEY` | Live quote bar plus earnings calendar; currently the working spot source |
| `yfinance` | bars | none | Unkeyed backup |

Fallback order is fixed tables: chains try Tradier then Market Data; bars try Alpaca, Finnhub, then Yahoo. An empty bar result counts as feed failure and falls through to the next source. `live_brief` journals each failure with the error class name only, raises `FeedUnavailableError` when every chain feed fails, and treats bars as best effort (without a bar or a flip, direction falls back to the regime sign and the brief says so).

Without arguments, `ingest_chain`/`ingest_bars` use canned vendor-shaped fixtures, so the whole suite and every offline flow run with no keys and no network.

## Configuration

Copy these into a `.env` file in the repo root (never commit it):

```
TRADIER_ACCESS_TOKEN=<token>
TRADIER_ACCOUNT_ID=<account id for live orders>
TRADIER_BASE_URL=https://api.tradier.com/v1   # optional; default is the sandbox host
APCA_API_KEY_ID=<id>
APCA_API_SECRET_KEY=<secret>
FINNHUB_API_KEY=<key>
MARKETDATA_API_KEY=<key>
MUSE_AUTH_TOKEN=<secret>   # optional; when set, chat/mcp/cards need `Authorization: Bearer <secret>`
DISCORD_WEBHOOK_URL=<url>   # optional; alert pushes (with no provider set, alerts stay journal-only)
TELEGRAM_BOT_TOKEN=<token>   # optional; alert pushes need this plus TELEGRAM_CHAT_ID
TELEGRAM_CHAT_ID=<id>   # optional; alert push target
META_MODEL_API_KEY=<key>   # optional; model auth for analyst phases and card reasons (without it, deterministic paths run)
MODEL_DAY_BUDGET_USD=<usd>   # optional; day budget for the model phases
```

The bot reads `.env` from the working directory automatically; real environment variables win over file values. Sandbox tokens only work on the sandbox host and production tokens only on production (`TRADIER_BASE_URL` selects the host). Secrets never appear in code, journals, or error messages. Model phases add `META_MODEL_API_KEY` and `MODEL_DAY_BUDGET_USD`; without both set, the deterministic paths run (Phase M0). Trade card reasons reuse `META_MODEL_API_KEY`; without it, the offline fallback writes each reason.

## How to run

### Prerequisites

Python 3.11+. All commands run from the repo root. Create the venv and install the test runner:

```bash
python -m venv .venv
.venv/bin/pip install pytest
```

### Configuration

Copy the keys from the [Configuration](#configuration) section into a `.env` file in the repo root (never commit it): `TRADIER_ACCESS_TOKEN`, `TRADIER_ACCOUNT_ID`, `TRADIER_BASE_URL` (optional), `MUSE_AUTH_TOKEN` (optional), `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `FINNHUB_API_KEY`, `MARKETDATA_API_KEY`, plus `META_MODEL_API_KEY` and `MODEL_DAY_BUDGET_USD` for the model phases, with card reasons reusing `META_MODEL_API_KEY`. The bot reads `.env` from the working directory automatically; real environment variables win over file values. Without keys, the suite and every offline flow still run on canned fixtures with no network.

### Test suite

Fully offline, no network:

```bash
.venv/bin/python -m pytest tests/ -q
```

### Full app in one script

`run_app.py` loads the JSON bundle, prints the live brief, saves, then serves the Muse HTTP surface (blocks serving) while re-ingesting on a timer (`--refresh-seconds`, default 900, 0 disables) so snapshots stay fresh without restarts. On a feed failure it prints the failure, still saves, and still serves the loaded state.

```bash
.venv/bin/python run_app.py
.venv/bin/python run_app.py --underlying SPY --store-dir data/trading-bot --host 0.0.0.0 --port 8000
```

| Flag | Default | Meaning |
|---|---|---|
| `--underlying` | `SPY` | Comma-separated symbols to brief |
| `--store-dir` | `data/trading-bot` | JSON bundle directory |
| `--host` | `0.0.0.0` | HTTP bind host |
| `--port` | `8000` | HTTP bind port |
| `--refresh-seconds` | `900` | Re-ingest interval while serving; 0 disables |
| `--session-schedule` | _(disabled)_ | Cron subset `M H * * DOW` for in-app sessions; replaces cron/launchd below |
| `--session-underlying` | _(value of `--underlying`)_ | Symbols for scheduled sessions |

Saved profiles drive the served limits: `max_day_loss` and `max_trade_size` come from the profile's suggested params when one is stored, otherwise the built-in defaults apply. A Muse bot instance hooks to http://HOST:PORT/chat, with an `Authorization: Bearer` header when `MUSE_AUTH_TOKEN` is set.

### Scheduled session by hand

`run_session.py` runs the scheduled session on this machine against a local JSON bundle in `data/trading-bot`; `run_scheduled_session` in `bot/schedule.py` is the same job for library drives. It prints the brief to stdout. Each run also paper trades at most one open position per symbol at the latest mark (`--no-paper-trade` disables), so `paper_pnl` shows fills plus marked P&L after the first run. Run once per symbol to add it to the bundle (e.g. `--underlying QQQ`); each run briefs the requested symbols plus every underlying already in the bundle, and the live server reloads the rewritten bundle without a restart.

```bash
.venv/bin/python run_session.py
.venv/bin/python run_session.py --underlying SPY --store-dir data/trading-bot
.venv/bin/python run_session.py --help
```

| Flag | Default | Meaning |
|---|---|---|
| `--underlying` | `SPY` | Comma-separated symbols to brief |
| `--store-dir` | `data/trading-bot` | JSON bundle directory |

### Scheduled session on a timer

Prefer `--session-schedule "30 7 * * 1-5"` on `run_app.py`: the server runs the same session in-app on weekdays at 07:30 machine-local and prints the brief, no external timer needed. `POST /session` triggers the same job on demand. The standalone timers below remain for setups that do not run the server.

Both timers run `run_session.py` at 07:30 machine-local on weekdays (08:30 ET on a CT machine) and append stdout plus stderr to `data/session.log`. Install once, replacing `REPO_ROOT` with the repo path.

Cron (`deploy/session.cron`): append the job line to your crontab (`crontab -e`):

```bash
crontab -e
```

```
30 7 * * 1-5 cd REPO_ROOT && REPO_ROOT/.venv/bin/python REPO_ROOT/run_session.py >> REPO_ROOT/data/session.log 2>&1
```

macOS launchd (`deploy/trading-bot-session.plist`):

```bash
sed 's|REPO_ROOT|/path/to/trading_bot_project|g' deploy/trading-bot-session.plist > ~/Library/LaunchAgents/com.tradingbot.session.plist
launchctl load ~/Library/LaunchAgents/com.tradingbot.session.plist
```

Check the log after a run:

```bash
tail data/session.log
```

### HTTP server

`bot.muse.serve(store, journal, config)` exposes the same surface as the drill harness over HTTP. It binds `0.0.0.0:8000` by default so external bots can connect over the network; pass `host`/`port` to override. Binding a non-loopback host without `MUSE_AUTH_TOKEN` leaves chat/mcp/cards open and prints a startup warning; `/health`, `/healthz`, and `/openapi.json` stay open by design.

| Endpoint | Method | Body |
|---|---|---|
| `/chat` | POST | `{"text": "...", "underlying": "SPY"}` (`live_models`, `grounding`, `cards`, `llm_router` optional, default false) |
| `/session` | POST | `{"underlying": "SPY"}` (same optional flags as `/chat`); runs a session now, replies `{"ok", "reply", "underlyings"}` |
| `/mcp` | POST | `{"method": "tools/list"}` or `{"method": "tools/call", "params": {"name": "...", "arguments": {}, "underlying": "SPY"}}` |
| `/cards?underlying=SPY[&query=...]` | GET | none; HTML trade cards page |
| `/cards.json?underlying=SPY[&query=...]` | GET | none; trade cards JSON |
| `/chart?underlying=SPY` | GET | none; gamma-by-strike SVG chart with flip line, refreshes every 60s |
| `/board` | GET | none; regime board across all stored underlyings, refreshes every 60s |
| `/health` | GET | none; last ingest time, feed used, snapshot age, quote ages, underlyings (always open) |
| `/healthz` | GET | none; minimal `{"ok": true}` probe (always open) |
| `/openapi.json` | GET | none |

MCP tools: `get_gex_brief`, `get_vex_brief`, `get_dex_brief`, `get_trade_cards`, `get_trade_ideas`, `get_wheel_ideas`, `journal_tail`, `paper_pnl`, `run_session`, `get_iv_rank`, `run_replay`. Each session run also pushes new flip, regime, and wall-proximity alerts to Discord or Telegram when configured. Session runs copy state out, run unlocked, and merge back, so reads never block on a session.

When `store_dir` is passed to `serve` (as `run_app.py` does), the server reloads the JSON bundle whenever the scheduled session rewrites it, and with `refresh_seconds > 0` it re-ingests on its own timer (`bot/schedule.py: refresh_store`, ingest-only, no signals minted), prunes in-memory growth, and persists every pass, so snapshots stay fresh without restarts. Each tick refreshes the startup symbols plus every underlying currently in the store, so a ticker added later via `run_session.py --underlying QQQ` stays fresh with no restart. Session runs (`/session`, `run_session`, and the session timer) likewise cover the requested symbols plus every underlying in the store, requested first.

Start it (seeded with canned data so `/chat` answers immediately; blocks serving):

```python
from bot.feeds import ingest_bars, ingest_chain
from bot.journal import Journal
from bot.muse import serve
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore

store, journal = SnapshotStore(), Journal()
config = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)
ingest_chain("tradier_sandbox", store, journal)
ingest_bars("alpaca", store, journal)
serve(store, journal, config)
```

Query it:

```bash
curl -s localhost:8000/openapi.json
curl -s -X POST localhost:8000/chat -d '{"text": "give me the gex brief", "underlying": "SPY"}'
curl -s -X POST localhost:8000/mcp -d '{"method": "tools/list"}'
curl -s -X POST localhost:8000/mcp -d '{"method": "tools/call", "params": {"name": "get_gex_brief", "arguments": {}, "underlying": "SPY"}}'
```

### Interactive snippets

Offline research brief with canned data:

```python
from datetime import datetime, timezone
from bot.feeds import ingest_chain, ingest_bars
from bot.journal import Journal
from bot.research import route_chat
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore

now = datetime.now(timezone.utc)
store, journal = SnapshotStore(), Journal()
config = OperatorConfig(
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)
ingest_chain("tradier_sandbox", store, journal)
ingest_bars("alpaca", store, journal)
print(route_chat("give me the gex brief", "SPY", store, journal, config, now))
print(route_chat("show me trade cards", "SPY", store, journal, config, now))
```

Live brief with real keys in `.env` (ingests across fallback tables, then briefs):

```python
from datetime import datetime, timezone
from bot.journal import Journal
from bot.research import live_brief
from bot.store import SnapshotStore

print(live_brief("SPY", SnapshotStore(), Journal(), datetime.now(timezone.utc)))
```

Paper trade off a signal (continuing with `store`, `journal`, and `now` from above):

```python
from bot.research import handle_chat_query
from bot.risk import build_intent, review_intent, submit_paper
from bot.shapes import RiskLimits, RiskState

_, signal = handle_chat_query("brief", "SPY", store, journal, now)
state = RiskState(account_size=100000.0, risk_per_trade=0.01, day_pnl=0.0,
                  halt_active=False, kill_active=False)
limits = RiskLimits(max_trade_size=5000.0, day_profit_target=500.0, max_day_loss=1000.0)
intent = build_intent(signal, "signal-1", state, journal, now)
if intent is not None and review_intent(intent, state, limits, journal, now) == "approved":
    fill, position, state = submit_paper(intent, 769.7, state, None, limits, journal, now,
                                         bid=769.6, ask=769.8, slippage_bps=5.0, fees=1.30)
```

(`build_intent` returns `None` for flat signals, and `submit_paper` fills at the touch when quotes are given, falling back to the mark price.)

Backtest replay over stored snapshots (totals only, or a full signal-to-fill backtest with bars):

```python
from bot.replay import paper_evidence_from_journal, replay_week
journal = replay_week([snapshot_day2, snapshot_day0, snapshot_day1], Journal())
journal = replay_week([snapshot_day2, snapshot_day0, snapshot_day1], Journal(), bars=bars)
evidence = paper_evidence_from_journal(journal)
```

Chat through the Muse transport (self-seeded drill harness):

```python
from bot.muse import Harness

harness = Harness()
harness.post("give me the gex brief")
harness.run_once()
print(harness.outbox[-1]["body"])
```

### Persistence

`save_bundle(store_dir, store, journal)` writes five JSON files into the store directory: `snapshots.json`, `bars.json`, `journal.json`, `profile.json`, and `iv.json` (per-symbol daily average-IV history, newest 600 days kept). Each write goes to a `.tmp` file first and is renamed into place, so a crash never leaves a half-written bundle. Retention caps keep the newest 500 snapshots and bars per underlying and the newest 20000 journal entries (tunable via `keep_snapshots`, `keep_bars`, `keep_journal`). `load_bundle(store_dir)` returns a `(store, journal)` pair and tolerates missing files, so a fresh directory just yields empty state. Every scheduled run persists to `data/trading-bot`. Inspect the journal any time via `journal.entries()`:

```python
from bot.persist import load_bundle, save_bundle

save_bundle("data/trading-bot", store, journal)
store, journal = load_bundle("data/trading-bot")
for entry in journal.entries():
    print(entry.timestamp_utc, entry.kind)
```

### Chat options

`route_chat` and `handle_chat_query` take `live_models`, `grounding`, and `cards` flags (all default `False`); the same flags work on the HTTP `/chat` payload. With `live_models=True` the analyst routes to the Spark backend through `MODEL_REGISTRY` when `META_MODEL_API_KEY` and `MODEL_DAY_BUDGET_USD` are set, otherwise the deterministic rule runs and the fallback is journaled as `model_fallback`. `grounding=True` appends a web-searched note to the thesis and `cards=True` appends a regime card line:

```python
print(route_chat("give me the gex brief", "SPY", store, journal, config, now,
                 live_models=True, grounding=True, cards=True))
```

### Voice notes

`handle_voice_note(audio, underlying, store, journal, now, live_models=True)` transcribes WAV bytes and routes the transcript as a chat query. Ogg input converts automatically when `ffmpeg` is on `PATH`. Voice needs live models plus the model keys; without them it returns `"voice notes need live models"`, and bad audio is journaled as `voice_rejected`:

```python
from bot.voice import handle_voice_note

audio = open("note.ogg", "rb").read()
print(handle_voice_note(audio, "SPY", store, journal, now, live_models=True))
```

### Operator profile

Chatting `"profile"` prints the four-question risk questionnaire. Answer in one message to save the profile and get suggested risk params:

```python
print(route_chat("profile", "SPY", store, journal, config, now))
print(route_chat("profile: risk_tolerance=moderate, portfolio_size=50000, "
                 "knowledge=intermediate, options_ok=yes",
                 "SPY", store, journal, config, now))
```

`risk_tolerance` is `conservative`, `moderate`, or `aggressive`; `knowledge` is `beginner`, `intermediate`, or `advanced`; `options_ok` is `yes` or `no`. `suggest_params` maps the answers to `risk_per_trade`, `account_size`, `max_day_loss`, and `max_trade_size`. The profile persists in `profile.json`, and `options_ok=no` blocks options trade ideas plus options intents at `review_intent`.

### Trade ideas

Any chat containing `"idea"` returns up to 10 ranked ideas. Each idea needs a direction, passes minimum-OI and maximum-spread filters, expires at least one day out (no 0DTE), and is structured as a defined-risk vertical with max loss and breakeven when quotes exist. Ideas rank by printed confidence with at most 7 per direction. Filter by timeframe words (`weekly`, `monthly`, `quarterly`, or `"N days"`) or by naming a stored underlying. Naming a ticker with no stored snapshot errors instead of falling back to the default:

```python
print(route_chat("weekly trade ideas", "SPY", store, journal, config, now))
# needs a QQQ snapshot first: run_session.py --underlying QQQ
print(route_chat("trade ideas for QQQ", "SPY", store, journal, config, now))
```

Queries mentioning `"card"` or `"cards"` return trade cards instead: one card per recommended trade with spread stats plus wall exposure plus regime plus spot versus flip, and a one-line reason stating why it is a good trade. The same cards render as a page at `/cards?underlying=SPY` and as JSON at `/cards.json?underlying=SPY`.

Each idea flags event risk: `earnings 2026-11-05` when the expiry spans the symbol's next earnings date (live Finnhub calendar, cached per process, fail-soft), plus `iv rank 87.0` when at least 20 IV points are stored. The `get_iv_rank` tool reports ranks per underlying.

### Wheel premium selling

Any chat containing `"wheel"` or `"wheels"` returns up to 10 ranked premium candidates. Each candidate is a `SELL_PUT` (cash-secured) or `SELL_CALL` (covered, flagged as needing 100 shares) that passes staged filters: 7–30 DTE, 0.15–0.30 absolute delta with the sign matching the side, open interest of at least 100, a 30% bid-relative spread cap for contracts at $1.00 and above (a $0.15 absolute cap for cheaper ones), and at least 1% premium over capital at risk (strike value for puts, spot value for calls, so calls need a stored bar). Each symbol screens its nearest three expiries with Fridays preferred, and an optional earnings map (`{symbol: date}` passed to `wheel_response`/`screen_premium`) skips any expiry an earnings date spans within a two-day buffer. Symbols with at least 20 days of IV history need an IV rank of 30 or more; thinner history passes. Every screen records the symbol's average candidate IV into the store (persisted in `iv.json`) and journals `iv_recorded` plus `wheel_screened`. Results rank by premium return with deterministic tiebreaks. Wheel output is research, so the saved profile never gates it:

```python
print(route_chat("wheel ideas", "SPY", store, journal, config, now))
```

Wheel-style risk caps live in `review_intent` behind an optional `PortfolioView` (open positions, open symbols, peak day PnL). `RiskLimits` adds `max_positions`, `drawdown_pause_pct`, and `max_per_sector`, all defaulting to off; with no view the review behaves exactly as before. The drawdown pause trips when the drop from peak day PnL reaches the configured share of a positive peak, and the sector cap counts known-sector tickers among open symbols (unknown tickers never trip it).

Deliberately not integrated: a volume filter needs a contract field that does not exist, early-close management needs contract-level paper positions, a GBM backtester loses to replay on real snapshots, and a Public.com venue is out of scope.

### Reason model

Each trade card carries a one-line reason. With `META_MODEL_API_KEY` set, the app calls the Meta analyst model for each card; otherwise (or on any failure) the deterministic offline fallback writes the reason from the card numbers. Model reasons cache per card stats in memory (cap 2000, journaled with `cached: true` on hits), so repeat `/cards` fetches skip the model calls entirely. The Baseten client (`bot/baseten.py`) plus the Truss app in [deploy/reason-model](deploy/reason-model) stay dormant for later, deployed with:

```bash
BASETEN_API_KEY=... deploy/baseten.sh
```

The script pushes the model dir and prints the predict URL pattern plus the two operator vars to set.

### Paper to live

Paper trade first (see the snippet above), then walk the promotion gate. `check_promotion` reads a `PaperEvidence` record and returns `"ready_for_checklist"` or `"stay_paper"`; build it by hand or generate it from a backtest with `paper_evidence_from_journal`. `advisory_review` adds a standalone Wonder pre-promotion read when the model backend is available:

```python
from bot.autonomy import check_promotion
from bot.research import advisory_review
from bot.shapes import PaperEvidence

evidence = PaperEvidence(clean_days=20, clean_days_target=20, max_drawdown=0.01,
                         drawdown_bound=0.05, halt_drills_passed=True)
print(check_promotion(evidence))
print(advisory_review(evidence, journal, now, live_models=True))
```

`advisory_review` needs `live_models=True` plus the model keys, and returns an empty string (journaled as `review_skipped`) when the backend is unavailable.

Live ordering ships disabled. `enable_live` flips it on with a non-empty operator signature unless the kill flag is active, and `submit_live` routes the intent through `TradierVenue.from_env()` (credentials from `.env`, host from `TRADIER_BASE_URL`). A positive `reference_price` is required for sizing:

```python
from bot.autonomy import enable_live, submit_live

print(enable_live("operator-smith-2026-10-29", state, journal, now))
fill, position, state = submit_live(intent, state, position, limits, journal, now,
                                    reference_price=769.7)
```

`kill_switch(state, journal, now)` disables live ordering and fires the kill flag; `reset_autonomy(state, journal, now)` clears halt and kill after an operator review. Both journal their transitions.

### Multi-leg intents

`even_legs(total_size, specs)` splits size evenly across leg specs of `(symbol, side, strike, expiry, call_or_put)` and returns `OrderLeg` tuples for `build_intent`:

```python
from bot.risk import even_legs, review_intent

legs = even_legs(10.0, [("SPY", "buy", 770.0, "2026-12-18", "call"),
                        ("SPY", "sell", 780.0, "2026-12-18", "call")])
intent = build_intent(signal, "signal-1", state, journal, now, legs=legs)
if intent is not None:
    print(review_intent(intent, state, limits, journal, now, profile=store.read_profile()))
```

Options legs need the saved profile at review (`options_ok` must be yes); without it `review_intent` vetoes the combo. Paper covers combos end to end. Live `place_order` places each combo leg as its own day option order and reports the legs as one net-premium fill.

## Status and roadmap

Implemented: five live feeds with fallback, GEX/VEX/DEX engine with dollar GEX and interpolated flip, research chat loop plus scheduled briefs with change alerts, trade cards over HTTP plus chat with Meta reasons and offline fallback, paper trading with quote fills and halt logic, autonomy gate with kill switch, signal-to-fill replay backtest with generated paper evidence, Muse program M0-M4 (Spark analyst, voice, grounding, cards, Wonder review), operator risk questionnaire, defined-risk trade-ideas screener, Muse chat transport with bearer auth, health endpoints, tool registry, and drill harness, local scheduled runner (cron/launchd) with persistent JSON store, bundle reload in the live server, multi-underlying briefs, ogg-to-WAV voice conversion, options-intent gating on the saved profile, multi-leg trade structures, Tradier live venue, broker combo support for multi-leg live orders, CI pytest workflow, wheel premium screener with IV rank plus risk caps, auto paper trade per session with one-position guard, gamma chart plus regime board pages, wall-migration brief section, LLM tool router, Discord/Telegram alert pushes, earnings overlay on ideas, one-click replay demo, 409-test suite with a socket-level server smoke test.

Not yet implemented: `live_enabled` defaults to `False`.
