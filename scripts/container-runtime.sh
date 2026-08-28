# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Source this file (do NOT execute it) to set container runtime variables.
#
# After sourcing, the following are exported:
#   CONTAINER_RT   – "podman" or "docker"
#   COMPOSE_CMD    – "podman compose", "docker compose", or "docker-compose"
#
# Override by exporting CONTAINER_RT before sourcing:
#   export CONTAINER_RT=docker
#   source scripts/container-runtime.sh
#
# Also provides a helper function:
#   container_image_exists <image>  – returns 0 if the image is local
# ---------------------------------------------------------------------------

if [[ -z "${CONTAINER_RT:-}" ]]; then
    if command -v podman &>/dev/null; then
        CONTAINER_RT=podman
    elif command -v docker &>/dev/null; then
        CONTAINER_RT=docker
    else
        echo "ERROR: Neither podman nor docker found in PATH."
        echo "       Install one of them and try again."
        return 1 2>/dev/null || exit 1
    fi
fi

# ── Compose command ──────────────────────────────────────────────────────────
if [[ "$CONTAINER_RT" == "podman" ]]; then
    COMPOSE_CMD="podman compose"
elif docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "ERROR: No compose command found."
    echo "       Install 'docker compose' (plugin) or 'docker-compose' (standalone)."
    return 1 2>/dev/null || exit 1
fi

export CONTAINER_RT COMPOSE_CMD

# ── Helper: check if an image exists locally ─────────────────────────────────
container_image_exists() {
    if [[ "$CONTAINER_RT" == "podman" ]]; then
        podman image exists "$1" 2>/dev/null
    else
        docker image inspect "$1" >/dev/null 2>&1
    fi
}
