#!/usr/bin/env bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# import-filter.sh — Upload the SST answer filter and attach it only to the
# SST Answerer model in the running seed container.
#
# Run after ./start-seed.sh, before pressing Enter.
# Credentials come from WEBUI_ADMIN_EMAIL / WEBUI_ADMIN_PASSWORD (env/webui-common.env).
# ---------------------------------------------------------------------------
set -euo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_URL="${1:-http://localhost:3000}"
FILTER_FILE="$SEED_DIR/filters/confidence-gate.py"
ENV_FILE="$SEED_DIR/../env/webui-common.env"

# Read admin credentials from the shared env file (avoids sourcing the whole file,
# which fails on multi-line values like RAG_TEMPLATE)
_env_val() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2-; }
ADMIN_EMAIL="${WEBUI_ADMIN_EMAIL:-$(_env_val WEBUI_ADMIN_EMAIL)}"
ADMIN_PASSWORD="${WEBUI_ADMIN_PASSWORD:-$(_env_val WEBUI_ADMIN_PASSWORD)}"

[[ -n "$ADMIN_EMAIL" ]]    || { echo "ERROR: WEBUI_ADMIN_EMAIL not set in env/webui-common.env" >&2; exit 1; }
[[ -n "$ADMIN_PASSWORD" ]] || { echo "ERROR: WEBUI_ADMIN_PASSWORD not set in env/webui-common.env" >&2; exit 1; }

[[ -f "$FILTER_FILE" ]] || { echo "ERROR: $FILTER_FILE not found." >&2; exit 1; }

# ── Authenticate ───────────────────────────────────────────────────────────
echo "Signing in as $ADMIN_EMAIL ..."
SIGNIN=$(curl -sS -X POST "$BASE_URL/api/v1/auths/signin" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}")
TOKEN=$(echo "$SIGNIN" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)

if [[ -z "$TOKEN" ]]; then
    echo "No account yet — creating admin..."
    SIGNUP=$(curl -sS -X POST "$BASE_URL/api/v1/auths/signup" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"Admin\",\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\",\"profile_image_url\":\"\"}")
    TOKEN=$(echo "$SIGNUP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
fi

[[ -n "$TOKEN" ]] || { echo "ERROR: Authentication failed. Check WEBUI_ADMIN_EMAIL / WEBUI_ADMIN_PASSWORD." >&2; exit 1; }

# ── Upload filter ──────────────────────────────────────────────────────────
echo "Uploading filter..."
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'id':      'confidence_gate',
    'name':    'SST Answer Outcome Tracker',
    'type':    'filter',
    'content': sys.stdin.read(),
    'meta': {
        'description': (
            'Verifies grounded SST answers, labels source-only documentation '
            'gaps, and records outcomes for the Gap Tracker.'
        ),
        'author': 'open-webui-demo',
    }
}))
" < "$FILTER_FILE")

# Try update first (filter already exists); fall back to create
RESPONSE=$(curl -sS -X POST "$BASE_URL/api/v1/functions/id/confidence_gate/update" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "$PAYLOAD")

if ! echo "$RESPONSE" | python3 -c "import sys,json; exit(0 if json.load(sys.stdin).get('id') else 1)" 2>/dev/null; then
    RESPONSE=$(curl -sS -X POST "$BASE_URL/api/v1/functions/create" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $TOKEN" \
        -d "$PAYLOAD")
    echo "$RESPONSE" | python3 -c "
import sys, json; r = json.load(sys.stdin)
if not r.get('id'):
    print('ERROR: ' + r.get('detail', r.get('error', 'unknown')), file=sys.stderr); sys.exit(1)
" || exit 1
fi

# ── Enable the filter, but do not make it global ───────────────────────────
# Fetch current state and only toggle if needed — toggle is not idempotent.
FUNC=$(curl -sS "$BASE_URL/api/v1/functions/id/confidence_gate" \
    -H "Authorization: Bearer $TOKEN")
IS_ACTIVE=$(echo "$FUNC" | python3 -c "import sys,json; print(json.load(sys.stdin).get('is_active', False))" 2>/dev/null || echo "False")
IS_GLOBAL=$(echo "$FUNC" | python3 -c "import sys,json; print(json.load(sys.stdin).get('is_global', False))" 2>/dev/null || echo "False")

[[ "$IS_ACTIVE" == "True" ]] || curl -sS -X POST "$BASE_URL/api/v1/functions/id/confidence_gate/toggle" \
    -H "Authorization: Bearer $TOKEN" > /dev/null
[[ "$IS_GLOBAL" != "True" ]] || curl -sS -X POST "$BASE_URL/api/v1/functions/id/confidence_gate/toggle/global" \
    -H "Authorization: Bearer $TOKEN" > /dev/null

# ── Attach the filter to SST Answerer ──────────────────────────────────────
MODEL=$(curl -sS "$BASE_URL/api/v1/models/model?id=sst-answerer" \
    -H "Authorization: Bearer $TOKEN")
MODEL_PAYLOAD=$(echo "$MODEL" | python3 -c "
import json, sys

model = json.load(sys.stdin)
if model.get('id') != 'sst-answerer':
    raise SystemExit('ERROR: SST Answerer model was not found.')

meta = model.get('meta') or {}
filter_ids = meta.get('filterIds')
if not isinstance(filter_ids, list):
    filter_ids = []
if 'confidence_gate' not in filter_ids:
    filter_ids.append('confidence_gate')
meta['filterIds'] = filter_ids

print(json.dumps({
    key: value
    for key, value in {
        'id': model.get('id'),
        'base_model_id': model.get('base_model_id'),
        'name': model.get('name'),
        'meta': meta,
        'params': model.get('params') or {},
        'access_grants': model.get('access_grants') or [],
        'is_active': model.get('is_active', True),
    }.items()
}))
")

MODEL_RESPONSE=$(curl -sS -X POST "$BASE_URL/api/v1/models/model/update" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN" \
    -d "$MODEL_PAYLOAD")
echo "$MODEL_RESPONSE" | python3 -c "
import json, sys

model = json.load(sys.stdin)
filters = (model.get('meta') or {}).get('filterIds') or []
if model.get('id') != 'sst-answerer' or 'confidence_gate' not in filters:
    raise SystemExit('ERROR: Failed to attach the filter to SST Answerer.')
"

echo "Done. SST answer filter active and attached only to SST Answerer."
