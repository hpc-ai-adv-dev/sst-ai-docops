#!/usr/bin/env bash
# Copyright Hewlett Packard Enterprise Development LP.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHECKSUM_FILE="$ROOT_DIR/models/SHA256SUMS"
EMBEDDINGS_ONLY=false

case "${1:-}" in
    "") ;;
    --embeddings-only) EMBEDDINGS_ONLY=true ;;
    -h|--help)
        echo "Usage: $0 [--embeddings-only]"
        exit 0
        ;;
    *)
        echo "Unknown argument: $1" >&2
        echo "Usage: $0 [--embeddings-only]" >&2
        exit 2
        ;;
esac

if [[ "$EMBEDDINGS_ONLY" == true ]]; then
    models=(nomic-embed-text-v1.5.Q8_0.gguf)
else
    models=(
        Qwen3-14B-Q4_K_M.gguf
        nomic-embed-text-v1.5.Q8_0.gguf
        bge-reranker-v2-m3-Q8_0.gguf
    )
fi

checksum() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

for model in "${models[@]}"; do
    path="$ROOT_DIR/models/$model"
    if [[ ! -s "$path" ]]; then
        echo "ERROR: Required model is missing or empty: models/$model" >&2
        exit 1
    fi
    expected="$(
        awk -v name="models/$model" '$2 == name { print $1 }' "$CHECKSUM_FILE"
    )"
    if [[ -z "$expected" ]]; then
        echo "ERROR: models/$model has no pinned checksum." >&2
        exit 1
    fi
    actual="$(checksum "$path")"
    if [[ "$actual" != "$expected" ]]; then
        echo "ERROR: checksum mismatch for models/$model" >&2
        exit 1
    fi
    echo "models/$model: OK"
done
