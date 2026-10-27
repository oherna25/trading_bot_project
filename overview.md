# Trading System Overview

Design-only pseudocode for the research-first trading system. No implementation.
The plan lives in `init.md`. Diagrams live in `diagrams/`.

Dashboard rule: the dashboard has no standalone entry point. The operator asks
the bot for the dashboard in chat and the bot replies with a link pinned to the
snapshot under discussion.

## Data shapes

```
SHAPE Bar
  interval
  timestamp_utc
  open
  high
  low
  close
  volume
END SHAPE

SHAPE OptionContract
  strike
  expiry
  call_or_put
  open_interest
  gamma
  vega
  price
  bid
  ask
  iv
END SHAPE

SHAPE ChainSnapshot
  underlying
  timestamp_utc
  source_name
  latency_ms
  contracts_list
END SHAPE

SHAPE GexProfile
  snapshot_id
  per_strike_exposure
  total_exposure
  flip_point
END SHAPE

SHAPE VexProfile
  snapshot_id
  per_strike_exposure
  total_exposure
END SHAPE

SHAPE Signal
  direction
  thesis
  confidence
  snapshot_id
END SHAPE

SHAPE OrderIntent
  symbol
  side
  size
  risk_numbers
  signal_id
END SHAPE

SHAPE Fill
  price
  size
  timestamp_utc
  venue
END SHAPE

SHAPE Position
  symbol
  net_size
  average_price
  unrealized_pnl
END SHAPE

SHAPE RiskState
  account_size
  risk_per_trade
  day_pnl
  halt_active
  kill_active
END SHAPE

SHAPE JournalEntry
  timestamp_utc
  kind
  payload
END SHAPE

SHAPE DashboardLink
  snapshot_id
  url
  created_utc
  expires_utc
END SHAPE
```

## Organizing structure

- Feeds use a registry table keyed by source name.
- Risk and halt use a state machine with explicit transitions.
- All flows pass typed shapes by name and never rebuild fields inline.

## Ingestion

```
FUNCTION ingest_chain WITH feed_name
  raw = pull_feed feed_name
  snapshot = normalize raw INTO ChainSnapshot
  stamp snapshot WITH source_name AND latency
  store snapshot
  RETURN snapshot
END FUNCTION

FUNCTION pull_feed WITH feed_name
  IF feed_name IS tradier_sandbox THEN RETURN delayed_chain
  IF feed_name IS alpaca THEN RETURN live_stocks
  IF feed_name IS finnhub THEN RETURN fallback_stocks
  IF feed_name IS marketdata_eod THEN RETURN history_seed
  IF feed_name IS yfinance THEN RETURN backup_fields
  RETURN empty
END FUNCTION
```

## Exposure engine

```
FUNCTION compute_gex WITH snapshot
  total = 0
  FOR EACH contract IN snapshot contracts_list
    sign = 1 IF contract call_or_put IS call ELSE -1
    exposure = sign TIMES contract gamma TIMES contract open_interest TIMES multiplier
    ADD exposure TO per_strike
    ADD exposure TO total
  END FOR
  flip = strike WHERE cumulative_sign_changes
  profile = BUILD GexProfile WITH snapshot_id AND per_strike AND total AND flip
  RETURN profile
END FUNCTION

FUNCTION compute_vex WITH snapshot
  total = 0
  FOR EACH contract IN snapshot contracts_list
    sign = 1 IF contract call_or_put IS call ELSE -1
    exposure = sign TIMES contract vega TIMES contract open_interest TIMES multiplier
    ADD exposure TO per_strike
    ADD exposure TO total
  END FOR
  profile = BUILD VexProfile WITH snapshot_id AND per_strike AND total
  RETURN profile
END FUNCTION
```

## Replay

```
FUNCTION replay_week WITH snapshot_list
  SORT snapshot_list BY timestamp_utc ASCENDING
  FOR EACH snapshot IN snapshot_list
    profile = compute_gex snapshot
    journal APPEND event replay_step WITH snapshot
  END FOR
  RETURN journal
END FUNCTION
```

## Research chat

Chat is the entry point. Dashboard requests route to the link builder.
All other requests run the analyst tools.

```
FUNCTION route_chat WITH text
  IF text asks_for_dashboard THEN RETURN dashboard_link_response
  RETURN analyst_response text
END FUNCTION

FUNCTION handle_chat_query WITH text
  snapshot = read_latest ChainSnapshot
  profile = compute_gex snapshot
  signal = analyst_tool text WITH snapshot AND profile
  journal APPEND signal WITH snapshot_id
  reply = format_brief profile AND signal
  RETURN reply
END FUNCTION

FUNCTION scheduled_brief
  snapshot = read_latest ChainSnapshot
  profile = compute_gex snapshot
  brief = format_brief profile
  journal APPEND event brief_sent
  SEND brief TO operator_chat
END FUNCTION

FUNCTION dashboard_link_response
  snapshot = read_latest ChainSnapshot
  link = build_dashboard_link snapshot
  journal APPEND event dashboard_link_sent WITH snapshot
  RETURN reply WITH link url
END FUNCTION

FUNCTION build_dashboard_link WITH snapshot
  path = dashboard_path_for snapshot snapshot_id
  url = dashboard_base_url PLUS path
  link = BUILD DashboardLink WITH snapshot_id AND url AND timestamps
  RETURN link
END FUNCTION
```

The dashboard base url and link expiry are operator config. No dashboard
framework is named, so no host is hardcoded.

## Paper trading and risk

Position size is account size times risk per trade with a one percent default.
Every intent is journaled with its risk numbers before submission.

```
FUNCTION build_intent WITH signal AND risk_state
  size = risk_state account_size TIMES risk_state risk_per_trade
  intent = BUILD OrderIntent WITH signal AND size
  journal APPEND intent AS proposed
  RETURN intent
END FUNCTION

FUNCTION review_intent WITH intent AND risk_state
  IF intent schema_invalid THEN journal APPEND veto AND RETURN rejected
  IF risk_state halt_active THEN journal APPEND block AND RETURN rejected
  IF risk_state kill_active THEN journal APPEND block AND RETURN rejected
  IF intent outside trade_targets THEN journal APPEND veto AND RETURN rejected
  IF intent outside day_targets THEN journal APPEND veto AND RETURN rejected
  journal APPEND intent AS approved
  RETURN approved
END FUNCTION

FUNCTION submit_paper WITH intent
  fill = paper_venue SUBMIT intent
  journal APPEND fill
  position = update_position fill
  risk_state = update_day_pnl fill
  IF day_loss_breached risk_state THEN risk_state halt_active = true
  IF day_loss_breached risk_state THEN journal APPEND halt
  RETURN fill
END FUNCTION
```

## Autonomy gate

The live adapter ships disabled. The operator enables it with an explicit
action after signing the go-live checklist.

```
FUNCTION check_promotion WITH paper_evidence
  IF clean_days_below_target THEN RETURN stay_paper
  IF drawdown_above_bound THEN RETURN stay_paper
  IF halt_drills_not_passed THEN RETURN stay_paper
  RETURN ready_for_checklist
END FUNCTION

FUNCTION enable_live WITH checklist_signature
  IF checklist_signature missing THEN RETURN stay_disabled
  IF kill_active THEN RETURN stay_disabled
  live_enabled = true
  journal APPEND event live_enabled
  RETURN live_enabled
END FUNCTION

FUNCTION kill_switch
  live_enabled = false
  halt_active = true
  journal APPEND event kill_fired
  REQUIRE operator_reset BEFORE any_new_intent
END FUNCTION
```

## Dashboard

Layout follows the reference card in `HVkDkv6XIAAaDgb (1).jpeg` from top to
bottom. The dashboard reads the same snapshot store and journal as chat. It is
reached only through the chat-provided link, pinned to one snapshot id.

Panels in order with bindings:

1. Header band. Rank chip plus scan name from the ranked Signal list.
   Underlying logo plus symbol plus company name from ChainSnapshot
   underlying plus the instrument master. Status pill from Signal status.
   Price plus day change from the latest Bar close versus the prior close.
2. Alignment strip. Checklist dots plus aligned count from Signal checklist
   outcomes. Data coverage percent from snapshot freshness plus contract
   coverage.
3. Conviction grid. Conviction from Signal confidence. Trigger name from
   Signal thesis. Confirmation score plus band from the checklist score.
   Entry band from price distance to trigger.
4. Options strip. Qualifying count from ChainSnapshot contracts passing the
   selection filters. Regime band from the analyst read over the snapshot
   plus GexProfile.
5. Top contract card. Contract header from OptionContract strike plus expiry
   plus call-or-put plus days to expiry. Delta plus OI plus bid-to-ask
   percent plus bid cross ask plus contract IV from OptionContract quote
   fields. Quality band from spread plus OI. Explainer from Signal thesis
   plus selection reasons.

Positions plus journal attach below the card per the Phase 5 scope. Signal
display fields of status plus trigger plus entry band plus checklist plus
coverage derive at brief time and journal with the signal.

```
FUNCTION dashboard_view WITH snapshot_id
  snapshot = read_snapshot snapshot_id
  profile = compute_gex snapshot
  signal = read_signal_for snapshot_id
  bar = read_latest Bar FOR snapshot underlying
  header = build_header signal AND snapshot AND bar
  strip = build_alignment signal AND snapshot
  grid = build_conviction signal AND bar
  options = build_options snapshot AND profile
  card = build_top_contract options AND signal
  positions = read_all Position
  journal_view = read_journal
  RETURN screen WITH header AND strip AND grid AND options AND card AND positions AND journal_view
END FUNCTION
```
