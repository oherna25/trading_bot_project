# Dashboard brief

## Sub-features

- `GET /dashboard?underlying=SPY` renders `dashboard_html` (bot/dashboard.py)
  from stored snapshots: positioning brief plus tool-action forms.
- `GET /board` renders the multi-symbol regime board; `GET /board/live`
  returns the live table fragment.
- `GET /chart?underlying=SPY` renders the gamma chart; `GET /shock` renders
  shock repricing (`/shock?underlying=SPY&pct=-2`).
- Empty store returns 400 with setup text instead of a page.

## How to get to it (user POV)

The operator opens `/dashboard?underlying=SPY` in any HTTP client to read
the current positioning brief, then follows links/forms to board, chart,
shock, cards, and tool actions.

## Driving it with plain HTTP + terminal

```bash
BASE=http://127.0.0.1:8123
curl -s -o /tmp/dash.html -w "%{http_code} %{size_download}\n" "$BASE/dashboard?underlying=SPY"
grep -o "<title>[^<]*" /tmp/dash.html | head -2
curl -s -o /dev/null -w "board %{http_code}\n" "$BASE/board"
curl -s -o /dev/null -w "chart %{http_code}\n" "$BASE/chart?underlying=SPY"
```

Expect: dashboard 200 with `<!doctype html>`; board/chart 200 once the
scratch bundle is seeded.

## Gotchas

- Dashboard needs at least one stored snapshot; unseeded store gives 400
  (`empty_store_text()`). Seed with `scripts/seed_offline.py`.
- `_page_underlying` falls back to the stored settings default when
  `underlying=` is absent; pass it explicitly for determinism.
- `/shock` requires a numeric `pct`; missing/non-numeric gives 400 with a
  usage message. Unknown underlying gives 400 naming the symbol
  (by design: naming a ticker with no stored snapshot errors instead of
  falling back).
