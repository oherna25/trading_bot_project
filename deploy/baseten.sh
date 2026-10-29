#!/usr/bin/env bash
# Stage a clean Truss dir and push it to Baseten.
#
# Usage, from the repo root:
#   BASETEN_API_KEY=... deploy/baseten.sh
#   BASETEN_API_KEY=... DASHBOARD_BASE_URL=https://model-<ID>.api.baseten.co/environments/production/sync deploy/baseten.sh
#
# Reads BASETEN_API_KEY from the environment only. Secret values come from
# the environment first, then the local .env. TRUSS_DIR overrides the stage dir.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="${TRUSS_DIR:-$ROOT/deploy/.truss-stage}"

SECRETS="TRADIER_ACCESS_TOKEN TRADIER_ACCOUNT_ID APCA_API_KEY_ID APCA_API_SECRET_KEY FINNHUB_API_KEY MARKETDATA_API_KEY META_MODEL_API_KEY"

if [ -z "${BASETEN_API_KEY:-}" ]; then
  echo "error: BASETEN_API_KEY is not set" >&2
  exit 1
fi
command -v baseten >/dev/null || { echo "error: baseten CLI not found" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$STAGE/data/trading-bot"
cp "$ROOT/run_app.py" "$STAGE/run_app.py"
cp -R "$ROOT/bot" "$STAGE/bot"
find "$STAGE/bot" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE" -name .DS_Store -delete
for bundle in snapshots bars journal profile; do
  if [ -f "$ROOT/data/trading-bot/$bundle.json" ]; then
    cp "$ROOT/data/trading-bot/$bundle.json" "$STAGE/data/trading-bot/"
  fi
done
python3 - "$ROOT/deploy/baseten.config.yaml" "$STAGE/config.yaml" <<'EOF'
import sys
template, rendered = sys.argv[1], sys.argv[2]
import os
text = open(template).read()
base_url = os.environ.get("DASHBOARD_BASE_URL", "")
if base_url:
    lines = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("DASHBOARD_BASE_URL:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f"{indent}DASHBOARD_BASE_URL: {base_url}\n")
        else:
            lines.append(line)
    text = "".join(lines)
open(rendered, "w").write(text)
EOF

echo "staged:"
find "$STAGE" -type f | sort

dotenv_value() {
  [ -f "$ROOT/.env" ] || return 1
  grep -E "^$1=" "$ROOT/.env" | tail -1 | cut -d= -f2- | tr -d '\r'
}

secret_exists() {
  baseten org secret list --output json 2>/dev/null | grep -q "\"$1\""
}

MISSING=0
for name in $SECRETS; do
  if secret_exists "$name"; then
    echo "secret $name exists"
    continue
  fi
  value="${!name:-}"
  [ -n "$value" ] || value="$(dotenv_value "$name" || true)"
  if [ -z "${value:-}" ]; then
    echo "secret $name has no value in env or .env; create it or this push serves without it"
    MISSING=1
    continue
  fi
  if printf '%s' "$value" | baseten org secret set --name "$name" >/dev/null 2>&1; then
    echo "secret $name created"
  else
    echo "secret $name could not be created (key may lack secret perms); continuing without it"
    MISSING=1
  fi
done
if [ "$MISSING" -ne 0 ]; then
  echo "warning: continuing without some secrets; boot serves the baked bundle until secrets exist" >&2
fi

baseten model push --dir "$STAGE" --tail --wait

SYNC_ROOT="${SYNC_ROOT:-https://model-<MODEL_ID>.api.baseten.co/environments/production/sync}"
cat <<EOF

sync root: $SYNC_ROOT
(if <MODEL_ID> is still a placeholder, take it from the push output or baseten model list,
set DASHBOARD_BASE_URL to the sync root, and push again)

checks:
  curl "$SYNC_ROOT/healthz"
  curl "$SYNC_ROOT/dashboard/<UNDERLYING>:<ISO_TIMESTAMP>"
  curl -X POST "${SYNC_ROOT%/sync}predict" \\
    -H "Authorization: Api-Key \$BASETEN_API_KEY" \\
    -H "Content-Type: application/json" \\
    -d '{"text": "gex brief", "underlying": "SPY"}'
EOF
