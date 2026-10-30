# Trading System Overview

Design-only pseudocode for the research-first trading system. No implementation.
The plan lives in `init.md`. Diagrams live in `diagrams/`.

Trade cards rule: every recommended trade gets one card with stats plus a
one-line reason stating why it is a good trade. Cards surface in chat text
and on the /cards page, both built from the same ranked ideas.

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

SHAPE TradeCard
  symbol
  direction
  spread
  strike
  expiry
  call_or_put
  hedge_strike
  confidence
  snapshot_id
  max_loss
  breakeven
  net_debit
  spread_pct
  wall_exposure
  regime
  spot
  flip_point
  reason
  reason_source
END SHAPE

SHAPE ModelReceipt
  model_id
  role_name
  prompt_hash
  tokens_in
  tokens_out
  cost_usd
  latency_ms
END SHAPE

SHAPE PremiumCandidate
  symbol
  action
  strike
  expiry
  dte
  bid
  ask
  delta
  iv
  open_interest
  premium_return
  capital_at_risk
  needs_shares
  iv_rank
  snapshot_id
END SHAPE

SHAPE PortfolioView
  open_positions
  open_symbols
  peak_day_pnl
END SHAPE
```

## Organizing structure

- Feeds use a registry table keyed by source name.
- Risk and halt use a state machine with explicit transitions.
- All flows pass typed shapes by name and never rebuild fields inline.
- Models use a registry table keyed by role name, mirroring the feed registry.
- Every model call journals a ModelReceipt. A missing key or budget selects the deterministic path.

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

Chat is the entry point. Card requests route to the card builder.
All other requests run the analyst tools.

```
FUNCTION route_chat WITH text
  IF text asks_for_cards THEN RETURN cards_response
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

FUNCTION analyst_backend WITH text AND snapshot AND profile AND bar
  IF model_key_set AND budget_remaining THEN RETURN spark_tool_call text WITH snapshot AND profile AND bar
  journal APPEND event model_fallback WITH reason
  RETURN flip_rule text WITH snapshot AND profile AND bar
END FUNCTION

FUNCTION spark_tool_call WITH text AND snapshot AND profile AND bar
  receipt = CALL model_role analyst WITH snapshot AND profile
  journal APPEND receipt AS model_receipt
  signal = BUILD Signal FROM receipt thesis WITH snapshot_id
  RETURN signal
END FUNCTION

FUNCTION scheduled_brief
  snapshot = read_latest ChainSnapshot
  profile = compute_gex snapshot
  brief = format_brief profile
  journal APPEND event brief_sent
  SEND brief TO operator_chat
END FUNCTION

FUNCTION cards_response WITH text
  IF no saved operator profile THEN RETURN profile setup prompt
  entries = latest snapshots FOR text symbols
  cards = build_trade_cards entries
  journal APPEND event ideas WITH card count
  RETURN format_trade_cards cards
END FUNCTION

FUNCTION build_trade_cards WITH entries
  ideas = rank_ideas entries
  FOR EACH idea IN ideas
    stats = idea numbers PLUS wall PLUS regime PLUS spot AND flip
    reason = meta_reason stats ELSE fallback_reason stats
    journal APPEND event trade_card_reason OR reason_fallback
    card = BUILD TradeCard WITH stats AND reason
  END FOR
  RETURN cards
END FUNCTION

FUNCTION meta_reason WITH stats
  IF meta key set THEN CALL Meta chat WITH stats
  IF call fails OR reason empty THEN RETURN fallback_reason stats
  RETURN reason
END FUNCTION

FUNCTION handle_voice_note WITH audio
  transcript = CALL model_role transcriber WITH audio
  journal APPEND event voice_transcript WITH transcript
  RETURN handle_chat_query WITH transcript
END FUNCTION

FUNCTION render_brief_card WITH signal AND profile
  art = CALL model_role artist WITH signal regime
  card = DRAW numbers FROM signal AND profile ONTO art
  journal APPEND event brief_card WITH card reference
  RETURN card
END FUNCTION

FUNCTION advisory_review WITH paper_evidence
  note = CALL model_role reviewer WITH paper_evidence
  journal APPEND note AS review_note
  RETURN note
END FUNCTION
```

The review note is advisory only. The autonomy gate keeps its deterministic checks.

The Meta key selects the reason backend. The Meta analyst model writes
each card reason when it is set, else the offline fallback runs.

## Wheel screening

```
FUNCTION screen_premium WITH entries
  FOR EACH symbol IN entries
    expiries = nearest 3 IN 7-30 dte, Fridays first
    SKIP expiry WHEN earnings_map date spans it
    FOR EACH contract ON expiries
      DROP unless put_or_call AND oi_above_100 AND spread_ok
      DROP unless delta 0.15-0.30 WITH sign matching side
      DROP call WHEN no bar
      DROP unless premium_over_capital_above_1pct
    END FOR
    DROP symbol survivors WHEN iv_rank_below_30 WITH 20-plus points
  END FOR
  SORT survivors BY premium_return DESCENDING WITH ties BY symbol strike expiry side
  RETURN top 10 AND drop_counts BY stage
END FUNCTION

FUNCTION wheel_response WITH text
  entries = latest snapshots FOR text symbols WITH bars, no gex profiles
  candidates = screen_premium entries
  RECORD per-symbol average candidate iv INTO store
  journal APPEND event iv_recorded AND wheel_screened
  RETURN format_wheel candidates
END FUNCTION
```

Wheel output is research, so the profile gate never applies. Risk caps
ride an optional PortfolioView: max positions, drawdown pause off the
peak day PnL, and a per-sector cap over known tickers.

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

## Trade cards

One card per recommended trade, built from the same ranked ideas as the
ideas reply. Chat answers card queries with text cards. The /cards page
renders one article per card with a stat list plus a reason paragraph.
Every card surface needs a saved operator profile first.

Card fields in order with bindings:

1. Title. Symbol plus direction plus spread plus expiry from the TradeIdea.
2. Stat list. Confidence plus max loss plus breakeven plus net debit or
   credit plus spread width pct from the TradeIdea. Wall exposure from the
   GexProfile strike row. Regime from the profile total sign. Spot from the
   latest Bar close versus the profile flip point.
3. Reason. One line stating why it is a good trade, written by the Meta
   analyst model when configured, else the deterministic offline fallback.

Card stats match the engine numbers on the same snapshot. Reasons journal
as trade_card_reason on model success and reason_fallback otherwise.

```
FUNCTION cards_page WITH underlying
  snapshots = read_latest ChainSnapshot FOR underlying
  cards = build_trade_cards snapshots
  RETURN page WITH one article PER card
END FUNCTION
```

## Standout model features (M5)

Ten Muse-powered features share one discipline: every model call
journals a ModelReceipt, honors the day budget, and degrades to a
deterministic path when keys, budget, or weights are missing. Local
calls cost zero and rescue failed hosted calls.

New shapes:

```
SHAPE TierDecision
  task
  tier
  role_name
  model_id
  reason
END SHAPE

SHAPE LocalSpec
  backend_name
  model_id
  weights_env_var
END SHAPE

SHAPE PortfolioAction
  action
  threshold_pct
  symbol
  kind
  strike
  to_expiry
  confirm
END SHAPE

SHAPE ImageRegion
  x
  y
  width
  height
  label
END SHAPE
```

Organizing structure:

- Tasks use a class table keyed by task name with draft, deep, and
  premium classes. Each class names its tier order over the local
  backend, Spark, and Wonder.
- The local backend uses a registry table keyed by backend name,
  mirroring the model registry. Glimmer weights win over Spark Open
  weights when both exist.
- Portfolio commands parse with deterministic rules first. The Spark
  tool loop only translates phrasings the rules reject, and the
  deterministic executor validates every translated action before
  running it through the risk reviewer and the paper venue.

```
FUNCTION select_model_tier WITH task
  class = class_table task WITH deep AS default
  FOR EACH step IN tier_order class
    IF step IS local AND weights_present THEN RETURN local decision
    IF step IS hosted AND backend_ready step THEN RETURN model decision
  END FOR
  RETURN fallback WITH last reason
END FUNCTION

FUNCTION local_analyst_signal WITH text AND snapshot AND profile AND bar
  reply = CALL local_backend analyst WITH prompt
  journal APPEND receipt AS model_receipt WITH zero cost
  signal = BUILD Signal FROM reply direction AND confidence
  RETURN signal
END FUNCTION

FUNCTION read_chart_image WITH image AND underlying AND question AND regions
  IF live_models_off THEN journal skip AND RETURN needs_models
  IF image_not_png_or_jpeg THEN journal skip AND RETURN rejected
  prompt = engine walls AND flip AND spot PLUS region hints
  reply = CALL model_role analyst WITH prompt AND image
  journal APPEND receipt AS model_receipt
  journal APPEND event chart_read
  RETURN reply
END FUNCTION

FUNCTION read_document WITH image AND question AND underlying
  regions = segment_image image WITH fail_soft_to_empty
  RETURN read_chart_image image WITH question AND regions
END FUNCTION

FUNCTION segment_image WITH image
  boxes = CALL sam_3_1 WITH image AND table_task
  journal APPEND receipt AS model_receipt
  RETURN clean boxes AS ImageRegion list
END FUNCTION

FUNCTION shock_news WITH underlying AND shock_pct
  facts = shock_text underlying WITH shock_pct
  IF no_snapshot OR no_bar THEN RETURN facts
  IF live_models_off THEN journal skip AND RETURN facts WITH pointer
  note = grounded_note WITH shock facts AND web_search
  journal APPEND event shock_news
  RETURN facts PLUS likely_driver note
END FUNCTION

FUNCTION handle_voice_holdings WITH transcript
  IF NOT holdings_command THEN RETURN none
  holdings = parse_spoken_holdings transcript
  journal APPEND event holdings_saved WITH voice source
  RETURN saved holdings text
END FUNCTION

FUNCTION handle_voice_kill WITH transcript
  IF NOT kill_command THEN RETURN none
  IF transcript confirms AND kill_armed_within_window THEN
    live_enabled = false
    journal APPEND event kill_fired WITH voice source
    RETURN fired text
  IF transcript confirms THEN journal reject AND RETURN no_armed_kill
  journal APPEND event kill_armed
  RETURN confirm prompt
END FUNCTION

FUNCTION coach_review WITH journal
  stats = count fills AND halts AND kills AND vetoes AND signals
  IF live_models_off THEN journal skip AND RETURN rules_coach stats
  decision = select_model_tier coach
  note = CALL decision_backend WITH stats
  journal APPEND receipt AS model_receipt
  journal APPEND event coach_note
  RETURN note
END FUNCTION

FUNCTION narrate_alert WITH text
  IF live_models_off THEN journal skip AND RETURN rules_narration text
  decision = select_model_tier narrate
  narration = CALL decision_backend WITH text
  journal APPEND receipt AS model_receipt
  RETURN narration WITH source tag
END FUNCTION

FUNCTION filter_noise WITH alerts
  DROP repeat stale alerts past first per day WITH alert_dropped
  IF live_models AND local_ready THEN
    DROP alerts the local judge answers NO WITH alert_dropped
  RETURN survivors
END FUNCTION

FUNCTION portfolio_response WITH text
  action = parse_portfolio_command text AS PortfolioAction
  IF action unknown AND live_models THEN action = spark_translate text
  IF action unknown THEN journal reject AND RETURN usage
  intents = build close intents FROM open_book journal fills
  FOR EACH intent
    verdict = review_intent intent
    IF approved THEN fill = submit_paper intent AT mark
  END FOR
  journal APPEND event portfolio_command
  RETURN paper_only lines
END FUNCTION

FUNCTION share_card WITH index AND underlying
  IF no saved operator profile THEN RETURN profile setup prompt
  card = demo_cards underlying AT index
  IF NOT live_models THEN journal skip AND RETURN caption text
  art = render_signal_art card WITH prompt_hash cache
  journal APPEND receipt AS model_receipt
  RETURN caption text WITH art hash
END FUNCTION
```
