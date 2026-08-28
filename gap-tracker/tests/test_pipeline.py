# Copyright Hewlett Packard Enterprise Development LP.
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import requests

from ai_docops_analytics.cluster import (
    _embed_queries,
    _gather_gap_records,
    cluster_gaps,
)
from ai_docops_analytics.collect import run_collect
from ai_docops_analytics.client import OpenWebUIClient
from ai_docops_analytics.metrics import compute_metrics
from ai_docops_analytics.publish import (
    _publish_github_issue,
    _update_metrics_md,
    _upsert_metrics_csv,
    publish,
)
from ai_docops_analytics.report import generate_report


def _cfg(tmp_path: Path) -> dict:
    return {
        "paths": {
            "events_dir": str(tmp_path / "events"),
            "snapshots_dir": str(tmp_path / "snapshots"),
            "embeddings_dir": str(tmp_path / "embeddings"),
            "metrics_md": str(tmp_path / "METRICS.md"),
            "metrics_csv": str(tmp_path / "metrics.csv"),
            "demo_event_log": "",
        },
        "openwebui": {},
        "embedding": {
            "base_url": "http://localhost:8001/v1",
            "model": "model-a",
            "query_prefix": "search_query: ",
        },
        "clustering": {"distance_threshold": 0.3, "top_n_clusters": 20},
    }


def test_since_filter_is_timezone_safe_and_excludes_future_window(tmp_path):
    cfg = _cfg(tmp_path)
    events = Path(cfg["paths"]["events_dir"])
    events.mkdir()
    event = {
        "event": "query",
        "timestamp": "2026-04-10T03:39:29+00:00",
        "tier": "total_gap",
        "query": "A gap",
        "user_id": "user-1",
    }
    (events / "a.jsonl").write_text(json.dumps(event) + "\n")
    # Identical copies from overlapping inputs must not double-count.
    (events / "b.jsonl").write_text(json.dumps(event) + "\n")

    collected, _ = run_collect(cfg, since="2099-01-01")
    assert collected["query_events"] == []

    collected, _ = run_collect(cfg, since="2026-04-01")
    assert len(collected["query_events"]) == 1
    assert len(collected["derived_gap_events"]) == 1


def test_invalid_since_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Invalid --since"):
        run_collect(_cfg(tmp_path), since="not-a-date")


def test_non_object_json_event_is_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    events = Path(cfg["paths"]["events_dir"])
    events.mkdir()
    (events / "events.jsonl").write_text(
        '["not", "an", "event"]\n'
        '{"event":"query","tier":"adequate_docs","query":"SST?"}\n'
    )

    collected, _ = run_collect(cfg)

    assert len(collected["query_events"]) == 1


def test_each_collection_gets_its_own_snapshot_directory(tmp_path):
    cfg = _cfg(tmp_path)

    first_collection, first = run_collect(cfg)
    _, second = run_collect(cfg)

    assert first != second
    assert first.is_dir()
    assert second.is_dir()
    assert first_collection["source_status"]["event_log"] == {
        "available": False,
        "reason": "no JSONL event logs found",
    }


def test_interaction_id_pairs_query_and_gap_even_if_timestamps_differ(tmp_path):
    cfg = _cfg(tmp_path)
    events = Path(cfg["paths"]["events_dir"])
    events.mkdir()
    common = {
        "query": "How do I configure SST?",
        "user_id": "u1",
        "interaction_id": "interaction-1",
    }
    rows = [
        {
            **common,
            "event": "query",
            "timestamp": "2026-04-10T00:00:00+00:00",
            "tier": "total_gap",
        },
        {
            **common,
            "event": "doc_gap_no_answer",
            "timestamp": "2026-04-10T00:00:01+00:00",
        },
    ]
    (events / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )

    collected, _ = run_collect(cfg)

    assert collected["derived_gap_events"] == []


def test_different_interaction_ids_do_not_pair(tmp_path):
    cfg = _cfg(tmp_path)
    events = Path(cfg["paths"]["events_dir"])
    events.mkdir()
    common = {
        "query": "How do I configure SST?",
        "user_id": "u1",
        "timestamp": "2026-04-10T00:00:00+00:00",
    }
    rows = [
        {
            **common,
            "event": "query",
            "tier": "total_gap",
            "interaction_id": "query-interaction",
        },
        {
            **common,
            "event": "doc_gap_no_answer",
            "interaction_id": "other-interaction",
        },
    ]
    (events / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )

    collected, _ = run_collect(cfg)
    records = _gather_gap_records(collected)

    assert [
        event["interaction_id"]
        for event in collected["derived_gap_events"]
    ] == ["query-interaction"]
    assert len(records) == 2


def test_collect_uses_environment_credentials(
    tmp_path,
    monkeypatch,
):
    cfg = _cfg(tmp_path)
    cfg["openwebui"] = {
        "url": "http://localhost:3000",
        "email": "admin@localhost",
        "password": "admin",
    }
    fake_client = MagicMock(spec=OpenWebUIClient)
    fake_client.get_feedbacks_export.return_value = []

    monkeypatch.setenv(
        "SST_GAP_TRACKER_OPENWEBUI_EMAIL",
        "collector@example.com",
    )
    monkeypatch.setenv(
        "SST_GAP_TRACKER_OPENWEBUI_PASSWORD",
        "environment-password",
    )
    with patch(
        "ai_docops_analytics.client.OpenWebUIClient",
        return_value=fake_client,
    ) as client_class:
        run_collect(cfg, since="2026-08-24")

    client_class.assert_called_once_with(
        "http://localhost:3000",
        "collector@example.com",
        "environment-password",
        timeout=30.0,
    )
    assert [call[0] for call in fake_client.method_calls] == [
        "get_feedbacks_export",
    ]


def test_metrics_separate_gap_and_rejection_rates_and_count_feedback(tmp_path):
    collected = {
        "query_events": [
            {"tier": "adequate_docs", "user_id": "u1"},
            {"tier": "source_only", "user_id": "u1"},
            {"tier": "total_gap", "user_id": "u2"},
            {"tier": "adequate_docs", "user_id": "u2"},
        ],
        "gap_events": [
            {"event": "doc_gap_source_only", "user_id": "u1"},
            {"event": "doc_gap_no_answer", "user_id": "u2"},
        ],
        "derived_gap_events": [],
        "feedbacks": [
            {
                "user_id": "u3",
                "data": {"rating": -1, "comment": "Missing from the docs"},
            },
            {"data": {"rating": 1}},
        ],
    }
    cfg = _cfg(tmp_path)
    metrics = compute_metrics(collected, cfg)

    assert metrics["gap_rate"] == 0.5
    assert metrics["rejection_rate"] == 0.25
    assert metrics["gaps_collected"] == 3
    assert metrics["gaps_by_type"]["negative_feedback"] == 1
    assert metrics["unique_gap_contributors"] == 3


def test_no_questions_produce_unavailable_rates(tmp_path):
    cfg = _cfg(tmp_path)
    collected = {
        "query_events": [],
        "gap_events": [],
        "derived_gap_events": [],
        "feedbacks": [],
    }

    metrics = compute_metrics(collected, cfg)
    _update_metrics_md(metrics, cfg, dry_run=False)
    rendered = Path(cfg["paths"]["metrics_md"]).read_text()

    assert metrics["gap_rate"] is None
    assert metrics["rejection_rate"] is None
    assert metrics["code_only_rate"] is None
    assert "| Documentation-gap rate | N/A |" in rendered
    assert "| Rejection rate | N/A |" in rendered


def test_failed_feedback_collection_is_not_reported_as_no_ratings(tmp_path):
    cfg = _cfg(tmp_path)
    collected = {
        "query_events": [{"tier": "adequate_docs", "user_id": "u1"}],
        "gap_events": [],
        "derived_gap_events": [],
        "feedbacks": [],
        "source_status": {
            "feedbacks": {"available": False, "reason": "request failed"},
        },
    }

    metrics = compute_metrics(collected, cfg)
    _update_metrics_md(metrics, cfg, dry_run=False)
    rendered = Path(cfg["paths"]["metrics_md"]).read_text()

    assert metrics["feedback_available"] is False
    assert metrics["thumbs_up"] is None
    assert metrics["thumbs_down"] is None
    assert "N/A (feedback unavailable)" in rendered


def test_embedding_failure_preserves_gaps_and_marks_degraded_report(tmp_path):
    cfg = _cfg(tmp_path)
    collected = {
        "gap_events": [
            {
                "event": "doc_gap_no_answer",
                "query": "How do I deploy SST?",
                "user_id": "u1",
                "timestamp": "2026-04-10T00:00:00+00:00",
            }
        ],
        "derived_gap_events": [],
        "feedbacks": [],
    }
    with patch(
        "ai_docops_analytics.cluster.requests.post",
        side_effect=requests.ConnectionError("offline"),
    ):
        clusters = cluster_gaps(collected, cfg)

    assert len(clusters) == 1
    assert clusters[0]["clustering_method"] == "exact_text_fallback"
    report = generate_report(clusters)
    assert "Degraded grouping" in report
    assert "configured report limit still applies" in report


def test_embedding_response_indices_control_output_order(tmp_path):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    with patch("ai_docops_analytics.cluster.requests.post", return_value=response):
        embeddings = _embed_queries(
            ["first", "second"],
            {"base_url": "http://embed/v1", "model": "model-a"},
            "search_query: ",
            tmp_path,
        )
    np.testing.assert_array_equal(
        embeddings, np.array([[1.0, 0.0], [0.0, 1.0]])
    )


def test_cluster_date_range_normalizes_iso_and_epoch_timestamps(tmp_path):
    cfg = _cfg(tmp_path)
    collected = {
        "gap_events": [
            {
                "event": "doc_gap_no_answer",
                "query": "How do I configure SST?",
                "user_id": "u1",
                "timestamp": "2026-04-10T00:00:00Z",
            }
        ],
        "derived_gap_events": [],
        "feedbacks": [
            {
                "user_id": "u2",
                "created_at": 1_800_000_000_000,
                "data": {
                    "rating": -1,
                    "snapshot": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "SST configuration help",
                            }
                        ]
                    },
                },
            }
        ],
    }

    with patch(
        "ai_docops_analytics.cluster.requests.post",
        side_effect=lambda url, json, timeout: _embedding_response(
            len(json["input"])
        ),
    ):
        clusters = cluster_gaps(collected, cfg)

    assert len(clusters) == 1
    assert clusters[0]["date_range"] == [
        "2026-04-10T00:00:00+00:00",
        "2027-01-15T08:00:00+00:00",
    ]


def _embedding_response(count: int):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": [
            {"index": index, "embedding": [1.0, 0.0]}
            for index in range(count)
        ]
    }
    return response


def test_report_uses_representative_question_without_duplicate_examples():
    report = generate_report(
        [
            {
                "representative": "Can SST replay events?",
                "size": 3,
                "queries": [
                    "Can SST replay events?",
                    "Can SST replay events?",
                    "How does event replay work?",
                ],
                "users": ["u1"],
                "gap_types": ["doc_gap_no_answer"],
                "clustering_method": "semantic_embeddings",
            }
        ],
    )

    assert (
        "<code>Can SST replay events?</code> — 3 questions, 1 user"
        in report
    )
    assert report.count("<code>Can SST replay events?</code>") == 1
    assert "<code>How does event replay work?</code>" in report
    assert "Gap type: Not found in documentation or source code" in report


def test_report_does_not_claim_zero_users_when_ids_are_unavailable():
    report = generate_report(
        [
            {
                "representative": "How do I configure SST?",
                "size": 2,
                "queries": ["How do I configure SST?"],
                "users": [],
                "gap_types": ["doc_gap_no_answer"],
            }
        ],
    )

    assert "2 questions, user count unavailable" in report
    assert "0 users" not in report


def test_report_escapes_user_controlled_question_text():
    report = generate_report(
        [
            {
                "representative": "Can I use <script>?\n- [ ] injected",
                "size": 1,
                "queries": ["Can I use <script>?\n- [ ] injected"],
                "users": ["u1"],
                "gap_types": ["doc_gap_no_answer"],
            }
        ],
    )

    assert (
        "<code>Can I use &lt;script&gt;? - [ ] injected</code> "
        "— 1 question, 1 user"
        in report
    )
    assert "\n- [ ] injected" not in report
    assert "<script>" not in report


def test_negative_feedback_nested_chat_snapshot_is_a_gap():
    collected = {
        "gap_events": [],
        "derived_gap_events": [],
        "feedbacks": [
            {
                "user_id": "u1",
                "data": {
                    "rating": -1,
                    "snapshot": {
                        "chat": {
                            "messages": [
                                {"role": "user", "content": "Missing SST topic"}
                            ]
                        }
                    },
                },
            }
        ],
    }
    records = _gather_gap_records(collected)
    assert records[0]["query"] == "Missing SST topic"
    assert records[0]["gap_type"] == "negative_feedback"


def test_rating_on_a_gap_does_not_double_count_the_question(tmp_path):
    collected = {
        "gap_events": [
            {
                "event": "doc_gap_no_answer",
                "query": "Can SST replay events?",
                "user_id": "u1",
                "chat_id": "chat-1",
                "timestamp": "2026-08-26T12:00:00Z",
            }
        ],
        "derived_gap_events": [],
        "feedbacks": [
            {
                "user_id": "u1",
                "created_at": 1_777_200_001,
                "meta": {"chat_id": "chat-1", "message_id": "message-1"},
                "data": {"rating": -1},
                "snapshot": {
                    "chat": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Can SST replay events?",
                            }
                        ]
                    }
                },
            }
        ],
    }

    records = _gather_gap_records(collected)

    assert len(records) == 1
    assert records[0]["gap_type"] == "doc_gap_no_answer"
    assert records[0]["additional_gap_types"] == ["negative_feedback"]
    with patch(
        "ai_docops_analytics.cluster.requests.post",
        return_value=_embedding_response(1),
    ):
        clusters = cluster_gaps(collected, _cfg(tmp_path))
    assert clusters[0]["size"] == 1
    assert clusters[0]["gap_types"] == [
        "doc_gap_no_answer",
        "negative_feedback",
    ]


def test_metrics_history_replaces_same_day_row(tmp_path):
    cfg = _cfg(tmp_path)
    first = {
        "questions_asked": 2,
        "unique_users": 1,
        "tier_breakdown": {"adequate_docs": 2},
    }
    second = {
        "questions_asked": 3,
        "unique_users": 2,
        "tier_breakdown": {"adequate_docs": 3},
    }

    _upsert_metrics_csv(first, cfg, dry_run=False)
    _upsert_metrics_csv(second, cfg, dry_run=False)

    with open(cfg["paths"]["metrics_csv"], newline="") as metrics_file:
        rows = list(csv.DictReader(metrics_file))
    assert len(rows) == 1
    assert rows[0]["questions_asked"] == "3"
    assert rows[0]["unique_users"] == "2"


def test_metric_deltas_ignore_an_existing_current_week_row(tmp_path):
    cfg = _cfg(tmp_path)
    current_period = date.today() - timedelta(days=date.today().weekday())
    previous_date = (current_period - timedelta(days=7)).isoformat()
    Path(cfg["paths"]["metrics_csv"]).write_text(
        "date,questions_asked,gaps_collected,thumbs_ratio\n"
        f"{previous_date},5,2,0.5\n"
        f"{current_period.isoformat()},99,99,1.0\n"
    )
    collected = {
        "query_events": [{"tier": "adequate_docs", "user_id": "u1"}] * 7,
        "gap_events": [],
        "derived_gap_events": [],
        "feedbacks": [],
    }

    metrics = compute_metrics(collected, cfg)

    assert metrics["questions_asked_delta"] == 2
    assert metrics["gaps_collected_delta"] == -2


def test_publish_writes_local_outputs_before_github():
    calls = []
    with (
        patch(
            "ai_docops_analytics.publish._update_metrics_md",
            side_effect=lambda *args, **kwargs: calls.append("markdown"),
        ),
        patch(
            "ai_docops_analytics.publish._upsert_metrics_csv",
            side_effect=lambda *args, **kwargs: calls.append("csv"),
        ),
        patch(
            "ai_docops_analytics.publish._publish_github_issue",
            side_effect=lambda *args, **kwargs: calls.append("github"),
        ),
    ):
        publish("report", {}, {}, dry_run=False)

    assert calls == ["markdown", "csv", "github"]


def test_github_publish_updates_existing_issue(monkeypatch):
    current_period = date.today() - timedelta(days=date.today().weekday())
    existing = MagicMock()
    existing.title = (
        "Weekly Doc Gap Report — Week of "
        f"{current_period.strftime('%B %d, %Y')}"
    )
    repo = MagicMock()
    repo.get_issues.return_value = [existing]
    client = MagicMock()
    client.get_repo.return_value = repo

    with (
        patch("github.Auth.Token", return_value="auth"),
        patch("github.Github", return_value=client),
    ):
        _publish_github_issue(
            "updated report",
            {
                "github": {
                    "token": "token",
                    "repo": "owner/repo",
                    "labels": ["doc-gap"],
                }
            },
            dry_run=False,
        )

    existing.edit.assert_called_once_with(
        body="updated report",
        labels=["doc-gap"],
    )
    repo.create_issue.assert_not_called()


def test_unconfigured_github_preview_is_not_labeled_dry_run(capsys):
    _publish_github_issue(
        "report",
        {"github": {"token": "", "repo": ""}},
        dry_run=False,
    )

    output = capsys.readouterr().out
    assert "GITHUB ISSUE (not published)" in output
    assert "GITHUB ISSUE (dry-run)" not in output


def test_report_uses_the_snapshot_reporting_week():
    report = generate_report([], period_start=date(2026, 4, 6))

    assert "Week of April 06, 2026" in report
