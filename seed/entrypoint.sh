#!/usr/bin/env bash
# Copyright Hewlett Packard Enterprise Development LP.
# ---------------------------------------------------------------------------
# Demo entrypoint – seeds /app/backend/data from the baked-in snapshot on
# first run, then execs the original open-webui startup command.
#
# This is necessary because a runtime volume mounted at /app/backend/data
# would otherwise shadow the pre-configured database / vector store.
# ---------------------------------------------------------------------------
set -euo pipefail

DATA_DIR="${DATA_DIR:-/app/backend/data}"
APP_SEED_DIR="${APP_SEED_DIR:-/app/backend/data-seed}"
SYNC_SCRIPT="${SYNC_SCRIPT:-/app/sync-seed-config.py}"
FILTER_SOURCE="${FILTER_SOURCE:-/app/confidence-gate.py}"

if [ ! -f "${DATA_DIR}/webui.db" ]; then
    echo "[demo] First run detected - seeding pre-configured data..."
    mkdir -p "${DATA_DIR}"

    # Copy the database last. Its presence is the first-run completion marker,
    # so an interrupted multi-gigabyte copy is retried on the next launch.
    rm -f "${DATA_DIR}/webui.db.seed-tmp"
    find "${APP_SEED_DIR}" -mindepth 1 -maxdepth 1 \
        ! -name webui.db \
        -exec cp -a "{}" "${DATA_DIR}/" \;
    cp -a "${APP_SEED_DIR}/webui.db" "${DATA_DIR}/webui.db.seed-tmp"
    mv "${DATA_DIR}/webui.db.seed-tmp" "${DATA_DIR}/webui.db"
    echo "[demo] Seed complete."
else
    echo "[demo] Existing data found - skipping seed."
    if [ -f "${APP_SEED_DIR}/sst-corpus-lock.json" ] &&
        ! cmp -s \
            "${APP_SEED_DIR}/sst-corpus-lock.json" \
            "${DATA_DIR}/sst-corpus-lock.json"
    then
        echo "[demo] WARNING: runtime data uses a different SST corpus." >&2
        echo "[demo] Start with an empty RUNTIME_DATA_DIR to use this image's corpus." >&2
    fi
fi

# Keep executable filter code and focused model capabilities aligned with
# reviewed, version-controlled source even when runtime-data already exists.
python3 "$SYNC_SCRIPT" \
    "${DATA_DIR}/webui.db" \
    "$FILTER_SOURCE"

# Hand off to the original CMD (bash start.sh) or whatever was passed in
exec "$@"
