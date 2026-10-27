# Trading Bot

A research-first trading system that grows into gated autonomy one verified phase at a time. It ingests options chains and stock bars from free-tier market APIs, computes its own GEX/VEX exposure profiles from stored snapshots, answers research questions in chat, and journals every signal, intent, and fill. Paper trading is implemented. Live ordering is disabled by default and has no venue yet.

The plan lives in [init.md](init.md). The design pseudocode lives in [overview.md](overview.md). Diagrams live in [diagrams/](diagrams/).

## How it works

Data flows one way, and every step writes to the journal:

```
market APIs ──▶ feeds ──▶ store ──▶ exposure ──▶ research ──▶ risk ──▶ paper venue
                  │          │          │             │           │
                  └──────────┴──────────┴─────────────┴───────────┴──▶ journal
```

1. **Feeds** (`bot/feeds.py`) pull options chains and daily bars from five sources through a registry table keyed by source name. Each adapter normalizes vendor JSON into `ChainSnapshot` or `Bar` records stamped with source name and latency.
2. **Store** (`bot/store.py`) is an in-memory snapshot and bar store. `read_latest` serves the research loop.
3. **Exposure** (`bot/exposure.py`) computes GEX and VEX from stored chains: signed Greek × open interest × multiplier per contract, summed per strike, plus the flip point where cumulative exposure changes sign. No vendor GEX feed is required.
4. **Research** (`bot/research.py`) is the chat loop. It reads the latest snapshot, computes the profile, runs the analyst tool, derives display fields (status, trigger, entry band, checklist, coverage), and returns a text brief. Dashboard requests route to a snapshot-pinned link builder instead.
5. **Risk** (`bot/risk.py`) sizes intents from account size × risk per trade (1% default), validates every intent against schema, halt/kill flags, and trade/day targets, and submits fills to the paper venue. A daily-loss breach halts the day.
6. **Autonomy** (`bot/autonomy.py`) gates promotion: clean paper days plus bounded drawdown plus passed halt drills yields `ready_for_checklist`. Live ordering ships disabled and needs an explicit signed action. The kill switch flattens auto-ordering and needs an operator reset.
7. **Dashboard** (`bot/dashboard.py`) builds a read-only screen (header, alignment strip, conviction grid, options strip, top contract card, positions, journal) from the same store and journal as chat. It has no entry point of its own; chat hands out the link.
8. **Replay** (`bot/replay.py`) replays stored snapshots in time order with no lookahead for backtests.

## Modules

| Module | Owns |
|---|---|
| `bot/shapes.py` | Frozen dataclasses: `Bar`, `OptionContract`, `ChainSnapshot`, `GexProfile`, `VexProfile`, `Signal`, `OrderIntent`, `Fill`, `Position`, `RiskState`, `JournalEntry`, `DashboardLink`, `OperatorConfig`, `RiskLimits`, `PaperEvidence` |
| `bot/feeds.py` | `FeedContext`, five adapters, `FEED_REGISTRY`, kind/fallback tables, `ingest_chain`, `ingest_bars`, `live_context`, `default_context` |
| `bot/store.py` | `SnapshotStore`: snapshots plus bars, latest-reads |
| `bot/journal.py` | `Journal`: append-only timestamped event log |
| `bot/exposure.py` | `compute_gex`, `compute_vex` |
| `bot/research.py` | `route_chat`, `handle_chat_query`, `scheduled_brief`, `live_brief`, dashboard links |
| `bot/risk.py` | `build_intent`, `review_intent`, `submit_paper`, halt/kill transitions |
| `bot/autonomy.py` | `check_promotion`, `enable_live`, `kill_switch`, `reset_autonomy` |
| `bot/dashboard.py` | `dashboard_view` plus panel builders |
| `bot/replay.py` | `replay_week` |

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
TRADIER_BASE_URL=https://api.tradier.com/v1   # optional; default is the sandbox host
APCA_API_KEY_ID=<id>
APCA_API_SECRET_KEY=<secret>
FINNHUB_API_KEY=<key>
MARKETDATA_API_KEY=<key>
```

The bot reads `.env` from the working directory automatically; real environment variables win over file values. Sandbox tokens only work on the sandbox host and production tokens only on production (`TRADIER_BASE_URL` selects the host). Secrets never appear in code, journals, or error messages.

## How to run

Prerequisites: Python 3.11+.

```bash
python -m venv .venv
.venv/bin/pip install pytest
```

Run the suite (fully offline, no network):

```bash
.venv/bin/python -m pytest tests/ -q
```

There is no chat transport, scheduler, or server yet; you drive the library from Python. Offline research brief with canned data:

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

## Status and roadmap

Implemented: five live feeds with fallback, GEX/VEX engine, research chat loop plus scheduled briefs, snapshot-pinned dashboard links, dashboard view models, paper trading with halt logic, autonomy gate with kill switch, replay harness, 42-test offline suite.

Not yet implemented: a chat transport (Telegram), a scheduler and persistent store (Modal), an LLM analyst (the current analyst is a deterministic flip rule), and the live order venue (`submit_live` raises until Phase 4). `live_enabled` defaults to `False`.
