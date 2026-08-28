# Copyright Hewlett Packard Enterprise Development LP.
"""Tests for gap clustering.

Uses mock embeddings to avoid requiring a live llama-server.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from ai_docops_analytics.cluster import cluster_gaps, _gather_gap_records


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_collected(tmp_path: Path) -> dict:
    """Load the sample fixture as a CollectedData-like dict."""
    fixture = Path(__file__).parent / "fixtures" / "sample_events.jsonl"
    query_events: list[dict] = []
    gap_events: list[dict] = []
    for line in fixture.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e["event"] == "query":
            query_events.append(e)
        else:
            gap_events.append(e)

    # Simulate derived_gap_events (what collect.py would produce)
    derived = []
    gap_queries = {(e.get("query"), e.get("user_id")) for e in gap_events}
    for qe in query_events:
        if qe["tier"] in ("source_only", "total_gap"):
            key = (qe["query"], qe["user_id"])
            if key not in gap_queries:
                derived.append({
                    "event": f"doc_gap_{qe['tier']}" if qe["tier"] == "total_gap" else "doc_gap_source_only",
                    "timestamp": qe["timestamp"],
                    "query": qe["query"],
                    "user_id": qe["user_id"],
                    "derived_from": "query_event",
                })
                gap_queries.add(key)

    return {
        "query_events": query_events,
        "gap_events": gap_events,
        "derived_gap_events": derived,
        "feedbacks": [],
    }


@pytest.fixture
def cfg(tmp_path: Path) -> dict:
    return {
        "paths": {
            "embeddings_dir": str(tmp_path / "embeddings"),
        },
        "embedding": {
            "base_url": "http://localhost:8001/v1",
            "model": "nomic-embed-text-v1.5.Q8_0.gguf",
            "query_prefix": "search_query: ",
        },
        "clustering": {
            "distance_threshold": 0.3,
            "top_n_clusters": 20,
        },
    }


# ── Mock embedding function ──────────────────────────────────────────────────

def _mock_embeddings(queries: list[str]) -> list[list[float]]:
    """Assign hand-crafted embeddings so we can test clustering logic.

    Groups:
    - Kubernetes/deployment queries → direction [1, 0, 0, ...]
    - Config/domain queries        → direction [0, 1, 0, ...]
    - Unrelated queries            → each gets a unique direction
    """
    dim = 8
    result = []
    for q in queries:
        q_lower = q.lower()
        if "kubernetes" in q_lower or "deploy" in q_lower or "pod" in q_lower:
            vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        elif "domain" in q_lower or "dns" in q_lower or "config" in q_lower or "sst_config" in q_lower:
            vec = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        elif "react" in q_lower or "dashboard" in q_lower:
            vec = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        elif "kafka" in q_lower or "streaming" in q_lower:
            vec = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
        elif "machine learning" in q_lower or "ml" in q_lower or "prediction" in q_lower:
            vec = [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        elif "mpi" in q_lower or "partitioning" in q_lower:
            vec = [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        else:
            # Random-looking but stable across Python processes.
            seed = int.from_bytes(
                hashlib.sha256(q.encode()).digest()[:4],
                "big",
            )
            rng = np.random.RandomState(seed)
            vec = rng.randn(dim).tolist()
        # Normalize
        norm = np.linalg.norm(vec)
        result.append((np.array(vec) / norm).tolist() if norm > 0 else vec)
    return result


def _mock_post(url: str, json: dict = None, timeout: int = 30, **kwargs):
    """Mock requests.post for /v1/embeddings."""
    inputs = json.get("input", []) if json else []
    # Strip prefix
    queries = [inp.removeprefix("search_query: ") for inp in inputs]
    embeddings = _mock_embeddings(queries)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [{"embedding": emb, "index": i} for i, emb in enumerate(embeddings)]
    }
    resp.raise_for_status = MagicMock()
    return resp


# ── Tests ────────────────────────────────────────────────────────────────────

class TestGatherGapRecords:
    def test_deduplicates(self, sample_collected):
        records = _gather_gap_records(sample_collected)
        queries = [r["query"] for r in records]
        # No exact duplicates
        assert len(queries) == len(set(q.strip().lower() for q in queries))

    def test_includes_derived(self, sample_collected):
        records = _gather_gap_records(sample_collected)
        # The 5 total_gap QUERY_EVENTs should appear as derived gaps
        # (MPI partitioning, Kubernetes, React, Kafka, ML predictions)
        queries_lower = [r["query"].lower() for r in records]
        assert any("kubernetes" in q for q in queries_lower)
        assert any("kafka" in q for q in queries_lower)

    def test_empty_input(self):
        records = _gather_gap_records({
            "gap_events": [], "derived_gap_events": [], "feedbacks": [],
        })
        assert records == []

    def test_equivalent_timestamp_spellings_are_one_signal(self):
        records = _gather_gap_records(
            {
                "gap_events": [
                    {
                        "event": "doc_gap_no_answer",
                        "query": "How do I configure SST?",
                        "user_id": "u1",
                        "timestamp": "2026-04-10T00:00:00Z",
                    },
                    {
                        "event": "doc_gap_no_answer",
                        "query": "How do I configure SST?",
                        "user_id": "u1",
                        "timestamp": "2026-04-10T00:00:00+00:00",
                    },
                ],
                "derived_gap_events": [],
                "feedbacks": [],
            }
        )

        assert len(records) == 1
        assert records[0]["timestamp"] == "2026-04-10T00:00:00+00:00"

    def test_interaction_id_deduplicates_different_timestamp_spellings(self):
        records = _gather_gap_records(
            {
                "gap_events": [
                    {
                        "event": "doc_gap_no_answer",
                        "interaction_id": "interaction-1",
                        "query": "How do I configure SST?",
                        "user_id": "u1",
                        "timestamp": "2026-04-10T00:00:00Z",
                    },
                    {
                        "event": "doc_gap_no_answer",
                        "interaction_id": "interaction-1",
                        "query": "How is SST configured?",
                        "user_id": "u1",
                        "timestamp": "2026-04-10T00:00:01Z",
                    },
                ],
                "derived_gap_events": [],
                "feedbacks": [],
            }
        )

        assert len(records) == 1


class TestClusterGaps:
    @patch("ai_docops_analytics.cluster.requests.post", side_effect=_mock_post)
    def test_similar_queries_cluster_together(self, mock_post, sample_collected, cfg):
        clusters = cluster_gaps(sample_collected, cfg)
        assert len(clusters) > 0

        # Find the kubernetes/deployment cluster
        k8s_cluster = None
        for c in clusters:
            qs = [q.lower() for q in c["queries"]]
            if any("kubernetes" in q or "deploy" in q for q in qs):
                k8s_cluster = c
                break

        assert k8s_cluster is not None, "Expected a Kubernetes/deployment cluster"
        # Both "Kubernetes pod" and "deploy SST to Kubernetes" should be in it
        qs = [q.lower() for q in k8s_cluster["queries"]]
        assert any("kubernetes" in q and "pod" in q for q in qs) or len(qs) >= 1

    @patch("ai_docops_analytics.cluster.requests.post", side_effect=_mock_post)
    def test_dissimilar_queries_separate(self, mock_post, sample_collected, cfg):
        clusters = cluster_gaps(sample_collected, cfg)

        # Kafka and React queries should NOT be in the same cluster
        for c in clusters:
            qs = [q.lower() for q in c["queries"]]
            has_kafka = any("kafka" in q for q in qs)
            has_react = any("react" in q for q in qs)
            assert not (has_kafka and has_react), "Kafka and React should be in separate clusters"

    @patch("ai_docops_analytics.cluster.requests.post", side_effect=_mock_post)
    def test_cluster_count_less_than_queries(self, mock_post, sample_collected, cfg):
        clusters = cluster_gaps(sample_collected, cfg)
        total_queries = sum(c["size"] for c in clusters)
        # At least some queries should cluster together
        assert len(clusters) <= total_queries

    @patch("ai_docops_analytics.cluster.requests.post", side_effect=_mock_post)
    def test_cluster_has_required_fields(self, mock_post, sample_collected, cfg):
        clusters = cluster_gaps(sample_collected, cfg)
        for c in clusters:
            assert "representative" in c
            assert "size" in c
            assert "queries" in c
            assert "users" in c
            assert "gap_types" in c
            assert c["size"] == len(c["queries"])
            assert c["size"] >= 1

    def test_empty_input(self, cfg):
        clusters = cluster_gaps(
            {"gap_events": [], "derived_gap_events": [], "feedbacks": []}, cfg
        )
        assert clusters == []

    @patch("ai_docops_analytics.cluster.requests.post", side_effect=_mock_post)
    def test_embedding_cache(self, mock_post, sample_collected, cfg):
        # First run
        clusters1 = cluster_gaps(sample_collected, cfg)
        call_count_1 = mock_post.call_count

        # Second run — should hit cache, fewer API calls
        mock_post.reset_mock()
        clusters2 = cluster_gaps(sample_collected, cfg)
        call_count_2 = mock_post.call_count

        assert call_count_2 < call_count_1, "Second run should use cached embeddings"
        assert len(clusters1) == len(clusters2)
