# Paper P&L / track record

## Sub-features

- `GET /pnl?underlying=SPY` renders `paper_pnl_text(journal, store)` as an
  HTML action page: realized/unrealized paper P&L from journaled fills.
- `GET /trackrecord?underlying=SPY` renders `trackrecord_html`: per-trade
  history and aggregate stats from the journal.
- `GET /replay.csv?underlying=SPY&section=steps` exports replay evidence as
  CSV (`sections`: steps, fills, benchmark); unknown section gives 400.
- Journal is the source of truth: a missing journal write is a bug, so an
  empty journal legitimately shows an empty record.

## How to get to it (user POV)

After paper sessions run, the operator opens `/pnl` for the bottom line
and `/trackrecord` for the trade-by-trade history, or downloads
`/replay.csv` for offline analysis.

## Driving it with plain HTTP + terminal

```bash
BASE=http://127.0.0.1:8123
curl -s -o /dev/null -w "pnl %{http_code}\n" "$BASE/pnl?underlying=SPY"
curl -s -o /dev/null -w "trackrecord %{http_code}\n" "$BASE/trackrecord"
curl -s "$BASE/replay.csv?underlying=SPY&section=steps" | head -5
curl -s "$BASE/replay.csv?underlying=SPY&section=bogus"; echo
```

Expect: `/pnl` and `/trackrecord` 200 (HTML); `steps` CSV with a header
row; `bogus` 400 naming the valid sections.

## Gotchas

- Freshly seeded scratch bundles have no fills, so `/pnl` shows zeros and
  `/trackrecord` is empty: that is correct, not a failure. Drive fills via
  the paper-session feature first if non-empty output is needed.
- `/replay.csv` for an unknown underlying returns 400 (same no-fallback
  rule as `/chat` and `/cards`).
- CSV `Content-Disposition` names the file `replay-<SYM>-<section>.csv`;
  keep downloads inside the evidence dir.
