#!/usr/bin/env bash
# Copyright Hewlett Packard Enterprise Development LP.
# Rebuild the SST knowledge collections in a staging seed and swap only after
# every file is indexed and the staged database passes validation.
set -euo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SEED_DIR/.." && pwd)"
SST_ROOT="${SST_REPOS_ROOT:-$HOME/dev/sstsimulator}"
OPEN_WEBUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui@sha256:b8095f79a6a8ffad8f830bdacc9b5b0aef805689b31bca0b065cc2424d3cfaeb}"
REFRESH_CONTAINER="webui-sst-refresh"
WORK_DIR="$SEED_DIR/.sst-refresh-work"
STAGED_DATA="$WORK_DIR/data"
BACKUP_DIR="$SEED_DIR/.config-seed-backup"
MANIFEST="$WORK_DIR/sst-corpus-manifest.json"
CORPUS_LOCK="$ROOT_DIR/benchmarks/sst-corpus-lock.json"
LOCK_BACKUP="$WORK_DIR/previous-sst-corpus-lock.json"
LOCK_CANDIDATE="$WORK_DIR/sst-corpus-lock.publish"
STATE_FILE="$WORK_DIR/sst-refresh-state.json"
DRY_RUN=false
FETCH=false
START_EMBEDDINGS=true
RESTART=false

usage() {
    cat <<'EOF'
Usage: seed/refresh-sst-corpus.sh [options]

Options:
  --sst-root=PATH       Parent containing sst-docs, sst-core, sst-elements
  --fetch               Fetch origin and require HEAD to equal its upstream
  --dry-run             Validate inputs and discard the temporary upload plan
  --external-embedding  Require an already-running embedding server on :8001
  --image=IMAGE         Open WebUI base image used for staging
  --restart             Discard a saved failed refresh and start again

Environment:
  SST_REFRESH_BATCH     Files per Open WebUI process cycle (default: 50)
EOF
}

for argument in "$@"; do
    case "$argument" in
        --sst-root=*) SST_ROOT="${argument#*=}" ;;
        --fetch) FETCH=true ;;
        --dry-run) DRY_RUN=true ;;
        --external-embedding) START_EMBEDDINGS=false ;;
        --image=*) OPEN_WEBUI_IMAGE="${argument#*=}" ;;
        --restart) RESTART=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

audit_args=(--sst-root "$SST_ROOT")
if [[ "$FETCH" == true ]]; then
    audit_args+=(--fetch)
fi

echo "==> Auditing SST checkouts and question-bank evidence"
python3 "$ROOT_DIR/scripts/audit_question_bank.py" \
    "${audit_args[@]}" \
    --no-lock

if [[ "$DRY_RUN" == true ]]; then
    DRY_RUN_MANIFEST="$(
        mktemp "${TMPDIR:-/tmp}/sst-corpus-manifest.XXXXXX"
    )"
    trap 'rm -f "$DRY_RUN_MANIFEST"' EXIT
    echo "==> Building the temporary upload plan"
    python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" plan \
        --sst-root "$SST_ROOT" \
        --output "$DRY_RUN_MANIFEST"
    echo "Dry run complete; no seed data was changed."
    exit 0
fi

# Check for an active refresh before --restart can remove saved state.
# shellcheck source=scripts/container-runtime.sh
source "$ROOT_DIR/scripts/container-runtime.sh"

if $CONTAINER_RT ps -a --format '{{.Names}}' 2>/dev/null |
    grep -qx "$REFRESH_CONTAINER"; then
    echo "ERROR: container $REFRESH_CONTAINER already exists." >&2
    echo "Inspect/remove it before retrying; the known-good seed is unchanged." >&2
    exit 1
fi
if nc -z 127.0.0.1 3000 >/dev/null 2>&1; then
    echo "ERROR: port 3000 is already in use." >&2
    exit 1
fi

if [[ "$RESTART" == true ]]; then
    rm -rf "$WORK_DIR"
fi

RESUME_REFRESH=false
if [[ -f "$STATE_FILE" && -f "$MANIFEST" && -f "$STAGED_DATA/webui.db" ]]; then
    RESUME_REFRESH=true
    echo "==> Resuming the saved staged refresh in $WORK_DIR"
elif [[ -e "$WORK_DIR" ]]; then
    echo "ERROR: incomplete refresh state exists at $WORK_DIR." >&2
    echo "       Inspect it, then rerun with --restart to discard it." >&2
    exit 1
else
    mkdir -p "$WORK_DIR"
    echo "==> Building the temporary upload plan"
    python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" plan \
        --sst-root "$SST_ROOT" \
        --output "$MANIFEST"
    [[ -f "$SEED_DIR/config-seed/webui.db" ]] || {
        echo "ERROR: $SEED_DIR/config-seed/webui.db is missing." >&2
        exit 1
    }
fi

bash "$ROOT_DIR/scripts/verify-model-files.sh" --embeddings-only

EMBED_LAUNCHER_PID=""
CAPTURE_STAGING=false
SWAP_IN_PROGRESS=false
cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    if $CONTAINER_RT ps --format '{{.Names}}' 2>/dev/null |
        grep -qx "$REFRESH_CONTAINER"; then
        $CONTAINER_RT stop "$REFRESH_CONTAINER" >/dev/null 2>&1 || true
    fi
    if $CONTAINER_RT ps -a --format '{{.Names}}' 2>/dev/null |
        grep -qx "$REFRESH_CONTAINER"; then
        if (( exit_code != 0 )) && [[ "$CAPTURE_STAGING" == true ]]; then
            local recovery_data="$WORK_DIR/data.recovery"
            rm -rf "$recovery_data"
            mkdir -p "$recovery_data"
            if $CONTAINER_RT cp \
                "$REFRESH_CONTAINER:/app/backend/data/." \
                "$recovery_data" >/dev/null 2>&1
            then
                rm -rf "$STAGED_DATA"
                mv "$recovery_data" "$STAGED_DATA"
            else
                rm -rf "$recovery_data"
                echo "WARNING: could not capture current container data;" >&2
                echo "         preserving the previous staged copy." >&2
            fi
        fi
        $CONTAINER_RT rm -v "$REFRESH_CONTAINER" >/dev/null 2>&1 || true
    fi
    if (( exit_code != 0 )) &&
        [[ "$SWAP_IN_PROGRESS" == true && -e "$BACKUP_DIR" ]]
    then
        if [[ -e "$SEED_DIR/config-seed" ]]; then
            rm -rf "$STAGED_DATA"
            mv "$SEED_DIR/config-seed" "$STAGED_DATA" || true
        fi
        if [[ -e "$BACKUP_DIR" ]]; then
            mv "$BACKUP_DIR" "$SEED_DIR/config-seed" || true
        fi
        if [[ -f "$LOCK_BACKUP" ]]; then
            cp "$LOCK_BACKUP" "$CORPUS_LOCK" || true
        else
            rm -f "$CORPUS_LOCK"
        fi
    fi
    if [[ -n "$EMBED_LAUNCHER_PID" ]]; then
        kill "$EMBED_LAUNCHER_PID" >/dev/null 2>&1 || true
        wait "$EMBED_LAUNCHER_PID" >/dev/null 2>&1 || true
    fi
    if (( exit_code != 0 )); then
        echo "Refresh failed; seed/config-seed was not changed." >&2
        echo "Staging data remains at $WORK_DIR for inspection." >&2
    fi
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

if ! nc -z 127.0.0.1 8001 >/dev/null 2>&1; then
    if [[ "$START_EMBEDDINGS" != true ]]; then
        echo "ERROR: embedding endpoint 127.0.0.1:8001 is unavailable." >&2
        exit 1
    fi
    echo "==> Starting the local embedding server"
    LLAMA_CHILD_PID_FILE="$WORK_DIR/embedding.children.pids" \
        bash "$ROOT_DIR/scripts/start-llama-server.sh" --embeddings-only \
        >"$WORK_DIR/embedding.log" 2>&1 &
    EMBED_LAUNCHER_PID=$!
    bash "$ROOT_DIR/scripts/wait-for-ports.sh" 8001
fi
bash "$ROOT_DIR/scripts/verify-model-endpoints.sh" --embeddings-only

echo "==> Starting isolated Open WebUI staging container"
RAG_TEMPLATE="$(cat "$ROOT_DIR/env/rag-template.txt")"
QUERY_GENERATION_PROMPT_TEMPLATE="$(
    cat "$ROOT_DIR/env/query-generation-template.txt"
)"
$CONTAINER_RT create --name "$REFRESH_CONTAINER" \
    -p 127.0.0.1:3000:8080 \
    --env-file "$ROOT_DIR/env/webui-common.env" \
    -e "RAG_TEMPLATE=$RAG_TEMPLATE" \
    -e "QUERY_GENERATION_PROMPT_TEMPLATE=$QUERY_GENERATION_PROMPT_TEMPLATE" \
    "$OPEN_WEBUI_IMAGE" >/dev/null
if [[ "$RESUME_REFRESH" == true ]]; then
    $CONTAINER_RT cp \
        "$STAGED_DATA/." \
        "$REFRESH_CONTAINER:/app/backend/data"
else
    $CONTAINER_RT cp \
        "$SEED_DIR/config-seed/." \
        "$REFRESH_CONTAINER:/app/backend/data"
fi
$CONTAINER_RT start "$REFRESH_CONTAINER" >/dev/null
CAPTURE_STAGING=true
bash "$ROOT_DIR/scripts/wait-for-ports.sh" 3000

echo "==> Uploading and indexing the complete SST corpus"
while true; do
    python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" ingest \
        --sst-root "$SST_ROOT" \
        --manifest "$MANIFEST" \
        --state "$STATE_FILE" \
        --resume \
        --max-files "${SST_REFRESH_BATCH:-50}"
    REFRESH_STATUS="$(
        python3 -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["status"])' \
            "$STATE_FILE"
    )"
    if [[ "$REFRESH_STATUS" == "ready_to_finalize" ||
        "$REFRESH_STATUS" == "finalized" ]]
    then
        break
    fi
    if [[ "$REFRESH_STATUS" != "indexing" ]]; then
        echo "ERROR: unexpected refresh status: $REFRESH_STATUS" >&2
        exit 1
    fi
    echo "==> Recycling Open WebUI before the next ingestion batch"
    $CONTAINER_RT stop --time 30 "$REFRESH_CONTAINER" >/dev/null
    $CONTAINER_RT start "$REFRESH_CONTAINER" >/dev/null
    bash "$ROOT_DIR/scripts/wait-for-ports.sh" 3000
done

echo "==> Stopping staging container and flushing its database"
$CONTAINER_RT stop --time 30 "$REFRESH_CONTAINER" >/dev/null
rm -rf "$STAGED_DATA"
mkdir -p "$STAGED_DATA"
$CONTAINER_RT cp \
    "$REFRESH_CONTAINER:/app/backend/data/." \
    "$STAGED_DATA"
$CONTAINER_RT rm -v "$REFRESH_CONTAINER" >/dev/null

echo "==> Validating and finalizing staged collection links"
python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" finalize \
    --database "$STAGED_DATA/webui.db" \
    --state "$STATE_FILE" \
    --manifest "$MANIFEST" \
    --lock-destination "$STAGED_DATA/sst-corpus-lock.json"
python3 "$SEED_DIR/sync-seed-config.py" \
    "$STAGED_DATA/webui.db" \
    "$SEED_DIR/filters/confidence-gate.py"
python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" compact \
    --database "$STAGED_DATA/webui.db" \
    --vector-database "$STAGED_DATA/vector_db/chroma.sqlite3"
python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" validate \
    --database "$STAGED_DATA/webui.db" \
    --vector-database "$STAGED_DATA/vector_db/chroma.sqlite3" \
    --uploads "$STAGED_DATA/uploads" \
    --lock "$STAGED_DATA/sst-corpus-lock.json"

echo "==> Atomically replacing the seed (backup retained)"
if [[ -f "$CORPUS_LOCK" ]]; then
    cp "$CORPUS_LOCK" "$LOCK_BACKUP"
fi
SWAP_IN_PROGRESS=true
rm -rf "$BACKUP_DIR"
mv "$SEED_DIR/config-seed" "$BACKUP_DIR"
if ! mv "$STAGED_DATA" "$SEED_DIR/config-seed"; then
    echo "ERROR: swap failed; the previous seed was restored." >&2
    exit 1
fi

# Replace the tracked compact lock only after the seed swap succeeds. If the
# lock cannot be installed, put both the seed and lock back as they were.
if ! cp "$SEED_DIR/config-seed/sst-corpus-lock.json" "$LOCK_CANDIDATE" ||
    ! mv "$LOCK_CANDIDATE" "$CORPUS_LOCK"
then
    echo "ERROR: corpus lock update failed; previous seed restored." >&2
    exit 1
fi
SWAP_IN_PROGRESS=false
rm -rf "$WORK_DIR"

trap - EXIT INT TERM
if [[ -n "$EMBED_LAUNCHER_PID" ]]; then
    kill "$EMBED_LAUNCHER_PID" >/dev/null 2>&1 || true
    wait "$EMBED_LAUNCHER_PID" >/dev/null 2>&1 || true
fi

echo "Refresh complete."
echo "  New seed: $SEED_DIR/config-seed"
echo "  Previous seed: $BACKUP_DIR"
echo "  Corpus lock: $CORPUS_LOCK"
echo "Run seed/build.sh and the full verification suite before removing the backup."
