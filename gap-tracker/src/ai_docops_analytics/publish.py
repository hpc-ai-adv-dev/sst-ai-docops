# Copyright Hewlett Packard Enterprise Development LP.
"""Publishing — GitHub Issue, METRICS.md, metrics-history.csv.

This module produces the Gap Tracker's three persistent outputs.
It is invoked by the ``publish`` CLI command, which reads a saved
``report.json`` bundle (containing ``report_md``, ``metrics``, and
``clusters``) so you always publish exactly what you reviewed.

1. **GitHub Issue**: A weekly gap report posted to a configured repo
   using PyGithub.  The issue title includes the current date, and
   configured labels (default: ``doc-gap``, ``sst``) are applied.
   When ``--dry-run`` is set (or GitHub isn't configured), the issue
   content is printed to stdout instead.

2. **METRICS.md**: A human-readable snapshot of headline metrics with
   week-over-week deltas. Written to the configured output directory.

3. **metrics-history.csv**: A CSV in the output directory with one row per
   reporting week. Rerunning publication for the same week replaces that row.
   Column order is fixed by ``_CSV_COLUMNS``.

Entry point: ``publish(report_md, metrics, cfg, dry_run=False)``
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ai_docops_analytics.labels import GAP_TYPE_LABELS, TIER_LABELS

logger = logging.getLogger(__name__)

# CSV columns in a stable order for the time-series file.
# New columns should be appended at the end to maintain backward
# compatibility with existing rows (older rows will have empty values
# for new columns when read by csv.DictReader).
_CSV_COLUMNS = [
    "date",
    "questions_asked",
    "unique_users",
    "tier_adequate_docs",
    "tier_source_only",
    "tier_total_gap",
    "rejection_rate",
    "gaps_collected",
    "thumbs_up",
    "thumbs_down",
    "thumbs_ratio",
    "gap_rate",
    "negative_feedbacks",
]


def publish(
    report_md: str,
    metrics: dict,
    cfg: dict,
    *,
    dry_run: bool = False,
    period_start: date | str | None = None,
) -> None:
    """Post gap report + update metrics files."""
    period = _reporting_week(period_start)
    _update_metrics_md(metrics, cfg, dry_run=dry_run, period_start=period)
    _upsert_metrics_csv(metrics, cfg, dry_run=dry_run, period_start=period)
    _publish_github_issue(
        report_md,
        cfg,
        dry_run=dry_run,
        period_start=period,
    )


# ── GitHub Issue ─────────────────────────────────────────────────────────────

def _publish_github_issue(
    report_md: str,
    cfg: dict,
    *,
    dry_run: bool,
    period_start: date | str | None = None,
) -> None:
    """Create a GitHub Issue with the gap report, or print it in dry-run mode.

    The GitHub token is read from config.yaml or the GITHUB_TOKEN env var.
    If neither is set, the issue is printed to stdout with a warning.
    """
    gh_cfg = cfg.get("github", {})
    token = gh_cfg.get("token") or os.environ.get("GITHUB_TOKEN", "")
    repo_name = gh_cfg.get("repo", "")
    labels = gh_cfg.get("labels", ["doc-gap", "sst"])

    period = _reporting_week(period_start)
    title = (
        "Weekly Doc Gap Report — Week of "
        f"{period.strftime('%B %d, %Y')}"
    )

    if dry_run or not token or not repo_name:
        if not dry_run and (not token or not repo_name):
            logger.warning("GitHub token/repo not configured — skipping issue creation")
        print(f"\n{'='*60}")
        preview_state = "dry-run" if dry_run else "not published"
        print(f"GITHUB ISSUE ({preview_state})")
        print(f"{'='*60}")
        print(f"Title: {title}")
        print(f"Labels: {labels}")
        print(f"Repo: {repo_name or '(not configured)'}")
        print(f"{'='*60}")
        print(report_md)
        print(f"{'='*60}\n")
        return

    from github import Auth, Github

    base_url = gh_cfg.get("base_url", "https://api.github.com")
    g = Github(auth=Auth.Token(token), base_url=base_url)
    repo = g.get_repo(repo_name)
    existing = next(
        (issue for issue in repo.get_issues(state="all") if issue.title == title),
        None,
    )
    if existing is not None:
        existing.edit(body=report_md, labels=labels)
        logger.info(
            "Updated GitHub Issue #%d: %s",
            existing.number,
            existing.html_url,
        )
        return

    issue = repo.create_issue(title=title, body=report_md, labels=labels)
    logger.info("Created GitHub Issue #%d: %s", issue.number, issue.html_url)


# ── METRICS.md ───────────────────────────────────────────────────────────────

def _update_metrics_md(
    metrics: dict,
    cfg: dict,
    *,
    dry_run: bool,
    period_start: date | str | None = None,
) -> None:
    """Write (overwrite) the METRICS.md snapshot file.

    The markdown includes:
    - Headline metrics table with deltas (e.g., "10 (+3)")
    - Tier breakdown table
    - Gap type distribution table
    """
    md_path = Path(cfg["paths"]["metrics_md"])
    period = _reporting_week(period_start)

    def _delta_str(val: Any) -> str:
        if val is None:
            return ""
        prefix = "+" if val > 0 else ""
        return f" ({prefix}{val})"

    tb = metrics.get("tier_breakdown", {})

    lines = [
        f"# SST Gap Tracker Metrics — Week of {period.isoformat()}",
        "",
        "## Headline Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Questions asked | {metrics.get('questions_asked', 0)}{_delta_str(metrics.get('questions_asked_delta'))} |",
        f"| Unique users | {metrics.get('unique_users', 0)} |",
        f"| Documentation-gap rate | {_fmt_percent(metrics.get('gap_rate'))} |",
        f"| Rejection rate | {_fmt_percent(metrics.get('rejection_rate'))} |",
        f"| Code-only rate | {_fmt_percent(metrics.get('code_only_rate'))} |",
        f"| Gap signals collected | {metrics.get('gaps_collected', 0)}{_delta_str(metrics.get('gaps_collected_delta'))} |",
        f"| 👍/👎 ratio | {_fmt_ratio(metrics.get('thumbs_ratio'), available=metrics.get('feedback_available', True))}{_delta_str(metrics.get('thumbs_ratio_delta'))} |",
        "",
        "## Tier Breakdown",
        "",
        "| Tier | Count |",
        "|------|-------|",
    ]
    for tier in _ordered_labels(tb, TIER_LABELS):
        lines.append(f"| {TIER_LABELS.get(tier, tier)} | {tb[tier]} |")
    lines.extend(
        [
            "",
            "## Gap Types",
            "",
            "| Type | Count |",
            "|------|-------|",
        ]
    )
    gap_types = metrics.get("gaps_by_type", {})
    for gap_type in _ordered_labels(gap_types, GAP_TYPE_LABELS):
        lines.append(
            f"| {GAP_TYPE_LABELS.get(gap_type, gap_type)} "
            f"| {gap_types[gap_type]} |"
        )

    content = "\n".join(lines) + "\n"

    if dry_run:
        print(f"\n{'='*60}")
        print("METRICS.MD (dry-run)")
        print(f"{'='*60}")
        print(content)
        print(f"{'='*60}\n")
        return

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content)
    logger.info("Updated %s", md_path)


def _fmt_ratio(val: float | None, *, available: bool = True) -> str:
    if not available:
        return "N/A (feedback unavailable)"
    if val is None:
        return "N/A (no ratings)"
    return f"{val:.1%}"


def _fmt_percent(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1%}"


def _ordered_labels(values: dict, labels: dict) -> list[str]:
    known = [key for key in labels if key in values]
    unknown = sorted(key for key in values if key not in labels)
    return known + unknown


# ── metrics-history.csv ──────────────────────────────────────────────────────

def _upsert_metrics_csv(
    metrics: dict,
    cfg: dict,
    *,
    dry_run: bool,
    period_start: date | str | None = None,
) -> None:
    """Write one row per date to the metrics history.

    Publishing can be retried after a partial failure. If the reporting
    week's row already exists, replace it instead of creating a duplicate.
    """
    csv_path = Path(cfg["paths"]["metrics_csv"])
    tb = metrics.get("tier_breakdown", {})

    row = {
        "date": _reporting_week(period_start).isoformat(),
        "questions_asked": metrics.get("questions_asked", 0),
        "unique_users": metrics.get("unique_users", 0),
        "tier_adequate_docs": tb.get("adequate_docs", 0),
        "tier_source_only": tb.get("source_only", 0),
        "tier_total_gap": tb.get("total_gap", 0),
        "rejection_rate": _csv_value(metrics.get("rejection_rate")),
        "gaps_collected": metrics.get("gaps_collected", 0),
        "thumbs_up": _csv_value(metrics.get("thumbs_up")),
        "thumbs_down": _csv_value(metrics.get("thumbs_down")),
        "thumbs_ratio": metrics.get("thumbs_ratio") if metrics.get("thumbs_ratio") is not None else "",
        "gap_rate": _csv_value(metrics.get("gap_rate")),
        "negative_feedbacks": _csv_value(metrics.get("thumbs_down")),
    }

    if dry_run:
        print(f"\n{'='*60}")
        print("METRICS CSV ROW (dry-run)")
        print(f"{'='*60}")
        print(",".join(_CSV_COLUMNS))
        print(",".join(str(row.get(c, "")) for c in _CSV_COLUMNS))
        print(f"{'='*60}\n")
        return

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    existing_rows: list[dict] = []
    if csv_path.is_file():
        with open(csv_path, newline="") as fh:
            existing_rows = list(csv.DictReader(fh))
    rows = [item for item in existing_rows if item.get("date") != row["date"]]
    rows.append(row)
    rows.sort(key=lambda item: item.get("date", ""))

    temporary_path = csv_path.with_name(f".{csv_path.name}.tmp")
    with open(temporary_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(
            {
                column: "" if item.get(column) is None else item.get(column, "")
                for column in _CSV_COLUMNS
            }
            for item in rows
        )
    temporary_path.replace(csv_path)

    logger.info("Updated %s", csv_path)


def _csv_value(value: Any) -> Any:
    return "" if value is None else value


def _reporting_week(value: date | str | None) -> date:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    report_date = value or date.today()
    return report_date - timedelta(days=report_date.weekday())
