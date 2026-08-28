#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Seed script -- starts an Open WebUI container for manual configuration.
# When you are happy, press Enter and the script captures the result into
# ./config-seed, which the Containerfile then bakes in.
#
# USAGE  (run from the seed/ directory)
#   ./start-seed.sh           # Fast mode (default)
#   ./start-seed.sh --fresh   # Start from blank (re-upload everything)
#   ./start-seed.sh --fresh --embedding-batch-size=32
#
# FAST MODE (default)
#   If config-seed/ already has an embedded knowledge base, the container
#   starts with it pre-loaded.  Only settings, filters, and users need to
#   be changed.  Knowledge bases are preserved automatically -- no re-upload.
#   Use this for iterating on filters, RAG settings, or any non-KB change.
#
# FRESH MODE (--fresh)
#   Starts from a completely blank Open WebUI instance.  You will need to
#   re-upload and re-embed all knowledge base documents (~1 hr for SST corpus).
#   Use this when you need to re-ingest the source content from scratch.
#
# STEP-BY-STEP
#  1. Start llama-server on the host so you can test connections:
#       bash ../scripts/start-llama-server.sh --embeddings-only
#  2. Run this script (optionally with --fresh)
#  3. Open http://localhost:3000
#  4. Make your changes:
#       Fast:  tweak settings / upload filter / adjust valves -- KB already there
#       Fresh: connect endpoints, upload KB docs, configure everything
#  5. Run  ./import-filter.sh  (if changing the confidence gate)
#  6. Press Enter here when done
#  7. ./build.sh
# ---------------------------------------------------------------------------
set -euo pipefail

SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SEED_DIR/.." && pwd)"
SEED_CONTAINER="webui-seed"
BACKUP_DIR="$SEED_DIR/.config-seed-backup"
OPEN_WEBUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui@sha256:b8095f79a6a8ffad8f830bdacc9b5b0aef805689b31bca0b065cc2424d3cfaeb}"
FRESH=false
EMBED_BATCH_SIZE=""
SWAP_IN_PROGRESS=false

usage() {
    cat <<'EOF'
Usage: seed/start-seed.sh [--fresh] [--embedding-batch-size=N]

Starts a local Open WebUI maintenance session and captures it only after the
staged database synchronizes successfully.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=true ;;
        --embedding-batch-size=*)
            EMBED_BATCH_SIZE="${arg#*=}"
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -n "$EMBED_BATCH_SIZE" ]]; then
    if ! [[ "$EMBED_BATCH_SIZE" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --embedding-batch-size must be a positive integer." >&2
        exit 1
    fi
    if (( EMBED_BATCH_SIZE < 1 )); then
        echo "ERROR: --embedding-batch-size must be >= 1." >&2
        exit 1
    fi
fi

# ── Detect container runtime (podman / docker) ────────────────────────────
# shellcheck source=scripts/container-runtime.sh
source "$ROOT_DIR/scripts/container-runtime.sh"

# ── Guard: abort if the container name or port is already in use ──────────
if $CONTAINER_RT ps -a --format '{{.Names}}' 2>/dev/null |
    grep -qx "$SEED_CONTAINER"; then
    echo "ERROR: Seed container '${SEED_CONTAINER}' already exists." >&2
    echo "       Inspect and remove it before retrying." >&2
    exit 1
fi
if nc -z 127.0.0.1 3000 >/dev/null 2>&1; then
    echo "ERROR: port 3000 is already in use." >&2
    exit 1
fi

# ── Decide mode ───────────────────────────────────────────────────────────
WORK_DIR="${SEED_DIR}/.seed-work"

if [[ "$FRESH" == true ]]; then
    echo "==> FRESH mode: starting from blank Open WebUI instance."
    echo "    You will need to re-upload all knowledge base documents."
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
elif [[ -f "${SEED_DIR}/config-seed/webui.db" ]]; then
    echo "==> FAST mode: pre-loading existing knowledge base from config-seed/."
    echo "    Knowledge bases are already embedded -- only change settings/filters."
    echo "    Run with --fresh to start from blank and re-upload everything."
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
    cp -r "${SEED_DIR}/config-seed/." "$WORK_DIR/"
else
    echo "==> FAST mode: no existing config-seed found, starting blank (same as --fresh)."
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
fi

echo ""

if [[ -n "$EMBED_BATCH_SIZE" ]]; then
    echo "==> EMBEDDING BATCH override: RAG_EMBEDDING_BATCH_SIZE=${EMBED_BATCH_SIZE}"
    echo ""
fi

# ── Start container with .seed-work bind-mounted as the data directory ────
# The bind mount means changes made inside Open WebUI are immediately
# written to .seed-work/ on the host -- no docker cp needed on capture.
echo "Starting seed container..."
_RAG_TEMPLATE="$(cat "${ROOT_DIR}/env/rag-template.txt")"
_QUERY_GENERATION_PROMPT_TEMPLATE="$(
    cat "${ROOT_DIR}/env/query-generation-template.txt"
)"
run_seed_container() {
    $CONTAINER_RT run -d --name "${SEED_CONTAINER}" \
        -p 127.0.0.1:3000:8080 \
        --env-file "${ROOT_DIR}/env/webui-common.env" \
        -e "RAG_TEMPLATE=${_RAG_TEMPLATE}" \
        -e "QUERY_GENERATION_PROMPT_TEMPLATE=${_QUERY_GENERATION_PROMPT_TEMPLATE}" \
        "$@" \
        -v "${WORK_DIR}:/app/backend/data" \
        "$OPEN_WEBUI_IMAGE"
}

if [[ -n "$EMBED_BATCH_SIZE" ]]; then
    run_seed_container -e "RAG_EMBEDDING_BATCH_SIZE=${EMBED_BATCH_SIZE}"
else
    run_seed_container
fi

# Remove the named container on interrupts and ordinary failures. Keep the
# staged data so a failed capture can be inspected instead of silently lost.
cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    if $CONTAINER_RT ps --format '{{.Names}}' 2>/dev/null |
        grep -qx "$SEED_CONTAINER"; then
        $CONTAINER_RT stop --time 30 "$SEED_CONTAINER" >/dev/null 2>&1 || true
    fi
    if $CONTAINER_RT ps -a --format '{{.Names}}' 2>/dev/null |
        grep -qx "$SEED_CONTAINER"; then
        $CONTAINER_RT rm -v "$SEED_CONTAINER" >/dev/null 2>&1 || true
    fi
    if (( exit_code != 0 )) &&
        [[ "$SWAP_IN_PROGRESS" == true && -d "$BACKUP_DIR" ]]
    then
        if [[ -d "$SEED_DIR/config-seed" ]]; then
            rm -rf "$WORK_DIR"
            mv "$SEED_DIR/config-seed" "$WORK_DIR" || true
        fi
        mv "$BACKUP_DIR" "$SEED_DIR/config-seed" || true
    fi
    if (( exit_code != 0 )); then
        echo "Seed capture failed; existing config-seed was not changed." >&2
        echo "Staged data remains at $WORK_DIR." >&2
    fi
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

echo "Waiting for Open WebUI to be ready..."
bash "${ROOT_DIR}/scripts/wait-for-ports.sh" 3000

echo ""
echo ">>> Open http://localhost:3000"
if [[ "$FRESH" == true ]]; then
    echo ">>> FRESH: configure connections, upload KB docs, add functions/users."
else
    echo ">>> FAST: KB is already loaded. Change settings, filters, or users as needed."
    echo ">>> Run  bash seed/import-filter.sh  in another terminal to upload the filter."
fi
echo ""
read -r -p "Press Enter when you are done configuring..."

echo ""
echo "Capturing configuration..."

# Stop Open WebUI cleanly so SQLite and Chroma writes are flushed.
$CONTAINER_RT stop --time 30 "$SEED_CONTAINER" >/dev/null
$CONTAINER_RT rm -v "$SEED_CONTAINER" >/dev/null

[[ -f "$WORK_DIR/webui.db" ]] || {
    echo "ERROR: staged webui.db is missing." >&2
    exit 1
}

echo "Synchronizing reviewed filter and model settings..."
python3 "$SEED_DIR/sync-seed-config.py" \
    "$WORK_DIR/webui.db" \
    "$SEED_DIR/filters/confidence-gate.py"
python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" compact \
    --database "$WORK_DIR/webui.db" \
    --vector-database "$WORK_DIR/vector_db/chroma.sqlite3"

# ── Guard: warn before overwriting an existing seed ───────────────────────
if [[ -f "${SEED_DIR}/config-seed/webui.db" ]]; then
    echo ""
    echo "WARNING: seed/config-seed/ already contains a previous capture."
    read -r -p "Overwrite it? [y/N] " yn
    case "$yn" in
        [Yy]) ;;
        *)
            echo "Aborted -- existing config-seed preserved."
            echo "Staged changes remain at $WORK_DIR."
            exit 1
            ;;
    esac
fi

# ── Swap the staged directory only after it is complete ───────────────────
rm -rf "$BACKUP_DIR"
SWAP_IN_PROGRESS=true
if [[ -d "$SEED_DIR/config-seed" ]]; then
    mv "$SEED_DIR/config-seed" "$BACKUP_DIR"
fi
if ! mv "$WORK_DIR" "$SEED_DIR/config-seed"; then
    echo "ERROR: seed swap failed; the previous seed was restored." >&2
    exit 1
fi
SWAP_IN_PROGRESS=false

trap - EXIT INT TERM
echo ""
echo "Config saved to seed/config-seed/."
if [[ -d "$BACKUP_DIR" ]]; then
    echo "Previous seed retained at seed/.config-seed-backup/."
fi
echo "Now run: seed/build.sh"
