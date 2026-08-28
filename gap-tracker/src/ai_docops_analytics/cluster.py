# Copyright Hewlett Packard Enterprise Development LP.
"""Gap query clustering using embeddings + agglomerative clustering.

This module turns raw gap queries into grouped "gap topics" by:

1. **Gathering** gap queries from three sources: explicit GAP_EVENTs,
   derived gaps from QUERY_EVENTs, and negative OpenWebUI
   feedbacks. Exact duplicate signals are removed while repeated interactions
   at different timestamps are retained as frequency evidence.

2. **Embedding** each query using the same model as the RAG pipeline
   (nomic-embed-text-v1.5 via llama-server on port 8001).  Queries are
   prefixed with ``"search_query: "`` as required by nomic's asymmetric
   architecture.  Embeddings are cached on disk by SHA256 hash to avoid
   re-computing on subsequent runs.

3. **Clustering** with ``AgglomerativeClustering(metric='cosine',
   linkage='average', distance_threshold=0.3)``.  Each cluster becomes
   a single gap topic in the weekly report.

Algorithm choice:

- **Agglomerative over K-means**: We don't know K (number of gap topics)
  upfront.  K-means requires specifying K and uses Euclidean distance,
  which is semantically wrong for normalized embeddings.
- **Agglomerative over DBSCAN**: DBSCAN labels singleton queries as noise,
  which is bad for our use case — a single user's unanswered question is
  a legitimate documentation gap signal.
- **Agglomerative over HDBSCAN**: Better than DBSCAN for variable-density
  data, but adds a C++ build dependency and is overkill for weekly batches
  of 10–100 gap queries.
- **Cosine distance over Euclidean**: Embedding models are trained using
  cosine similarity.  Euclidean distance over-weights vector magnitude
  (which varies with sentence length) instead of semantic direction.

Entry point: ``cluster_gaps(collected, cfg) -> list[ClusterInfo]``
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import requests
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics.pairwise import cosine_distances

from ai_docops_analytics.timestamps import normalize_timestamp

logger = logging.getLogger(__name__)


def cluster_gaps(collected: dict, cfg: dict) -> list[dict]:
    """Embed gap queries and cluster into topic groups.

    Returns a list of cluster dicts sorted by size descending::

        [{"representative": str, "size": int, "queries": [...],
          "users": [...], "gap_types": [...], "date_range": [min, max]}, ...]
    """
    # ── 1. Collect gap queries from all sources ──────────────────────
    gap_records = _gather_gap_records(collected)
    if not gap_records:
        logger.warning("No gap queries found — nothing to cluster")
        return []

    # ── 2. Embed ─────────────────────────────────────────────────────
    # Convert each gap query string into a dense vector using the same
    # embedding model as the RAG pipeline.  This ensures the clustering
    # operates in the same semantic space that determined the original
    # relevance scores.  Cached embeddings are reused across runs.
    emb_cfg = cfg.get("embedding", {})
    cache_dir = Path(cfg["paths"]["embeddings_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    prefix = emb_cfg.get("query_prefix", "search_query: ")

    queries = [r["query"] for r in gap_records]
    embeddings = _embed_queries(queries, emb_cfg, prefix, cache_dir)

    clust_cfg = cfg.get("clustering", {})
    threshold = clust_cfg.get("distance_threshold", 0.3)
    top_n = clust_cfg.get("top_n_clusters", 20)

    if embeddings is None or len(embeddings) == 0:
        # Never convert an infrastructure outage into "zero gaps." Exact
        # normalized-text grouping is less useful than semantic clustering,
        # but preserves every signal and makes the degraded mode explicit.
        logger.warning(
            "Embedding unavailable — falling back to exact-text grouping"
        )
        labels_by_query: dict[str, int] = {}
        labels = []
        for query in queries:
            normalized = " ".join(query.lower().split())
            labels.append(labels_by_query.setdefault(normalized, len(labels_by_query)))
        return _build_cluster_summaries(
            gap_records,
            labels,
            embeddings=None,
            method="exact_text_fallback",
            top_n=top_n,
        )

    # ── 3. Cluster ───────────────────────────────────────────────────
    # AgglomerativeClustering with n_clusters=None and a distance_threshold
    # auto-determines the number of clusters.
    if len(embeddings) == 1:
        labels = [0]
    else:
        model = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=threshold,
            metric="cosine",
            linkage="average",
        )
        labels = model.fit_predict(embeddings).tolist()

    return _build_cluster_summaries(
        gap_records,
        labels,
        embeddings=embeddings,
        method="semantic_embeddings",
        top_n=top_n,
    )


def _build_cluster_summaries(
    gap_records: list[dict],
    labels: list[int],
    *,
    embeddings: np.ndarray | None,
    method: str,
    top_n: int,
) -> list[dict]:
    """Build deterministic report records from cluster labels."""
    # For each cluster, compute the centroid (mean embedding) and pick
    # the query closest to it as the "representative" — the most typical
    # phrasing of that gap topic.  Also collect all unique users, gap
    # types, and the date range for the report.
    clusters_map: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        clusters_map.setdefault(label, []).append(idx)

    results: list[dict] = []
    for label in sorted(clusters_map):
        indices = clusters_map[label]
        if embeddings is not None:
            cluster_embs = embeddings[indices]
            centroid = cluster_embs.mean(axis=0, keepdims=True)
            dists = cosine_distances(centroid, cluster_embs)[0]
            rep_idx = indices[int(dists.argmin())]
        else:
            rep_idx = indices[0]

        cluster_queries = [gap_records[i]["query"] for i in indices]
        cluster_users = sorted(
            {gap_records[i].get("user_id") for i in indices} - {None, "unknown"}
        )
        cluster_types = sorted(
            {
                gap_type
                for i in indices
                for gap_type in (
                    [gap_records[i].get("gap_type")]
                    + gap_records[i].get("additional_gap_types", [])
                )
                if gap_type is not None
            }
        )
        timestamps = [gap_records[i].get("timestamp") for i in indices if gap_records[i].get("timestamp")]

        results.append({
            "representative": gap_records[rep_idx]["query"],
            "size": len(indices),
            "queries": cluster_queries,
            "users": cluster_users,
            "gap_types": cluster_types,
            "date_range": [min(timestamps), max(timestamps)] if timestamps else None,
            "clustering_method": method,
        })

    results.sort(key=lambda c: (-c["size"], c["representative"].lower()))
    return results[:top_n]


# ── Internal helpers ─────────────────────────────────────────────────────────

def _gather_gap_records(collected: dict) -> list[dict]:
    """Merge gap signals and suppress only exact duplicate records.

    Sources, in priority order:
    1. GAP_EVENTs from JSONL (explicit gap events from the filter's outlet)
    2. Derived gaps from QUERY_EVENTs when a paired GAP_EVENT is missing
    3. Negative feedbacks from OpenWebUI (thumbs-down with extractable query)

    Answerer events use the filter-generated interaction ID as their identity.
    Records without one use their question, user, signal type, and timestamp.
    """
    seen: set[tuple[str, str, str, str, str]] = set()
    records: list[dict] = []

    def _add(
        query: str,
        user_id: str,
        gap_type: str,
        timestamp: Any,
        chat_id: str | None = None,
        interaction_id: str | None = None,
    ) -> None:
        if not isinstance(query, str):
            return
        query = query.strip()
        if not query:
            return
        normalized_timestamp = normalize_timestamp(timestamp)
        key = (
            ("interaction", str(interaction_id), gap_type, "", "")
            if interaction_id
            else (
                "content",
                query.lower(),
                user_id or "unknown",
                gap_type,
                normalized_timestamp or "",
            )
        )
        if key in seen:
            return
        seen.add(key)
        records.append({
            "query": query,
            "user_id": user_id,
            "gap_type": gap_type,
            "timestamp": normalized_timestamp,
            "chat_id": chat_id,
            "interaction_id": interaction_id,
        })

    # GAP_EVENTs from JSONL
    for e in collected.get("gap_events", []):
        _add(
            e.get("query", ""),
            e.get("user_id", ""),
            e.get("event", ""),
            e.get("timestamp"),
            e.get("chat_id"),
            e.get("interaction_id"),
        )

    # Derived gaps from QUERY_EVENTs
    for e in collected.get("derived_gap_events", []):
        _add(
            e.get("query", ""),
            e.get("user_id", ""),
            e.get("event", ""),
            e.get("timestamp"),
            e.get("chat_id"),
            e.get("interaction_id"),
        )

    # Negative feedbacks from OpenWebUI
    for f in collected.get("feedbacks", []):
        data = f.get("data", {}) if isinstance(f.get("data"), dict) else {}
        rating = data.get("rating", f.get("rating"))
        if isinstance(rating, (int, float)) and rating < 0:
            # Try to extract the query from the feedback snapshot
            snapshot = data.get("snapshot") or f.get("snapshot") or {}
            msgs = _snapshot_messages(snapshot)
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            query = user_msgs[-1].get("content", "") if user_msgs else ""
            if not isinstance(query, str):
                query = ""
            user_id = f.get("user_id") or data.get("user_id", "")
            meta = f.get("meta", {}) if isinstance(f.get("meta"), dict) else {}
            chat_id = meta.get("chat_id")
            matching_record = next(
                (
                    record
                    for record in records
                    if chat_id
                    and record.get("chat_id") == chat_id
                    and record["query"].strip().lower()
                    == query.strip().lower()
                    and record.get("user_id") == user_id
                ),
                None,
            )
            if matching_record is not None:
                additional = matching_record.setdefault(
                    "additional_gap_types",
                    [],
                )
                if "negative_feedback" not in additional:
                    additional.append("negative_feedback")
                continue
            _add(
                query,
                user_id,
                "negative_feedback",
                f.get("created_at"),
                chat_id,
            )

    return records


def _snapshot_messages(snapshot: Any) -> list[dict]:
    """Extract messages across OpenWebUI feedback snapshot schemas."""
    if not isinstance(snapshot, dict):
        return []
    if isinstance(snapshot.get("messages"), list):
        return snapshot["messages"]
    chat = snapshot.get("chat")
    if isinstance(chat, dict) and isinstance(chat.get("messages"), list):
        return chat["messages"]
    return []


def _embed_queries(
    queries: list[str],
    emb_cfg: dict,
    prefix: str,
    cache_dir: Path,
) -> np.ndarray | None:
    """Embed queries via the llama-server /v1/embeddings endpoint.

    Uses a file-based cache keyed by SHA256(prefix + query_text).  Each
    cached embedding is a JSON file containing the raw float array.
    Queries not in cache are batched into groups of 16 (matching the
    RAG_EMBEDDING_BATCH_SIZE in webui-common.env) and sent to the
    embedding server in a single POST.

    Args:
        queries: The raw query strings (without prefix).
        emb_cfg: The ``embedding`` section of config.yaml.
        prefix: Task prefix for nomic (``"search_query: "``).
        cache_dir: Directory for cached embedding JSON files.

    Returns:
        A numpy array of shape (len(queries), embedding_dim), or None
        if any embedding call failed.
    """
    base_url = emb_cfg.get("base_url", "http://localhost:8001/v1")
    model = emb_cfg.get("model", "nomic-embed-text-v1.5.Q8_0.gguf")

    def cache_path_for(query: str) -> Path:
        identity = f"{model}\0{prefix}{query}"
        cache_key = hashlib.sha256(identity.encode()).hexdigest()
        return cache_dir / f"{cache_key}.json"

    all_embeddings: list[list[float]] = [[] for _ in queries]
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    # Check cache
    for i, q in enumerate(queries):
        cache_path = cache_path_for(q)
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text())
                if isinstance(cached, list) and cached:
                    all_embeddings[i] = cached
                    continue
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        uncached_indices.append(i)
        uncached_texts.append(prefix + q)

    # Embed uncached in batches of 16 (matches RAG_EMBEDDING_BATCH_SIZE)
    batch_size = 16
    for batch_start in range(0, len(uncached_texts), batch_size):
        batch = uncached_texts[batch_start : batch_start + batch_size]
        batch_indices = uncached_indices[batch_start : batch_start + batch_size]
        try:
            resp = requests.post(
                f"{base_url}/embeddings",
                json={"input": batch, "model": model},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            by_index = {
                item.get("index", position): item
                for position, item in enumerate(data)
                if isinstance(item, dict)
            }
            for j, idx in enumerate(batch_indices):
                item = by_index.get(j, {})
                emb = item.get("embedding", [])
                all_embeddings[idx] = emb
                # Cache
                cache_path_for(queries[idx]).write_text(json.dumps(emb))
        except Exception as exc:
            logger.warning(
                "Embedding API call failed for batch starting at %d: %s",
                batch_start,
                exc,
            )
            logger.debug("Embedding failure details", exc_info=True)
            return None

    # Validate: all must have embeddings
    dimensions = {len(e) for e in all_embeddings if isinstance(e, list)}
    if (
        any(not isinstance(e, list) or len(e) == 0 for e in all_embeddings)
        or len(dimensions) != 1
    ):
        logger.error("Embeddings are empty or have inconsistent dimensions")
        return None

    try:
        array = np.array(all_embeddings, dtype=np.float64)
    except (TypeError, ValueError):
        logger.exception("Embedding conversion failed")
        return None
    if not np.isfinite(array).all() or np.any(np.linalg.norm(array, axis=1) == 0):
        logger.error("Embeddings contain non-finite or zero-length vectors")
        return None
    return array
