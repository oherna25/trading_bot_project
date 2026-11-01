# Offline chat

## Sub-features

- `POST /chat` with JSON `{"text": ..., "underlying": "SPY"}` routes via
  `parse_chat_payload` / `handle_chat` (`bot/muse_chat.py`) to `route_chat`.
- Flags `live_models` / `grounding` / `cards` / `llm_router` all default
  `False`: the deterministic offline path always works with no keys.
- Replies are `{"ok": true, "reply": ...}`; unknown tickers or bad payloads
  return `{"ok": false, ...}` or 400 `missing chat text`.
- Example deterministic queries: `gex brief`, `brief`, dashboard-style
  positioning questions answered from stored snapshots.

## How to get to it (user POV)

The operator sends a chat message (text plus optional underlying) and gets
a positioning reply computed from stored snapshots, with no model calls
and no journal `model_receipt` entries on the offline path.

## Driving it with plain HTTP + terminal

```bash
BASE=http://127.0.0.1:8123
curl -s -X POST $BASE/chat -H 'Content-Type: application/json' \
  -d '{"text":"gex brief","underlying":"SPY"}' | .venv/bin/python -m json.tool
```

Expect: 200, `"ok": true`, and `reply` containing `total exposure:`.
Empty `text` gives 400 `missing chat text`; unknown underlying gives
`"ok": false` naming the symbol.

## Gotchas

- `MuseChatRequest.underlying` defaults to `SPY` when missing/blank; always
  pass it explicitly when verifying so the reply is attributable.
- Setting `llm_router: true` or `live_models: true` leaves the offline
  path and needs `META_MODEL_API_KEY`; keep them absent/`false` here.
- Chat auth: with `MUSE_AUTH_TOKEN` set, `/chat` needs
  `Authorization: Bearer <token>`; offline verification leaves it unset
  (loopback bind `127.0.0.1` avoids the open-server warning).
