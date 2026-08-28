# Copyright Hewlett Packard Enterprise Development LP.
"""Collect Answerer events and OpenWebUI feedback into a local snapshot."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_docops_analytics.timestamps import parse_timestamp

logger = logging.getLogger(__name__)

CollectedData = dict[str, Any]

_GAP_EVENT_TYPES = (
    "doc_gap_source_only",
    "doc_gap_no_answer",
)
_GAP_EVENT_BY_TIER = {
    "source_only": "doc_gap_source_only",
    "total_gap": "doc_gap_no_answer",
}


def run_collect(cfg: dict, since: str | None = None) -> tuple[CollectedData, Path]:
    """Return collected data and the directory holding this run's snapshot."""
    since_dt = parse_timestamp(since) if since else None
    if since and since_dt is None:
        raise ValueError(f"Invalid --since timestamp: {since!r}")

    events_dir = Path(cfg["paths"]["events_dir"])
    events_dir.mkdir(parents=True, exist_ok=True)

    demo_log = cfg.get("paths", {}).get("demo_event_log")
    if demo_log:
        src = Path(demo_log)
        if src.is_file():
            dest = events_dir / src.name
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
                logger.info("Copied demo event log %s → %s", src, dest)
        else:
            logger.warning("demo_event_log not found: %s", src)

    query_events: list[dict] = []
    gap_events: list[dict] = []
    seen_events: set[str] = set()

    event_logs = sorted(events_dir.glob("*.jsonl"))
    for jsonl_path in event_logs:
        logger.info("Reading %s", jsonl_path)
        with open(jsonl_path) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("%s:%d — invalid JSON, skipping", jsonl_path, lineno)
                    continue
                if not isinstance(event, dict):
                    logger.warning(
                        "%s:%d — JSON event is not an object, skipping",
                        jsonl_path,
                        lineno,
                    )
                    continue

                if since_dt:
                    ts = parse_timestamp(event.get("timestamp"))
                    if ts is None or ts < since_dt:
                        continue

                fingerprint = json.dumps(
                    event, sort_keys=True, separators=(",", ":"), default=str
                )
                if fingerprint in seen_events:
                    continue
                seen_events.add(fingerprint)

                etype = event.get("event", "")
                if etype == "query":
                    query_events.append(event)
                elif etype in _GAP_EVENT_TYPES:
                    gap_events.append(event)

    logger.info(
        "JSONL: %d query events, %d gap events", len(query_events), len(gap_events)
    )

    derived_gap_events = _derive_missing_gap_events(query_events, gap_events)

    if derived_gap_events:
        logger.info(
            "Derived %d additional gap signals from QUERY_EVENTs", len(derived_gap_events)
        )

    feedbacks: list[dict] = []
    source_status = {
        "event_log": (
            {"available": True}
            if event_logs
            else {
                "available": False,
                "reason": "no JSONL event logs found",
            }
        ),
        "feedbacks": {
            "available": False,
            "reason": "OpenWebUI is not configured",
        },
    }

    owui_cfg = cfg.get("openwebui", {})
    if owui_cfg.get("url"):
        try:
            from ai_docops_analytics.client import OpenWebUIClient

            client = OpenWebUIClient(
                owui_cfg["url"],
                os.environ.get(
                    "SST_GAP_TRACKER_OPENWEBUI_EMAIL",
                    owui_cfg["email"],
                ),
                os.environ.get(
                    "SST_GAP_TRACKER_OPENWEBUI_PASSWORD",
                    owui_cfg["password"],
                ),
                timeout=float(owui_cfg.get("timeout", 30)),
            )
        except Exception as exc:
            source_status["authentication"] = {
                "available": False,
                "reason": str(exc),
            }
            source_status["feedbacks"] = {
                "available": False,
                "reason": "OpenWebUI authentication failed",
            }
            logger.warning(
                "Could not authenticate to OpenWebUI; continuing with "
                "JSONL data only: %s",
                exc,
            )
            logger.debug("OpenWebUI authentication failure details", exc_info=True)
        else:
            source_status["authentication"] = {"available": True}
            try:
                feedbacks = client.get_feedbacks_export()
            except Exception as exc:
                source_status["feedbacks"] = {
                    "available": False,
                    "reason": str(exc),
                }
                logger.warning("Could not collect OpenWebUI feedback: %s", exc)
                logger.debug("OpenWebUI feedback failure details", exc_info=True)
            else:
                source_status["feedbacks"] = {"available": True}
        if since_dt:
            feedbacks = [
                item
                for item in feedbacks
                if (parse_timestamp(item.get("created_at")) or datetime.min.replace(
                    tzinfo=timezone.utc
                ))
                >= since_dt
            ]

    collected_at = datetime.now(timezone.utc)
    run_id = collected_at.strftime("%Y-%m-%dT%H%M%S.%fZ")
    snap_dir = Path(cfg["paths"]["snapshots_dir"]) / run_id
    snap_dir.mkdir(parents=True)

    (snap_dir / "feedbacks.json").write_text(
        json.dumps(feedbacks, indent=2, default=str)
    )
    logger.info("Snapshots saved to %s", snap_dir)

    return {
        "query_events": query_events,
        "gap_events": gap_events,
        "derived_gap_events": derived_gap_events,
        "feedbacks": feedbacks,
        "source_status": source_status,
        "collection_window": {
            "since": since_dt.isoformat() if since_dt else None,
            "collected_at": collected_at.isoformat(),
        },
    }, snap_dir


def _derive_missing_gap_events(
    query_events: list[dict],
    gap_events: list[dict],
) -> list[dict]:
    """Derive a gap when a query has no paired event for its interaction."""
    explicit_pairs = {
        (str(event["interaction_id"]), event.get("event", ""))
        for event in gap_events
        if event.get("interaction_id")
    }
    derived: list[dict] = []

    for query_event in query_events:
        event_type = _GAP_EVENT_BY_TIER.get(query_event.get("tier", ""))
        if event_type is None:
            continue
        interaction_id = query_event.get("interaction_id")
        if interaction_id and (str(interaction_id), event_type) in explicit_pairs:
            continue
        derived.append({
            "event": event_type,
            "interaction_id": interaction_id,
            "timestamp": query_event.get("timestamp"),
            "query": query_event.get("query"),
            "user_id": query_event.get("user_id"),
            "chat_id": query_event.get("chat_id"),
            "derived_from": "query_event",
            "docs_best": query_event.get("docs_best"),
            "code_best": query_event.get("code_best"),
        })
    return derived
