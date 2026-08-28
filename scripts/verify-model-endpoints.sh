#!/usr/bin/env bash
# Copyright Hewlett Packard Enterprise Development LP.
set -euo pipefail

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

check_model() {
    local port="$1"
    local expected="$2"
    local response
    if ! response="$(
        curl -fsS --connect-timeout 5 --max-time 30 \
            "http://127.0.0.1:${port}/v1/models"
    )"; then
        echo "ERROR: model endpoint on port ${port} is unavailable." >&2
        return 1
    fi
    python3 -c '
import json
import sys

expected = sys.argv[1]
payload = json.load(sys.stdin)
identifiers = {
    str(item.get("id", ""))
    for item in payload.get("data", [])
    if isinstance(item, dict)
}
if not any(expected in identifier for identifier in identifiers):
    raise SystemExit(
        f"expected model {expected!r}; endpoint reported {sorted(identifiers)!r}"
    )
' "$expected" <<<"$response"
}

check_model 8001 "nomic-embed-text-v1.5.Q8_0.gguf"

if ! embedding_response="$(
    curl -fsS --connect-timeout 5 --max-time 30 \
        "http://127.0.0.1:8001/v1/embeddings" \
        -H "Content-Type: application/json" \
        -d '{"model":"nomic-embed-text-v1.5.Q8_0.gguf","input":["search_query: endpoint check"]}'
)"; then
    echo "ERROR: embedding request on port 8001 failed." >&2
    exit 1
fi
python3 -c '
import json
import sys

payload = json.load(sys.stdin)
embedding = (payload.get("data") or [{}])[0].get("embedding")
if not isinstance(embedding, list) or not embedding:
    raise SystemExit("embedding endpoint returned no vector")
' <<<"$embedding_response"

if [[ "$EMBEDDINGS_ONLY" == true ]]; then
    echo "Embedding endpoint matches the expected nomic model."
    exit 0
fi

check_model 8000 "Qwen3-14B-Q4_K_M.gguf"
check_model 8002 "bge-reranker-v2-m3-Q8_0.gguf"

if ! reranker_response="$(
    curl -fsS --connect-timeout 5 --max-time 30 \
        "http://127.0.0.1:8002/v1/rerank" \
        -H "Content-Type: application/json" \
        -d '{"model":"bge-reranker-v2-m3-Q8_0.gguf","query":"SST","documents":["Structural Simulation Toolkit"]}'
)"; then
    echo "ERROR: reranker request on port 8002 failed." >&2
    exit 1
fi
python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if not isinstance(payload.get("results"), list) or not payload["results"]:
    raise SystemExit("reranker endpoint returned no result")
' <<<"$reranker_response"

echo "Model endpoints match the expected chat, embedding, and reranker stack."
