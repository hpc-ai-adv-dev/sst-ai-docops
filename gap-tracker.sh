#!/usr/bin/env bash
# Copyright Hewlett Packard Enterprise Development LP.
# Install and run the SST Gap Tracker from this repository.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRACKER_DIR="$ROOT_DIR/gap-tracker"
VENV_DIR="${GAP_TRACKER_VENV:-$TRACKER_DIR/.venv}"
PYTHON="${PYTHON:-python3}"

if [[ ! -x "$VENV_DIR/bin/sst-gap-tracker" ]]; then
    echo "Setting up the SST Gap Tracker in $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install \
        -r "$TRACKER_DIR/requirements.lock"
    "$VENV_DIR/bin/python" -m pip install \
        --no-build-isolation \
        --no-deps \
        -e "$TRACKER_DIR"
fi

exec "$VENV_DIR/bin/sst-gap-tracker" \
    --config "$TRACKER_DIR/config.yaml" \
    "$@"
