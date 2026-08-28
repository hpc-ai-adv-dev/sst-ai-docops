#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Load and validate the hand-reviewed SST Answerer question bank."""

from __future__ import annotations

import json
from pathlib import Path


VALID_TIERS = frozenset({"adequate_docs", "source_only", "total_gap"})
VALID_DIFFICULTIES = frozenset({"easy", "medium", "hard"})
CORPUS_FIELDS = (
    "documentation_repository",
    "documentation_commit",
    "sst_core_repository",
    "sst_core_commit",
    "sst_elements_repository",
    "sst_elements_commit",
)


def _nonempty_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_string_list(value, field: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if any(not _nonempty_string(item) for item in value):
        raise ValueError(f"{field} must contain only non-empty strings")


def _validate_evidence(value, field: str, *, source: bool = False) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    for index, item in enumerate(value, 1):
        prefix = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        if not _nonempty_string(item.get("path")):
            raise ValueError(f"{prefix}.path must be a non-empty string")
        _validate_string_list(
            item.get("terms"),
            f"{prefix}.terms",
            allow_empty=False,
        )
        if source and item.get("repository") not in {
            "sst-core",
            "sst-elements",
        }:
            raise ValueError(
                f"{prefix}.repository must be sst-core or sst-elements"
            )


def validate_question_bank(bank: dict) -> dict:
    """Return a valid bank or raise ValueError with a specific problem."""
    if not isinstance(bank, dict):
        raise ValueError("Question bank must be a JSON object")
    if bank.get("schema_version") != 1:
        raise ValueError("Question bank schema_version must be 1")

    for field in ("name", "review_status", "validated_at"):
        if not _nonempty_string(bank.get(field)):
            raise ValueError(f"Question bank {field} must be a non-empty string")

    corpus = bank.get("corpus")
    if not isinstance(corpus, dict):
        raise ValueError("Question bank must contain corpus metadata")
    for field in CORPUS_FIELDS:
        if not _nonempty_string(corpus.get(field)):
            raise ValueError(f"Question bank corpus.{field} is required")
    documentation_root = corpus.get("documentation_root", "docs")
    if not _nonempty_string(documentation_root):
        raise ValueError(
            "Question bank corpus.documentation_root must be a non-empty string"
        )

    tier_contract = bank.get("tier_contract")
    if not isinstance(tier_contract, dict):
        raise ValueError("Question bank must contain tier_contract")
    for tier in VALID_TIERS:
        if not _nonempty_string(tier_contract.get(tier)):
            raise ValueError(
                f"Question bank tier_contract.{tier} is required"
            )

    questions = bank.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Question bank must contain at least one question")

    ids: set[str] = set()
    for index, question in enumerate(questions, 1):
        prefix = f"Question {index}"
        if not isinstance(question, dict):
            raise ValueError(f"{prefix} must be an object")

        question_id = question.get("id")
        if not _nonempty_string(question_id):
            raise ValueError(f"{prefix} must have a non-empty id")
        if question_id in ids:
            raise ValueError(f"Duplicate question id: {question_id}")
        ids.add(question_id)

        for field in ("persona", "use_case", "question", "notes"):
            if not _nonempty_string(question.get(field)):
                raise ValueError(f"{prefix} must have a non-empty {field}")

        difficulty = question.get("difficulty")
        if difficulty not in VALID_DIFFICULTIES:
            raise ValueError(
                f"{prefix} difficulty must be one of "
                + ", ".join(sorted(VALID_DIFFICULTIES))
            )

        tier = question.get("expected_tier")
        if tier not in VALID_TIERS:
            raise ValueError(f"{prefix} has invalid expected_tier: {tier}")

        key_terms = question.get("key_terms")
        _validate_string_list(key_terms, f"{prefix} key_terms")
        docs = question.get("documentation_evidence")
        source = question.get("source_evidence")
        _validate_evidence(docs, f"{prefix} documentation_evidence")
        _validate_evidence(
            source,
            f"{prefix} source_evidence",
            source=True,
        )

        if tier == "adequate_docs" and not docs:
            raise ValueError(
                f"{prefix} adequate_docs requires documentation evidence"
            )
        if tier == "source_only":
            if not source:
                raise ValueError(
                    f"{prefix} source_only requires source evidence"
                )
            _validate_string_list(
                question.get("documentation_absence_queries"),
                f"{prefix} documentation_absence_queries",
                allow_empty=False,
            )
        if tier == "total_gap":
            if question.get("reference_answer") is not None:
                raise ValueError(
                    f"{prefix} total_gap reference_answer must be null"
                )
            if docs or source:
                raise ValueError(
                    f"{prefix} total_gap cannot contain supporting evidence"
                )
            _validate_string_list(
                question.get("absence_queries"),
                f"{prefix} absence_queries",
                allow_empty=False,
            )
        else:
            if not _nonempty_string(question.get("reference_answer")):
                raise ValueError(f"{prefix} requires a reference_answer")
            if not key_terms:
                raise ValueError(f"{prefix} requires key_terms")

    return bank


def load_question_bank(path: str | Path) -> dict:
    """Read and validate a question-bank JSON file."""
    bank_path = Path(path)
    try:
        bank = json.loads(bank_path.read_text())
    except OSError as exc:
        raise ValueError(f"Cannot read question bank {bank_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid question-bank JSON in {bank_path}: {exc}"
        ) from exc
    return validate_question_bank(bank)
