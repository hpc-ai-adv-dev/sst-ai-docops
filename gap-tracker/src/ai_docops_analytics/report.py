# Copyright Hewlett Packard Enterprise Development LP.
"""Weekly gap report generation.

Generates a GitHub Issue-ready markdown document from clustered gap data.
The report contains:

- A triage checklist (``- [ ]`` items) for each gap topic, ordered by
  query frequency (most-asked gaps first).
- Per-gap: a representative question, user count, gap type, and sample
  questions.
- Triage instructions telling the reviewer to mark each gap as:
  ✅ Valid gap (needs documentation)
  ❌ Already documented (search/RAG issue)
  ⏭️ Out of scope

The report uses the representative question selected during clustering as
the checklist label. No additional model call is needed.

Entry point: ``generate_report(clusters, period_start=...) -> str``
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from ai_docops_analytics.labels import GAP_TYPE_LABELS


def generate_report(
    clusters: list[dict],
    *,
    period_start: date | None = None,
) -> str:
    """Generate markdown gap report from cluster data."""
    today = period_start or date.today()
    week_start = today - timedelta(days=today.weekday())

    if not clusters:
        return (
            f"## Weekly Doc Gap Report — Week of {week_start.strftime('%B %d, %Y')}\n\n"
            "No documentation-gap signals were collected in this window. "
            "Check the question count before interpreting this as complete coverage.\n"
        )

    # Build the markdown report.
    lines: list[str] = []
    lines.append(f"## Weekly Doc Gap Report — Week of {week_start.strftime('%B %d, %Y')}")
    lines.append("")
    lines.append("### Top Gaps (by question frequency)")
    lines.append("")
    if any(
        cluster.get("clustering_method") == "exact_text_fallback"
        for cluster in clusters
    ):
        lines.append(
            "> **Degraded grouping:** the embedding service was unavailable, "
            "so queries were grouped only when their normalized text matched "
            "exactly. Every signal remained available to grouping; the "
            "configured report limit still applies."
        )
        lines.append("")

    for cluster in clusters:
        representative = cluster["representative"]
        title = _format_question(representative)
        gap_type_str = ", ".join(
            GAP_TYPE_LABELS.get(gap_type, gap_type)
            for gap_type in cluster.get("gap_types", [])
        ) or "Unknown"
        users = cluster.get("users", [])
        question_count = cluster["size"]
        question_summary = (
            f"{question_count} "
            f"{'question' if question_count == 1 else 'questions'}"
        )
        user_summary = (
            f"{len(users)} {'user' if len(users) == 1 else 'users'}"
            if users
            else "user count unavailable"
        )

        lines.append(f"- [ ] {title} — {question_summary}, {user_summary}")
        lines.append(f"  - Gap type: {gap_type_str}")

        # Keep frequency in cluster["size"], but do not repeat identical
        # questions or the representative question in the examples.
        samples = [
            question
            for question in dict.fromkeys(cluster.get("queries", []))
            if question != representative
        ][:3]
        if samples:
            formatted = ", ".join(_format_question(q) for q in samples)
            lines.append(f"  - Sample questions: {formatted}")
        lines.append("")

    lines.append("### Triage Instructions")
    lines.append("Mark each item as:")
    lines.append("- ✅ **Valid gap** — needs documentation")
    lines.append("- ❌ **Already documented** — search/RAG issue, not a content issue")
    lines.append("- ⏭️ **Out of scope** — not something we need to document")

    return "\n".join(lines)


def _format_question(question: str) -> str:
    """Render a user question without allowing it to alter the checklist."""
    text = " ".join(str(question).split())
    return f"<code>{html.escape(text)}</code>"
