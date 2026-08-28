#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Validate SST benchmark evidence against pinned local repositories."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sst_corpus import (
    SOURCE_SUFFIXES,
    default_repositories,
    fetch_origin,
    git_head,
    load_corpus_lock,
    run_git,
    tracked_files,
    validate_corpus_lock,
    verify_checkout,
)
from sst_question_bank import load_question_bank


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BANK = ROOT / "benchmarks" / "sst-question-bank.json"
DEFAULT_LOCK = ROOT / "benchmarks" / "sst-corpus-lock.json"
TEXT_SUFFIXES = SOURCE_SUFFIXES | {".md", ".mdx", ".txt"}


def contains_all(path: Path, terms: list[str]) -> list[str]:
    text = path.read_text(errors="replace").lower()
    return [term for term in terms if term.lower() not in text]


def searchable_files(root: Path):
    resolved_root = root.resolve()
    repository = Path(
        run_git(resolved_root, "rev-parse", "--show-toplevel")
    ).resolve()
    yield from tracked_files(repository, resolved_root, TEXT_SUFFIXES)


def search_phrase(phrase: str, roots: list[Path]) -> list[str]:
    needle = phrase.lower()
    hits: list[str] = []
    for root in roots:
        for path in searchable_files(root):
            if needle in path.read_text(errors="replace").lower():
                hits.append(str(path))
    return hits


def audit(
    bank: dict,
    docs_repo: Path,
    core_repo: Path,
    elements_repo: Path,
    *,
    check_commits: bool = True,
) -> list[str]:
    errors: list[str] = []
    corpus = bank["corpus"]
    expected_heads = {
        "sst-docs": (docs_repo, corpus["documentation_commit"]),
        "sst-core": (core_repo, corpus["sst_core_commit"]),
        "sst-elements": (elements_repo, corpus["sst_elements_commit"]),
    }
    if check_commits:
        for name, (repo, expected) in expected_heads.items():
            actual = git_head(repo)
            if actual != expected:
                errors.append(
                    f"{name} HEAD {actual} does not match bank commit {expected}"
                )

    repository_roots = {
        "sst-core": core_repo,
        "sst-elements": elements_repo,
    }
    docs_root = docs_repo / corpus.get("documentation_root", "docs")

    for question in bank["questions"]:
        question_id = question["id"]
        for evidence in question["documentation_evidence"]:
            path = docs_root / evidence["path"]
            if not path.is_file():
                errors.append(
                    f"{question_id}: missing documentation evidence {path}"
                )
                continue
            missing = contains_all(path, evidence.get("terms", []))
            if missing:
                errors.append(
                    f"{question_id}: {path} lacks evidence terms {missing}"
                )

        for evidence in question["source_evidence"]:
            repository = evidence["repository"]
            root = repository_roots.get(repository)
            if root is None:
                errors.append(
                    f"{question_id}: unsupported source repository {repository}"
                )
                continue
            path = root / evidence["path"]
            if not path.is_file():
                errors.append(f"{question_id}: missing source evidence {path}")
                continue
            missing = contains_all(path, evidence.get("terms", []))
            if missing:
                errors.append(
                    f"{question_id}: {path} lacks evidence terms {missing}"
                )

        if question["expected_tier"] == "source_only":
            for phrase in question.get("documentation_absence_queries", []):
                hits = search_phrase(phrase, [docs_root])
                if hits:
                    errors.append(
                        f"{question_id}: source-only documentation absence "
                        f"query {phrase!r} now matches "
                        + ", ".join(hits[:5])
                    )

        if question["expected_tier"] == "total_gap":
            roots = [docs_root, core_repo / "src", elements_repo / "src"]
            for phrase in question["absence_queries"]:
                hits = search_phrase(phrase, roots)
                if hits:
                    errors.append(
                        f"{question_id}: absence query {phrase!r} now matches "
                        + ", ".join(hits[:5])
                    )

    return errors


def tier_counts(bank: dict) -> dict[str, int]:
    tiers: dict[str, int] = {}
    for question in bank["questions"]:
        tier = question["expected_tier"]
        tiers[tier] = tiers.get(tier, 0) + 1
    return dict(sorted(tiers.items()))


def main() -> None:
    defaults = default_repositories()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--sst-root", type=Path)
    parser.add_argument("--docs-repo", type=Path)
    parser.add_argument("--core-repo", type=Path)
    parser.add_argument("--elements-repo", type=Path)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="fetch origin before checking that each checkout equals its upstream",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="skip corpus-lock validation while preparing a refresh",
    )
    args = parser.parse_args()

    if args.sst_root:
        defaults = default_repositories(args.sst_root.expanduser())
    repositories = {
        "sst-docs": (args.docs_repo or defaults["sst-docs"]).expanduser().resolve(),
        "sst-core": (args.core_repo or defaults["sst-core"]).expanduser().resolve(),
        "sst-elements": (
            args.elements_repo or defaults["sst-elements"]
        ).expanduser().resolve(),
    }

    if args.fetch:
        for repo in repositories.values():
            fetch_origin(repo)

    errors: list[str] = []
    checkout_metadata: dict[str, dict] = {}
    for name, repo in repositories.items():
        try:
            checkout_metadata[name] = verify_checkout(repo)
        except ValueError as exc:
            errors.append(str(exc))

    try:
        bank = load_question_bank(args.bank)
    except ValueError as exc:
        errors.append(str(exc))
        bank = {"questions": [], "corpus": {}}

    if not errors:
        errors.extend(
            audit(
                bank,
                repositories["sst-docs"],
                repositories["sst-core"],
                repositories["sst-elements"],
            )
        )

    lock_status = "skipped" if args.no_lock else "not_present"
    if not args.no_lock and args.lock.is_file():
        lock_status = "valid"
        try:
            lock = load_corpus_lock(args.lock)
            lock_errors = validate_corpus_lock(lock, repositories)
            if lock_errors:
                lock_status = "stale"
                errors.extend(lock_errors)
        except ValueError as exc:
            lock_status = "invalid"
            errors.append(str(exc))

    result = {
        "valid": not errors,
        "question_bank": str(args.bank),
        "questions": len(bank["questions"]),
        "tiers": tier_counts(bank),
        "checkouts": {
            name: metadata.get("commit")
            for name, metadata in checkout_metadata.items()
        },
        "corpus_lock": lock_status,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
    else:
        print(
            f"Question bank valid: {len(bank['questions'])} questions against "
            f"sst-docs {bank['corpus']['documentation_commit']}"
        )
        for tier, count in tier_counts(bank).items():
            print(f"  {tier}: {count}")
        print(f"  corpus lock: {lock_status}")

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
