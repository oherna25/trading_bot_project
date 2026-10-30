#!/usr/bin/env bash
# Push the trade-reason model to Baseten.
#
# Usage, from the repo root:
#   BASETEN_API_KEY=... deploy/baseten.sh
#
# Reads BASETEN_API_KEY from the environment only. The app calls the
# deployed predict endpoint for each trade card reason.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="$ROOT/deploy/reason-model"

if [ ! -d "$MODEL_DIR" ]; then
  echo "error: $MODEL_DIR not found" >&2
  exit 1
fi
if [ -z "${BASETEN_API_KEY:-}" ]; then
  echo "error: BASETEN_API_KEY is not set" >&2
  exit 1
fi
command -v baseten >/dev/null || { echo "error: baseten CLI not found" >&2; exit 1; }

baseten model push --dir "$MODEL_DIR"

cat <<EOF

predict URL pattern:
  https://model-<MODEL_ID>.api.baseten.co/environments/production/predict
(take <MODEL_ID> from the push output or baseten model list)

operator vars (shell or .env, never committed):
  BASETEN_API_KEY=<key>
  BASETEN_REASON_MODEL_ID=<MODEL_ID or full predict URL>

checks:
  curl -X POST "https://model-<MODEL_ID>.api.baseten.co/environments/production/predict" \\
    -H "Authorization: Api-Key \$BASETEN_API_KEY" \\
    -H "Content-Type: application/json" \\
    -d '{"stats": {"symbol": "SPY", "direction": "long", "spread": "770/780 call vertical"}}'
EOF
