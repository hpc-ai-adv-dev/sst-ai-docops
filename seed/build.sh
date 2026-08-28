#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
set -euo pipefail
# ---------------------------------------------------------------------------
# Build the pre-seeded Open WebUI image locally.
#
# Usage:
#   ./build.sh
#   ./build.sh --yes   # non-interactive rebuild for release automation
#   ./build.sh --check # validate generated image inputs without building
#
# Builds open-webui-rag-demo:v2 from seed/Containerfile. Called automatically
# by start.sh when the image is not present locally.
#
# Works with both podman and docker (auto-detected).
# ---------------------------------------------------------------------------
SEED_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SEED_DIR/.." && pwd)"
IMAGE_TAG="${DEMO_IMAGE:-open-webui-rag-demo:v2}"
ASSUME_YES=false
CHECK_ONLY=false
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=true ;;
        --check) CHECK_ONLY=true ;;
        -h|--help)
            echo "Usage: $0 [--yes] [--check]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--yes] [--check]" >&2
            exit 1
            ;;
    esac
done

# ── Guard: refuse to package an incomplete or unreviewed seed ─────────────
required_seed_files=(
    "$SEED_DIR/config-seed/webui.db"
    "$SEED_DIR/config-seed/vector_db/chroma.sqlite3"
    "$SEED_DIR/config-seed/sst-corpus-lock.json"
)
for required_file in "${required_seed_files[@]}"; do
    if [[ ! -s "$required_file" ]]; then
        echo "ERROR: required seed file is missing or empty: $required_file" >&2
        echo "       Run seed/refresh-sst-corpus.sh before building." >&2
        exit 1
    fi
done
if [[ ! -d "$SEED_DIR/config-seed/uploads" ]]; then
    echo "ERROR: seed/config-seed/uploads is missing." >&2
    exit 1
fi
if ! cmp -s \
    "$SEED_DIR/config-seed/sst-corpus-lock.json" \
    "$ROOT_DIR/benchmarks/sst-corpus-lock.json"
then
    echo "ERROR: generated seed and tracked corpus locks differ." >&2
    echo "       Finish the corpus refresh before building." >&2
    exit 1
fi
python3 "$ROOT_DIR/scripts/refresh_sst_corpus.py" validate \
    --database "$SEED_DIR/config-seed/webui.db" \
    --vector-database "$SEED_DIR/config-seed/vector_db/chroma.sqlite3" \
    --uploads "$SEED_DIR/config-seed/uploads" \
    --lock "$SEED_DIR/config-seed/sst-corpus-lock.json"
if [[ "$CHECK_ONLY" == true ]]; then
    echo "Seed image inputs are complete, internally consistent, and use the reviewed corpus lock."
    exit 0
fi

# ── Detect container runtime (podman / docker) ───────────────────────────
# shellcheck source=scripts/container-runtime.sh
source "$ROOT_DIR/scripts/container-runtime.sh"

# ── Guard: ask before overwriting an existing image ──────────────────────
if container_image_exists "$IMAGE_TAG"; then
    if [[ "$ASSUME_YES" != true ]]; then
        read -r -p "Image $IMAGE_TAG already exists. Rebuild it? [y/N] " yn
        case "$yn" in
            [Yy]) ;;
            *) echo "Skipping build."; exit 0 ;;
        esac
    fi
fi

echo "Building $IMAGE_TAG ..."
$CONTAINER_RT build -f "$SEED_DIR/Containerfile" -t "$IMAGE_TAG" "$SEED_DIR"

echo "Done."
