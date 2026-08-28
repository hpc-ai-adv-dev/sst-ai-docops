#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$ROOT_DIR/runtime-data/.pids"
LLAMA_PARENT_PID_FILE="$PID_DIR/llama-server.parent.pid"
LLAMA_CHILD_PID_FILE="$PID_DIR/llama-server.children.pids"

# ── Detect container runtime (podman / docker) ───────────────────────────
# shellcheck source=scripts/container-runtime.sh
source "$ROOT_DIR/scripts/container-runtime.sh"

# Read platform from .env (default mac)
if [[ -f "$ROOT_DIR/.env" ]]; then
    PLATFORM="$(
        grep -E '^PLATFORM=' "$ROOT_DIR/.env" |
            cut -d= -f2 |
            tr -d '[:space:]' ||
            true
    )"
fi
PLATFORM="${PLATFORM:-mac}"

# Read DEMO_IMAGE so compose can interpolate ${DEMO_IMAGE:-...}
if [[ -f "$ROOT_DIR/.env" ]]; then
    _env_image="$(
        grep -E '^DEMO_IMAGE=' "$ROOT_DIR/.env" |
            cut -d= -f2 |
            tr -d '[:space:]' ||
            true
    )"
    DEMO_IMAGE="${DEMO_IMAGE:-$_env_image}"
fi
export DEMO_IMAGE="${DEMO_IMAGE:-open-webui-rag-demo:v2}"

case "$PLATFORM" in
    nvidia)
        COMPOSE_FILES="-f $ROOT_DIR/compose.yaml -f $ROOT_DIR/compose.nvidia.yaml" ;;
    amd)
        COMPOSE_FILES="-f $ROOT_DIR/compose.yaml -f $ROOT_DIR/compose.amd.yaml" ;;
    mac)
        COMPOSE_FILES="-f $ROOT_DIR/compose.yaml" ;;
    *)
        echo "ERROR: Unknown PLATFORM '$PLATFORM' in .env" >&2
        exit 1 ;;
esac

CLEAN=false
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

echo "Stopping containers (platform: $PLATFORM)..."
# shellcheck disable=SC2086
compose_status=0
$COMPOSE_CMD $COMPOSE_FILES down || compose_status=$?

if [[ "$PLATFORM" == "mac" ]]; then
    echo "Stopping host llama-server instances..."
    stopped=false
    if [[ -f "$LLAMA_CHILD_PID_FILE" ]]; then
        while IFS= read -r pid; do
            if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
                command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
                if [[ "$command_line" == *llama-server* ]]; then
                    kill "$pid" 2>/dev/null || true
                    stopped=true
                fi
            fi
        done < "$LLAMA_CHILD_PID_FILE"
    fi
    if [[ -f "$LLAMA_PARENT_PID_FILE" ]]; then
        parent_pid="$(cat "$LLAMA_PARENT_PID_FILE" 2>/dev/null || true)"
        if [[ "$parent_pid" =~ ^[0-9]+$ ]] && kill -0 "$parent_pid" 2>/dev/null; then
            kill "$parent_pid" 2>/dev/null || true
            stopped=true
        fi
    fi
    rm -f "$LLAMA_PARENT_PID_FILE" "$LLAMA_CHILD_PID_FILE"
    if [[ "$stopped" == true ]]; then
        echo "  stopped demo-managed servers"
    else
        echo "  none managed by this demo"
    fi
fi

if [[ "$CLEAN" == true ]]; then
    echo "Wiping runtime-data/ so the next start reseeds from the baked image..."
    rm -rf "$ROOT_DIR/runtime-data"
    echo "  done."
fi

echo "Done."
exit "$compose_status"
