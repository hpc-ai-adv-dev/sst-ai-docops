#!/bin/bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Polls the /health endpoint on a list of ports until all respond 200.
# Usage:  bash scripts/wait-for-ports.sh 8000 8001 8002
# ---------------------------------------------------------------------------
set -u

TIMEOUT="${WAIT_TIMEOUT:-120}"

if ! [[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: WAIT_TIMEOUT must be a positive integer." >&2
    exit 2
fi

for port in "$@"; do
    echo -n "  Waiting for port $port..."
    for i in $(seq 1 $TIMEOUT); do
        if curl -sf "http://localhost:$port/health" >/dev/null 2>&1; then
            echo " ready (${i}s)"
            break
        fi
        if [[ $i -eq $TIMEOUT ]]; then
            echo " FAILED (not ready after ${TIMEOUT}s)"
            exit 1
        fi
        sleep 1
    done
done
