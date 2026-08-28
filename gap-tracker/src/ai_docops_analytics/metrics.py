# Copyright Hewlett Packard Enterprise Development LP.
"""Compute Answerer outcomes, documentation gaps, and feedback metrics."""

from __future__ import annotations

import csv
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MetricsSnapshot = dict[str, Any]


def compute_metrics(
    collected: dict,
    cfg: dict,
    *,
    period_start: date | None = None,
) -> MetricsSnapshot:
    """Compute all headline metrics from collected data.

    Args:
        collected: The dict returned by ``collect.run_collect()``.
        cfg: The loaded config.yaml dict (used for metrics_csv path).

    Returns:
        A flat dict of metric name to value.
    """
    m: MetricsSnapshot = {}

    # ── From JSONL QUERY_EVENTs ──────────────────────────────────────
    # Every user query logged by the answer outcome tracker produces a
    # QUERY_EVENT with a tier classification:
    #   - adequate_docs: documentation had relevant chunks (good answer)
    #   - source_only:   only source code matched (documentation gap)
    #   - total_gap:     nothing matched (hard rejection)
    # These tiers are the foundation for Answerer and gap-tracking metrics.
    qe = collected.get("query_events", [])
    m["questions_asked"] = len(qe)
    m["unique_users"] = len({e.get("user_id") for e in qe} - {None, "unknown"})

    tier_counts: dict[str, int] = {}
    for e in qe:
        t = e.get("tier", "unknown")
        tier_counts[t] = tier_counts.get(t, 0) + 1
    m["tier_breakdown"] = tier_counts

    total = len(qe)
    if total:
        # gap_rate includes both source-only answers and hard rejections.
        non_adequate = (
            tier_counts.get("source_only", 0)
            + tier_counts.get("total_gap", 0)
        )
        m["gap_rate"] = round(non_adequate / total, 4)
        # rejection_rate means what it says: requests that were hard-rejected.
        m["rejection_rate"] = round(
            tier_counts.get("total_gap", 0) / total,
            4,
        )
        # code_only_rate is the fraction answered from source code only.
        m["code_only_rate"] = round(
            tier_counts.get("source_only", 0) / total,
            4,
        )
    else:
        m["gap_rate"] = None
        m["rejection_rate"] = None
        m["code_only_rate"] = None

    # ── From JSONL GAP_EVENTs ────────────────────────────────────────
    # Combines "real" GAP_EVENTs (from outlet) with derived gaps (from
    # QUERY_EVENTs where tier != adequate_docs).  The derived gaps ensure
    # we still count gaps even if the filter's outlet never fired.
    ge = collected.get("gap_events", []) + collected.get("derived_gap_events", [])
    gaps_by_type: dict[str, int] = {}
    for e in ge:
        t = e.get("event", "unknown")
        gaps_by_type[t] = gaps_by_type.get(t, 0) + 1
    gap_contributors = {e.get("user_id") for e in ge} - {None, "unknown"}

    # ── From OpenWebUI Feedback API ──────────────────────────────────
    # OpenWebUI stores thumbs up/down ratings with optional text comments.
    # The rating schema varies between OpenWebUI versions:
    #   - Newer: f["data"]["rating"] (nested under a "data" dict)
    #   - Older: f["rating"] (top-level field)
    # We try both to be robust.  Positive ratings (>0) count as thumbs up,
    # negative (<0) as thumbs down.
    fb = collected.get("feedbacks") or []
    source_status = collected.get("source_status") or {}
    feedback_available = (
        source_status.get("feedbacks", {}).get("available", True)
    )
    thumbs_up = 0
    thumbs_down = 0
    feedback_with_comments = 0
    for f in fb:
        rating = _feedback_rating(f)
        if isinstance(rating, (int, float)):
            if rating > 0:
                thumbs_up += 1
            elif rating < 0:
                thumbs_down += 1
        data = f.get("data", {}) if isinstance(f.get("data"), dict) else {}
        comment = data.get("comment") or f.get("comment") or ""
        if comment and comment.strip():
            feedback_with_comments += 1
        if isinstance(rating, (int, float)) and rating < 0:
            user_id = f.get("user_id") or data.get("user_id")
            if user_id not in (None, "unknown"):
                gap_contributors.add(user_id)

    if feedback_available:
        gaps_by_type["negative_feedback"] = thumbs_down
    m["gaps_collected"] = len(ge) + thumbs_down
    m["gaps_by_type"] = {
        key: value for key, value in gaps_by_type.items() if value
    }
    m["unique_gap_contributors"] = len(gap_contributors)

    m["feedback_available"] = feedback_available
    m["thumbs_up"] = thumbs_up if feedback_available else None
    m["thumbs_down"] = thumbs_down if feedback_available else None
    total_rated = thumbs_up + thumbs_down
    m["thumbs_ratio"] = (
        round(thumbs_up / total_rated, 4)
        if feedback_available and total_rated
        else None
    )
    m["feedback_with_comments"] = (
        feedback_with_comments if feedback_available else None
    )

    # ── Weekly deltas ────────────────────────────────────────────────
    # Compare the current run against the last row of metrics-history.csv
    # to compute week-over-week changes.  Deltas are shown in METRICS.md
    # as "+5" or "-3" next to each metric.  If there's no previous row
    # (first run), deltas are None.
    csv_path = cfg["paths"]["metrics_csv"]
    prev = _read_previous_csv_row(csv_path, period_start=period_start)
    if prev:
        m["questions_asked_delta"] = m["questions_asked"] - int(prev.get("questions_asked", 0))
        m["gaps_collected_delta"] = m["gaps_collected"] - int(prev.get("gaps_collected", 0))
        prev_ratio = prev.get("thumbs_ratio")
        if prev_ratio and m["thumbs_ratio"] is not None:
            m["thumbs_ratio_delta"] = round(m["thumbs_ratio"] - float(prev_ratio), 4)
        else:
            m["thumbs_ratio_delta"] = None
    else:
        m["questions_asked_delta"] = None
        m["gaps_collected_delta"] = None
        m["thumbs_ratio_delta"] = None

    return m


def _feedback_rating(feedback: dict) -> int | float | None:
    data = feedback.get("data", {})
    rating = data.get("rating") if isinstance(data, dict) else None
    return feedback.get("rating") if rating is None else rating


def _read_previous_csv_row(
    path: str,
    *,
    period_start: date | None = None,
) -> dict | None:
    """Read the newest metrics row before the current reporting week."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(p) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        current_period = period_start or (
            date.today() - timedelta(days=date.today().weekday())
        )
        previous_rows = [
            row
            for row in rows
            if row.get("date", "") < current_period.isoformat()
        ]
        return (
            max(previous_rows, key=lambda row: row.get("date", ""))
            if previous_rows
            else None
        )
    except Exception:
        logger.warning("Could not read previous metrics CSV: %s", path)
        return None
