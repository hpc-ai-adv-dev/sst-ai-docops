# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Source this file (do NOT execute it) to resolve LLAMA_BIN.
#
# Caller must export ROOT_DIR pointing to the project root before sourcing.
#
# Resolution order:
#   1. $LLAMA_SERVER env var        – set this to use any custom install
#   2. $ROOT_DIR/llama.cpp/build/bin/llama-server  – local build in project dir
#   3. llama-server in PATH         – system-wide install
#
# After sourcing, LLAMA_BIN is set and exported, or the script returns 1.
# ---------------------------------------------------------------------------

_LOCAL_BUILD="${ROOT_DIR}/llama.cpp/build/bin/llama-server"
_PATH_BIN="$(command -v llama-server 2>/dev/null || true)"

if [[ -n "${LLAMA_SERVER:-}" && -x "$LLAMA_SERVER" ]]; then
    LLAMA_BIN="$LLAMA_SERVER"
    echo "[find-llama-server] Using custom install: $LLAMA_BIN"
elif [[ -x "$_LOCAL_BUILD" ]]; then
    LLAMA_BIN="$_LOCAL_BUILD"
    echo "[find-llama-server] Using local build: $LLAMA_BIN"
elif [[ -n "$_PATH_BIN" ]]; then
    LLAMA_BIN="$_PATH_BIN"
    echo "[find-llama-server] Using PATH install: $LLAMA_BIN"
else
    echo ""
    echo "ERROR: llama-server not found. Options:"
    echo "  1. Run bash install-llama.sh   (builds locally in ./llama.cpp/)"
    echo "  2. Set LLAMA_SERVER=/path/to/llama-server to use an existing install"
    echo "  3. Add llama-server to your PATH"
    return 1 2>/dev/null || exit 1
fi

export LLAMA_BIN
