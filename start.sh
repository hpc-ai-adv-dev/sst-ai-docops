#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Main entry point. Just run:  ./start.sh
#
# Platform is controlled by the PLATFORM variable in .env:
#   mac    – llama-server on the host with Metal (default)
#   nvidia – llama-server in containers with NVIDIA CUDA
#   amd    – llama-server in containers with AMD ROCm
#
# Container runtime (podman or docker) is auto-detected. Override with:
#   export CONTAINER_RT=docker
#
# Image source: downloadable releases set DEMO_IMAGE to a versioned seeded
# registry image. Maintainers can leave it unset to use/build the local seed.
#   DEMO_IMAGE=REGISTRY/OWNER/sst-answerer:v2
#
# Copy .env.example to .env and set PLATFORM to change.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$ROOT_DIR/runtime-data/.pids"
LLAMA_PARENT_PID_FILE="$PID_DIR/llama-server.parent.pid"
export LLAMA_CHILD_PID_FILE="$PID_DIR/llama-server.children.pids"
LLAMA_LOG_FILE="$ROOT_DIR/runtime-data/llama-server.log"
LLAMA_STARTED=false

cleanup_failed_start() {
    if [[ "$LLAMA_STARTED" == true && -f "$LLAMA_PARENT_PID_FILE" ]]; then
        local pid
        pid="$(cat "$LLAMA_PARENT_PID_FILE" 2>/dev/null || true)"
        if [[ "$pid" =~ ^[0-9]+$ ]]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
        rm -f "$LLAMA_PARENT_PID_FILE" "$LLAMA_CHILD_PID_FILE"
    fi
}
trap cleanup_failed_start ERR INT TERM

# ── Detect container runtime (podman / docker) ───────────────────────────
# shellcheck source=scripts/container-runtime.sh
source "$ROOT_DIR/scripts/container-runtime.sh"
echo "Container runtime: $CONTAINER_RT"

# ── Read platform from .env ───────────────────────────────────────────────
if [[ ! -f "$ROOT_DIR/.env" ]]; then
    echo "No .env found – defaulting to PLATFORM=mac"
    echo "Copy .env.example to .env and set PLATFORM=mac|nvidia|amd to change."
    PLATFORM="mac"
else
    PLATFORM="$(
        grep -E '^PLATFORM=' "$ROOT_DIR/.env" |
            cut -d= -f2 |
            tr -d '[:space:]' ||
            true
    )"
    PLATFORM="${PLATFORM:-mac}"
fi
echo "Platform: $PLATFORM"

# ── Read DEMO_IMAGE from .env (default: local build tag) ─────────────────
DEMO_IMAGE="${DEMO_IMAGE:-}"
if [[ -f "$ROOT_DIR/.env" ]]; then
    _env_image="$(
        grep -E '^DEMO_IMAGE=' "$ROOT_DIR/.env" |
            cut -d= -f2 |
            tr -d '[:space:]' ||
            true
    )"
    DEMO_IMAGE="${DEMO_IMAGE:-$_env_image}"
fi
DEMO_IMAGE="${DEMO_IMAGE:-open-webui-rag-demo:v2}"
export DEMO_IMAGE

# ── Determine compose files (and validate PLATFORM) ─────────────────────
if [[ "$PLATFORM" == "mac" ]]; then
    COMPOSE_FILES="-f $ROOT_DIR/compose.yaml"
elif [[ "$PLATFORM" == "nvidia" ]]; then
    COMPOSE_FILES="-f $ROOT_DIR/compose.yaml -f $ROOT_DIR/compose.nvidia.yaml"
elif [[ "$PLATFORM" == "amd" ]]; then
    COMPOSE_FILES="-f $ROOT_DIR/compose.yaml -f $ROOT_DIR/compose.amd.yaml"
else
    echo "ERROR: Unknown PLATFORM '$PLATFORM' in .env"
    echo "       Valid values: mac | nvidia | amd"
    exit 1
fi

# ── Load multiline prompt templates from files ────────────────────────────
# compose.yaml passes these exported values through to Open WebUI.
export RAG_TEMPLATE QUERY_GENERATION_PROMPT_TEMPLATE
RAG_TEMPLATE="$(cat "$ROOT_DIR/env/rag-template.txt")"
QUERY_GENERATION_PROMPT_TEMPLATE="$(
    cat "$ROOT_DIR/env/query-generation-template.txt"
)"

# ── Ensure the Open WebUI image is available ─────────────────────────────
# (Done BEFORE starting llama-server so an abort here leaves nothing running)
if ! container_image_exists "$DEMO_IMAGE"; then
    echo ""
    echo "Image $DEMO_IMAGE is not available locally."
    if [[ "$DEMO_IMAGE" != "open-webui-rag-demo:v2" ]]; then
        # DEMO_IMAGE points to a registry – pull it
        echo "Pulling $DEMO_IMAGE ..."
        $CONTAINER_RT pull "$DEMO_IMAGE"
    else
        # Default local image – build from seed/
        if [[ ! -f "$ROOT_DIR/seed/config-seed/webui.db" ]]; then
            echo "ERROR: No prebuilt DEMO_IMAGE was configured and the local" >&2
            echo "       generated seed is unavailable." >&2
            echo "       Set DEMO_IMAGE in .env to the release image, or build" >&2
            echo "       a maintainer seed before running start.sh." >&2
            exit 1
        fi
        read -r -p "Build it now from seed/? This will take a minute or two. [Y/n] " yn
        case "$yn" in
            [Nn]) exit 1 ;;
        esac
        bash "$ROOT_DIR/seed/build.sh"
    fi
fi

# ── Validate model assets before starting anything ──────────────────────
echo "Verifying model checksums..."
bash "$ROOT_DIR/scripts/verify-model-files.sh"

# ── Mac: llama-server must run on the host (Metal, no container possible) ─
if [[ "$PLATFORM" == "mac" ]]; then
    ready_model_ports=0
    for port in 8000 8001 8002; do
        if nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
            ready_model_ports=$((ready_model_ports + 1))
        fi
    done
    if (( ready_model_ports == 3 )); then
        echo "Reusing llama-server instances already listening on ports 8000-8002."
    elif (( ready_model_ports != 0 )); then
        echo "ERROR: Only $ready_model_ports of the three model ports are available." >&2
        echo "       Stop the partial model stack before retrying." >&2
        exit 1
    else
        # Ensure a binary is available
        source "$ROOT_DIR/scripts/find-llama-server.sh" 2>/dev/null || {
            read -r -p "llama-server not found. Build it now? (runs install-llama.sh) [Y/n] " yn
            case "$yn" in
                [Nn]) exit 1 ;;
            esac
            bash "$ROOT_DIR/install-llama.sh"
        }
        echo "Starting llama-server instances on host..."
        mkdir -p "$PID_DIR"
        nohup bash "$ROOT_DIR/scripts/start-llama-server.sh" \
            >"$LLAMA_LOG_FILE" 2>&1 &
        echo $! > "$LLAMA_PARENT_PID_FILE"
        LLAMA_STARTED=true
        bash "$ROOT_DIR/scripts/wait-for-ports.sh" 8000 8001 8002
    fi
    bash "$ROOT_DIR/scripts/verify-model-endpoints.sh"
fi

# ── Start Open WebUI (and llama-server containers if nvidia/amd) ──────────
echo "Starting containers..."
# shellcheck disable=SC2086
$COMPOSE_CMD $COMPOSE_FILES up -d
bash "$ROOT_DIR/scripts/wait-for-ports.sh" 3000

echo ""
echo "Demo running at http://localhost:3000"
echo "Stop with: ./stop.sh"
trap - ERR INT TERM
