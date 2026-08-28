#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Clones and builds llama.cpp locally into ./llama.cpp/
#
# Usage:
#   bash install-llama.sh           # skip if already built
#   bash install-llama.sh --force   # clean and rebuild
#
# To use a custom llama-server binary instead (skip this script entirely),
# set the env var before running start.sh:
#   LLAMA_SERVER=/path/to/llama-server ./start.sh
# ---------------------------------------------------------------------------
set -e

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$WORK_DIR/llama.cpp"
BINARY="$INSTALL_DIR/build/bin/llama-server"
REPO="https://github.com/ggml-org/llama.cpp.git"
LLAMA_CPP_COMMIT="${LLAMA_CPP_COMMIT:-6a257d44633d4a752183ed778b88d2924d0a6b9d}"

if [[ "$1" == "--force" ]]; then
    echo "Force rebuild requested - removing existing build..."
    rm -rf "$INSTALL_DIR/build"
fi

if [[ -x "$BINARY" ]]; then
    echo "llama-server already built at $BINARY"
    echo "Run with --force to rebuild."
    exit 0
fi

# ── Clone ──────────────────────────────────────────────────────────────────
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    echo "Cloning llama.cpp into $INSTALL_DIR ..."
    git clone --filter=blob:none --no-checkout "$REPO" "$INSTALL_DIR"
else
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]]; then
        echo "ERROR: tracked llama.cpp files are modified; refusing to overwrite them." >&2
        exit 1
    fi
fi
echo "Checking out pinned llama.cpp commit $LLAMA_CPP_COMMIT ..."
git -C "$INSTALL_DIR" fetch --depth 1 origin "$LLAMA_CPP_COMMIT"
git -C "$INSTALL_DIR" checkout --detach "$LLAMA_CPP_COMMIT"

# ── Detect platform and set cmake flags ────────────────────────────────────
CMAKE_FLAGS=""
OS="$(uname -s)"
ARCH="$(uname -m)"

if [[ "$OS" == "Darwin" ]]; then
    # Metal is the default on macOS/Apple Silicon; be explicit anyway
    CMAKE_FLAGS="-DGGML_METAL=ON"
    PARALLEL="$(sysctl -n hw.logicalcpu)"
    echo "Platform: macOS ($ARCH) - building with Metal"
elif [[ "$OS" == "Linux" ]]; then
    PARALLEL="$(nproc)"
    if command -v nvidia-smi >/dev/null 2>&1; then
        CMAKE_FLAGS="-DGGML_CUDA=ON"
        echo "Platform: Linux + NVIDIA GPU - building with CUDA"
    else
        echo "Platform: Linux CPU-only"
    fi
else
    PARALLEL=4
    echo "Platform: unknown ($OS) - building without GPU flags"
fi

# ── Build ──────────────────────────────────────────────────────────────────
echo "Building llama.cpp (this takes a few minutes)..."
cmake -S "$INSTALL_DIR" -B "$INSTALL_DIR/build" $CMAKE_FLAGS
cmake --build "$INSTALL_DIR/build" --config Release -j "$PARALLEL" --target llama-server

echo ""
echo "llama-server built at $BINARY"
