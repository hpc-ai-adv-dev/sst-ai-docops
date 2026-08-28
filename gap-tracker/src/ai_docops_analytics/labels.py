# Copyright Hewlett Packard Enterprise Development LP.
"""Human-readable labels for Answerer outcomes and gap signals."""

TIER_LABELS = {
    "adequate_docs": "Documentation-backed",
    "source_only": "Source-code-only",
    "total_gap": "Not found",
}

GAP_TYPE_LABELS = {
    "doc_gap_source_only": "Source-code-only answer",
    "doc_gap_no_answer": "Not found in documentation or source code",
    "negative_feedback": "Negative rating",
}
