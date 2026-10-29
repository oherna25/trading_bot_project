# Trading Bot

A research-first trading system that grows into gated autonomy one verified phase at a time. It ingests options chains and stock bars from free-tier market APIs, computes its own GEX/VEX exposure profiles from stored snapshots, answers research questions in chat, and journals every signal, intent, and fill. Paper trading is implemented. Live ordering is disabled by default and routes to a Tradier venue once the operator enables it with a signed action.

The plan lives in [init.md](init.md). The design pseudocode lives in [overview.md](overview.md). Diagrams live in [diagrams/](diagrams/).

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
2. **Store** (`bot/store.py`) is an in-memory snapshot and bar store. `read_latest` serves the research loop.
3. **Exposure** (`bot/exposure.py`) computes GEX and VEX from stored chains: signed Greek × open interest × multiplier per contract, summed per strike, plus the flip point where cumulative exposure changes sign. No vendor GEX feed is required.
4. **Research** (`bot/research.py`) is the chat loop. It reads the latest snapshot, computes the profile, runs the analyst tool, derives display fields (status, trigger, entry band, checklist, coverage), and returns a text brief. Dashboard requests route to a snapshot-pinned link builder instead. The analyst tool routes to a Muse Spark backend behind a role registry when live_models=True; otherwise the deterministic rule runs as the offline fallback (Phase M1). Grounding appends a web-searched note to theses when grounding=True, and voice notes transcribe through bot/voice.py and route as chat queries (Phase M2). Brief cards append a card line when cards=True, and advisory_review gives a standalone Wonder pre-promotion read (Phase M3). Profile messages print a four-question risk questionnaire or save a one-shot OperatorProfile with suggested risk params. Idea requests return up to 10 ranked TradeIdeas filtered by timeframe words and named stocks.
5. **Risk** (`bot/risk.py`) sizes intents from account size × risk per trade (1% default), validates every intent against schema, halt/kill flags, and trade/day targets, and submits fills to the paper venue. A daily-loss breach halts the day.
6. **Autonomy** (`bot/autonomy.py`) gates promotion: clean paper days plus bounded drawdown plus passed halt drills yields `ready_for_checklist`. Live ordering ships disabled and needs an explicit signed action. The kill switch flattens auto-ordering and needs an operator reset.
7. **Dashboard** (`bot/dashboard.py`) builds a read-only screen (header, alignment strip, conviction grid, options strip, top contract card, positions, journal) from the same store and journal as chat. It has no entry point of its own; chat hands out the link.
8. **Replay** (`bot/replay.py`) replays stored snapshots in time order with no lookahead for backtests.

## Modules

| Module | Owns |
|---|---|
| `bot/shapes.py` | Frozen dataclasses: `Bar`, `OptionContract`, `ChainSnapshot`, `GexProfile`, `VexProfile`, `Signal`, `OrderIntent`, `OrderLeg`, `Fill`, `Position`, `RiskState`, `JournalEntry`, `DashboardLink`, `OperatorConfig`, `RiskLimits`, `PaperEvidence`, `ModelReceipt`, `OperatorProfile`, `TradeIdea` |
| `bot/feeds.py` | `FeedContext`, five adapters, `FEED_REGISTRY`, kind/fallback tables, `ingest_chain`, `ingest_bars`, `live_context`, `default_context` |
| `bot/store.py` | `SnapshotStore`: snapshots plus bars plus saved profile, latest-reads, underlying listing |
| `bot/journal.py` | `Journal`: append-only timestamped event log |
| `bot/persist.py` | `save_bundle`, `load_bundle`: JSON snapshots plus bars plus journal plus profile |
| `bot/schedule.py` | `run_scheduled_session`: ingest plus brief plus persist in one job |
| `bot/exposure.py` | `compute_gex`, `compute_vex` |
| `bot/research.py` | `route_chat`, `handle_chat_query`, `scheduled_brief`, `live_brief`, dashboard links, profile questionnaire, trade ideas, `advisory_review` |
| `bot/risk.py` | `build_intent`, `even_legs`, `review_intent` with profile gating, `submit_paper`, halt/kill transitions |
| `bot/autonomy.py` | `check_promotion`, `enable_live`, `kill_switch`, `reset_autonomy`, `submit_live` |
| `bot/venue.py` | Tradier live venue: `occ_symbol`, equity plus single-option orders, `LiveAck` |
| `bot/dashboard.py` | `dashboard_view` plus panel builders |
| `bot/replay.py` | `replay_week` |
| `bot/models.py` | `MODEL_REGISTRY`, Spark tool loop, `ModelReceipt`, day-budget gate |
| `bot/voice.py` | Ogg-to-WAV conversion plus 16kHz WAV transcription into chat commands, `voice_transcript` journaling |
| `bot/cards.py` | Regime card art with engine numbers, `brief_card` journaling |
| `bot/profile.py` | Risk questionnaire table, answer parsing, `OperatorProfile`, suggested params |
| `bot/ideas.py` | Timeframe and symbol parsing, ranked top-10 `TradeIdea` screener |
| `bot/muse.py` | Muse chat transport: `parse_chat_payload`, `handle_chat`, `TOOL_REGISTRY`, `dispatch_tool`, `openapi_spec`, `route_http`, `serve`, drill `Harness` |

## Feeds

| Feed | Kind | Credential | Role |
|---|---|---|---|
| `tradier_sandbox` | chain | `TRADIER_ACCESS_TOKEN` | Primary chain. Sandbox is delayed ~15 min without Greeks; production has Greeks |
| `marketdata_eod` | chain | `MARKETDATA_API_KEY` | EOD chain seed and fallback. 100 free credits/day |
| `alpaca` | bars | `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY` | Primary daily bars (IEX rail) |
| `finnhub` | bars | `FINNHUB_API_KEY` | Fallback quote (single fresh bar; candle history is plan-gated) |
| `yfinance` | bars | none | Unkeyed backup |

Fallback order is fixed tables: chains try Tradier then Market Data; bars try Alpaca, Finnhub, then Yahoo. `live_brief` journals each failure with the error class name only, raises `FeedUnavailableError` when every chain feed fails, and treats bars as best effort (the brief works without a bar, with direction `flat`).

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
```

The bot reads `.env` from the working directory automatically; real environment variables win over file values. Sandbox tokens only work on the sandbox host and production tokens only on production (`TRADIER_BASE_URL` selects the host). Secrets never appear in code, journals, or error messages. Model phases add `META_MODEL_API_KEY` and `MODEL_DAY_BUDGET_USD`; without both set, the deterministic paths run (Phase M0).

## How to run

### Prerequisites

Python 3.11+. All commands run from the repo root. Create the venv and install the test runner:

```bash
python -m venv .venv
.venv/bin/pip install pytest
```

### Configuration

Copy the keys from the [Configuration](#configuration) section into a `.env` file in the repo root (never commit it): `TRADIER_ACCESS_TOKEN`, `TRADIER_ACCOUNT_ID`, `TRADIER_BASE_URL` (optional), `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `FINNHUB_API_KEY`, `MARKETDATA_API_KEY`, plus `META_MODEL_API_KEY` and `MODEL_DAY_BUDGET_USD` for the model phases. The bot reads `.env` from the working directory automatically; real environment variables win over file values. Without keys, the suite and every offline flow still run on canned fixtures with no network.

### Test suite

Fully offline, no network:

```bash
.venv/bin/python -m pytest tests/ -q
```

### Full app in one script

`run_app.py` loads the JSON bundle, prints the live brief, saves, then serves the Muse HTTP surface (blocks serving). On a feed failure it prints the failure, still saves, and still serves the loaded state.

```bash
.venv/bin/python run_app.py
.venv/bin/python run_app.py --underlying SPY --store-dir data/trading-bot --host 0.0.0.0 --port 8000
```

| Flag | Default | Meaning |
|---|---|---|
| `--underlying` | `SPY` | Symbol to brief |
| `--store-dir` | `data/trading-bot` | JSON bundle directory |
| `--host` | `0.0.0.0` | HTTP bind host |
| `--port` | `8000` | HTTP bind port |

A Muse bot instance hooks to http://HOST:PORT/chat.

### Scheduled session by hand

`run_session.py` runs the scheduled session on this machine against a local JSON bundle in `data/trading-bot`; `run_scheduled_session` in `bot/schedule.py` is the same job for library drives. It prints the brief to stdout.

```bash
.venv/bin/python run_session.py
.venv/bin/python run_session.py --underlying SPY --store-dir data/trading-bot
.venv/bin/python run_session.py --help
```

| Flag | Default | Meaning |
|---|---|---|
| `--underlying` | `SPY` | Symbol to brief |
| `--store-dir` | `data/trading-bot` | JSON bundle directory |

### Scheduled session on a timer

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

`bot.muse.serve(store, journal, config)` exposes the same surface as the drill harness over HTTP. It binds `0.0.0.0:8000` by default so external bots can connect over the network; pass `host`/`port` to override.

| Endpoint | Method | Body |
|---|---|---|
| `/chat` | POST | `{"text": "...", "underlying": "SPY"}` (`live_models`, `grounding`, `cards` optional, default false) |
| `/mcp` | POST | `{"method": "tools/list"}` or `{"method": "tools/call", "params": {"name": "...", "arguments": {}, "underlying": "SPY"}}` |
| `/openapi.json` | GET | none |

MCP tools: `get_gex_brief`, `get_dashboard_link`, `get_trade_ideas`.

Start it (seeded with canned data so `/chat` answers immediately; blocks serving):

```python
from bot.feeds import ingest_bars, ingest_chain
from bot.journal import Journal
from bot.muse import serve
from bot.shapes import OperatorConfig
from bot.store import SnapshotStore

store, journal = SnapshotStore(), Journal()
config = OperatorConfig(
    dashboard_base_url="https://ops.example.internal",
    dashboard_link_expiry_seconds=3600,
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
    dashboard_base_url="https://ops.example.internal",
    dashboard_link_expiry_seconds=3600,
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)
ingest_chain("tradier_sandbox", store, journal)
ingest_bars("alpaca", store, journal)
print(route_chat("give me the gex brief", "SPY", store, journal, config, now))
print(route_chat("show me the dashboard", "SPY", store, journal, config, now))
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
if review_intent(intent, state, limits, journal, now) == "approved":
    fill, position, state = submit_paper(intent, 769.7, 769.7, state, None, limits, journal, now)
```

Backtest replay over stored snapshots:

```python
from bot.replay import replay_week
journal = replay_week([snapshot_day2, snapshot_day0, snapshot_day1], Journal())
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

`save_bundle(store_dir, store, journal)` writes four JSON files into the store directory: `snapshots.json`, `bars.json`, `journal.json`, and `profile.json`. Each write goes to a `.tmp` file first and is renamed into place, so a crash never leaves a half-written bundle. `load_bundle(store_dir)` returns a `(store, journal)` pair and tolerates missing files, so a fresh directory just yields empty state. Every scheduled run persists to `data/trading-bot`. Inspect the journal any time via `journal.entries()`:

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

Any chat containing `"idea"` returns up to 10 ranked ideas. Filter by timeframe words (`weekly`, `monthly`, `quarterly`, or `"N days"`) or by naming a stored underlying:

```python
print(route_chat("weekly trade ideas", "SPY", store, journal, config, now))
print(route_chat("trade ideas for QQQ", "SPY", store, journal, config, now))
```

### Paper to live

Paper trade first (see the snippet above), then walk the promotion gate. `check_promotion` reads a `PaperEvidence` record and returns `"ready_for_checklist"` or `"stay_paper"`; `advisory_review` adds a standalone Wonder pre-promotion read when the model backend is available:

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
print(review_intent(intent, state, limits, journal, now, profile=store.read_profile()))
```

Options legs need the saved profile at review (`options_ok` must be yes); without it `review_intent` vetoes the combo. Paper covers combos end to end. Live `place_order` rejects multi-leg intents until broker combo support lands.

## Status and roadmap

Implemented: five live feeds with fallback, GEX/VEX engine, research chat loop plus scheduled briefs, snapshot-pinned dashboard links, dashboard view models, paper trading with halt logic, autonomy gate with kill switch, replay harness, Muse program M0-M4 (Spark analyst, voice, grounding, cards, Wonder review), operator risk questionnaire, ranked trade-ideas screener, Muse chat transport with tool registry and drill harness, local scheduled runner (cron/launchd) with persistent JSON store, ogg-to-WAV voice conversion, options-intent gating on the saved profile, multi-leg trade structures, Tradier live venue, 188-test offline suite.

Not yet implemented: broker combo support for multi-leg live orders (`place_order` rejects combos; paper covers them). `live_enabled` defaults to `False`.
