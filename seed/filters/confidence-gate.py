#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Classify the answer delivered by SST Answerer from its cited sources.

Open WebUI performs retrieval and generation once. This filter then enforces a
small, deterministic contract for responses that claim SST evidence:

* an answer must cite SST evidence that passed native reranking;
* an answer labeled source-only must cite relevant source code, even when it
  also cites documentation that provides only partial support;
* the explicit not-found response is preserved without irrelevant sources.

Uncited replies pass through unchanged and do not create Gap Tracker events.
The filter does not call a model, search the corpus again, or contain
question-specific answer rules.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


logger = logging.getLogger(__name__)

SST_ANSWERER_ID = "sst-answerer"
EXACT_REJECTION = (
    "I couldn't find an answer in the available SST documentation or source "
    "code."
)
SOURCE_ONLY_PREFIX = (
    "Note: This answer is based on source code only — official documentation "
    "for this topic may be missing."
)
LEGACY_INSUFFICIENT_TAG = "[INSUFFICIENT_CONTEXT]"


def _message_text(content: Any) -> str:
    """Return text from a plain or multimodal Open WebUI message."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _visible_text(content: str) -> str:
    """Remove hidden reasoning blocks before checking the visible answer."""
    return re.sub(
        r"<details[^>]*>.*?</details>",
        "",
        content,
        flags=re.DOTALL,
    ).strip()


def _normalize_citations(content: str) -> str:
    """Normalize common citation spellings to Open WebUI's clickable form."""
    content = re.sub(
        r"[\[(]\s*source\s*[:=]?\s*(\d+)\s*[\])]",
        r"[\1]",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r"[\[(]\s*id\s*=\s*(\d+)\s*[\])]",
        r"[\1]",
        content,
        flags=re.IGNORECASE,
    )
    return content


def _citation_ids(content: str) -> set[int]:
    return {
        int(match)
        for match in re.findall(r"\[(\d+)\]", content)
        if int(match) > 0
    }


def _uncited_answer(content: Any) -> str | None:
    """Return a visible model response that makes no citation claim."""
    if not isinstance(content, str):
        return None
    visible = _normalize_citations(_visible_text(content))
    if (
        not visible
        or visible == EXACT_REJECTION
        or LEGACY_INSUFFICIENT_TAG in visible
        or _citation_ids(visible)
    ):
        return None
    return visible


def _clear_sources(body: dict, message: dict) -> None:
    """Remove retrieved context when the visible response cites none of it."""
    message["sources"] = []
    if "sources" in body:
        body["sources"] = []


def _source_items(message: dict) -> dict[int, dict[str, Any]]:
    """Map visible citation numbers to repository paths and reranker scores."""
    items: dict[int, dict[str, Any]] = {}
    citation_ids: dict[str, int] = {}
    for source_group in message.get("sources") or []:
        documents = (
            source_group.get("document")
            or source_group.get("documents")
            or []
        )
        metadata = (
            source_group.get("metadata")
            or source_group.get("metadatas")
            or []
        )
        source = source_group.get("source") or {}
        item_count = max(len(documents), len(metadata))
        if item_count == 0 and source:
            item_count = 1
        for index in range(item_count):
            item_metadata = metadata[index] if index < len(metadata) else {}
            source_key = (
                item_metadata.get("source")
                or source.get("id")
                or "N/A"
            )
            if source_key not in citation_ids:
                citation_id = len(citation_ids) + 1
                citation_ids[source_key] = citation_id
                items[citation_id] = {
                    "path": (
                        item_metadata.get("source")
                        or item_metadata.get("name")
                        or source.get("name")
                        or ""
                    ),
                    "score": None,
                }
            citation_id = citation_ids[source_key]
            score = item_metadata.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                previous = items[citation_id]["score"]
                if previous is None or score > previous:
                    items[citation_id]["score"] = float(score)
    return items


def _prefixes(value: str) -> tuple[str, ...]:
    return tuple(
        prefix.strip().lower().replace("\\", "/")
        for prefix in value.split(",")
        if prefix.strip()
    )


def _source_kind(
    path: str,
    documentation_prefixes: tuple[str, ...] = ("sst-docs",),
    source_prefixes: tuple[str, ...] = ("sst-core", "sst-elements"),
) -> str | None:
    normalized = path.lower().replace("\\", "/")
    is_documentation = normalized.startswith(documentation_prefixes)
    is_source = normalized.startswith(source_prefixes)
    if is_documentation == is_source:
        return None
    if is_documentation:
        return "documentation"
    if is_source:
        return "source"
    return None


def _requested_model_ids(
    body: dict,
    model: dict | None,
) -> set[str]:
    """Collect model identifiers supplied by Open WebUI."""
    identifiers: set[str] = set()
    body_model = body.get("model")
    if isinstance(body_model, str):
        identifiers.add(body_model)
    if isinstance(model, dict):
        for value in (
            model.get("id"),
            model.get("model"),
            (model.get("info") or {}).get("id"),
        ):
            if isinstance(value, str):
                identifiers.add(value)
    return identifiers


def _is_sst_answerer_request(
    body: dict,
    model: dict | None,
    model_ids: tuple[str, ...] = (SST_ANSWERER_ID,),
) -> bool:
    """Pass through other models if this filter is accidentally made global."""
    identifiers = _requested_model_ids(body, model)
    normalized_identifiers = {identifier.lower() for identifier in identifiers}
    return not identifiers or bool(normalized_identifiers & set(model_ids))


def _classify_answer(
    message: dict,
    min_cited_score: float = 0.0,
    documentation_prefixes: tuple[str, ...] = ("sst-docs",),
    source_prefixes: tuple[str, ...] = ("sst-core", "sst-elements"),
) -> tuple[str, str]:
    """Return the tier and normalized visible answer."""
    content = message.get("content", "")
    if not isinstance(content, str):
        return "total_gap", EXACT_REJECTION

    visible = _normalize_citations(_visible_text(content))
    if LEGACY_INSUFFICIENT_TAG in visible or visible == EXACT_REJECTION:
        return "total_gap", EXACT_REJECTION

    citations = _citation_ids(visible)
    sources = _source_items(message)
    if not citations or not citations <= set(sources):
        return "total_gap", EXACT_REJECTION

    cited_kinds = {
        _source_kind(
            sources[item]["path"],
            documentation_prefixes,
            source_prefixes,
        )
        for item in citations
    }
    if None in cited_kinds:
        return "total_gap", EXACT_REJECTION

    relevant_citations = {
        item
        for item in citations
        if sources[item]["score"] is not None
        and sources[item]["score"] >= min_cited_score
    }
    if not relevant_citations:
        return "total_gap", EXACT_REJECTION

    relevant_kinds = {
        _source_kind(
            sources[item]["path"],
            documentation_prefixes,
            source_prefixes,
        )
        for item in relevant_citations
    }
    model_marked_source_only = visible.startswith(SOURCE_ONLY_PREFIX)
    if model_marked_source_only:
        if "source" in relevant_kinds:
            return "source_only", visible
        return "total_gap", EXACT_REJECTION
    if "documentation" not in relevant_kinds:
        answer = visible
        if not answer.startswith(SOURCE_ONLY_PREFIX):
            answer = f"{SOURCE_ONLY_PREFIX}\n\n{answer}"
        return "source_only", answer

    return "adequate_docs", visible


class Filter:
    class Valves(BaseModel):
        gap_log_path: str = "/app/backend/data/gap_log.jsonl"
        min_cited_score: float = 0.0
        model_ids: str = SST_ANSWERER_ID
        documentation_prefixes: str = "sst-docs"
        source_prefixes: str = "sst-core,sst-elements"

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __model__: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        """Open WebUI owns retrieval and generation; no pre-processing needed."""
        return body

    async def outlet(
        self,
        body: dict,
        __chat_id__: str | None = None,
        __user__: dict | None = None,
        __model__: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        model_ids = _prefixes(self.valves.model_ids)
        if not _is_sst_answerer_request(body, __model__, model_ids):
            return body

        messages = body.get("messages") or []
        user_messages = [
            message for message in messages if message.get("role") == "user"
        ]
        assistant_messages = [
            message
            for message in messages
            if message.get("role") == "assistant"
        ]
        if not user_messages or not assistant_messages:
            return body

        query = _message_text(user_messages[-1].get("content", ""))
        last_message = assistant_messages[-1]
        uncited_answer = _uncited_answer(last_message.get("content"))
        if uncited_answer is not None:
            last_message["content"] = uncited_answer
            _clear_sources(body, last_message)
            return body

        tier, answer = _classify_answer(
            last_message,
            self.valves.min_cited_score,
            _prefixes(self.valves.documentation_prefixes),
            _prefixes(self.valves.source_prefixes),
        )
        last_message["content"] = answer
        if tier == "total_gap":
            _clear_sources(body, last_message)

        timestamp = datetime.now(timezone.utc).isoformat()
        interaction_id = uuid.uuid4().hex
        user_id = (__user__ or {}).get("id", "unknown")
        chat_id = __chat_id__ or body.get("chat_id")
        query_event = {
            "event": "query",
            "interaction_id": interaction_id,
            "timestamp": timestamp,
            "tier": tier,
            "query": query,
            "user_id": user_id,
            "chat_id": chat_id,
        }
        is_verification = bool(
            (body.get("metadata") or {}).get("verification_run")
        )
        if not is_verification:
            logger.warning("QUERY_EVENT %s", json.dumps(query_event))
            self._write_event(query_event)

        gap_type = {
            "source_only": "doc_gap_source_only",
            "total_gap": "doc_gap_no_answer",
        }.get(tier)
        if gap_type and not is_verification:
            gap_event = {
                "event": gap_type,
                "interaction_id": interaction_id,
                "timestamp": timestamp,
                "query": query,
                "response_snippet": answer[:200],
                "user_id": user_id,
                "chat_id": chat_id,
            }
            logger.warning("GAP_EVENT %s", json.dumps(gap_event))
            self._write_event(gap_event)
        return body

    def _write_event(self, event: dict) -> None:
        path = self.valves.gap_log_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as event_file:
                event_file.write(json.dumps(event) + "\n")
        except OSError as exc:
            logger.warning(
                "outcome_tracker: failed to write event log: %s",
                exc,
            )
