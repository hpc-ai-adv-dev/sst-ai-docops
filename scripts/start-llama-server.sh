#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Starts three llama-server instances (LLM, embeddings, reranker).
# Can be run directly or called from start.sh.
#
# Optional modes:
#   ./scripts/start-llama-server.sh                   # default: all servers
#   ./scripts/start-llama-server.sh --embeddings-only # only port 8001
#
# Download models if missing:
#   curl -L "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf" -o ./models/nomic-embed-text-v1.5.Q8_0.gguf
#   curl -L "https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf" -o ./models/bge-reranker-v2-m3-Q8_0.gguf
#   curl -L "https://huggingface.co/MaziyarPanahi/Qwen3-14B-GGUF/resolve/main/Qwen3-14B.Q4_K_M.gguf" -o ./models/Qwen3-14B-Q4_K_M.gguf
# ---------------------------------------------------------------------------
set -euo pipefail

MODE="all"
for arg in "$@"; do
  case "$arg" in
    --embeddings-only)
      MODE="embeddings-only"
      ;;
    -h|--help)
      echo "Usage: $0 [--embeddings-only]"
      echo "  --embeddings-only  Start only the embeddings endpoint on port 8001"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--embeddings-only]" >&2
      exit 1
      ;;
  esac
done

# This script lives in scripts/ -- root is one level up
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/find-llama-server.sh
source "$ROOT_DIR/scripts/find-llama-server.sh"

MODELS="$ROOT_DIR/models"
PID_FILE="${LLAMA_CHILD_PID_FILE:-$ROOT_DIR/runtime-data/llama-server.children.pids}"
PIDS=()
LLAMA_LOG_ARGS=(--log-disable)
if [[ "${LLAMA_SERVER_LOGGING:-0}" == "1" ]]; then
  LLAMA_LOG_ARGS=()
fi

mkdir -p "$(dirname "$PID_FILE")"
: > "$PID_FILE"

cleanup() {
  local pid
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM

start_server() {
  "$@" &
  local pid=$!
  PIDS+=("$pid")
  printf '%s\n' "$pid" >> "$PID_FILE"
}

if [[ "$MODE" == "all" ]]; then
# ── LLM (chat / completions) ─────────────────── port 8000 ──
echo "Starting LLM on port 8000..."
start_server "$LLAMA_BIN" \
  --model "$MODELS/Qwen3-14B-Q4_K_M.gguf" \
  --alias Qwen3-14B-Q4_K_M.gguf \
  --host "${LLAMA_HOST:-127.0.0.1}" \
  --port 8000 \
  --n-gpu-layers -1 \
  --ctx-size 8192 \
  "${LLAMA_LOG_ARGS[@]}"
fi

# ── Embeddings ───────────────────────────────── port 8001 ──
# nomic-embed-text-v1.5: 8192-token context, far better than MiniLM's 256
echo "Starting embeddings on port 8001..."
start_server "$LLAMA_BIN" \
  --model "$MODELS/nomic-embed-text-v1.5.Q8_0.gguf" \
  --alias nomic-embed-text-v1.5.Q8_0.gguf \
  --host "${LLAMA_HOST:-127.0.0.1}" \
  --port 8001 \
  --n-gpu-layers -1 \
  --ctx-size 8192 \
  --embedding \
  --cache-ram 0 \
  --no-cache-prompt \
  --pooling mean \
  "${LLAMA_LOG_ARGS[@]}"

if [[ "$MODE" == "all" ]]; then
# ── Reranker ─────────────────────────────────── port 8002 ──
# bge-reranker-v2-m3 cross-encoder paired with the Nomic embeddings above.
echo "Starting reranker on port 8002..."
start_server "$LLAMA_BIN" \
  --model "$MODELS/bge-reranker-v2-m3-Q8_0.gguf" \
  --alias bge-reranker-v2-m3-Q8_0.gguf \
  --host "${LLAMA_HOST:-127.0.0.1}" \
  --port 8002 \
  --n-gpu-layers -1 \
  --ctx-size 8192 \
  --reranking \
  "${LLAMA_LOG_ARGS[@]}"
fi

wait "${PIDS[@]}"
