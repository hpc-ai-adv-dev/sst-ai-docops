#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""
SST Answerer Verification Test Runner
=====================================
End-to-end regression test for the SST Answerer.

Sends expert-curated questions to the SST Answerer model via the
OpenWebUI chat API, then checks each response for expected behavior:

  - Do answer citations resolve to retrieved sources?
  - Did it correctly reject an unanswerable question?
  - Did the response mention expected key terms from the reference answer?

Each test question is tagged with an expected tier (adequate_docs,
source_only, total_gap) that matches the confidence gate filter's
three-tier classification.  The runner checks whether the response is
consistent with that tier.

Results are printed as a per-question pass/fail table with a per-tier
breakdown, or as JSON for automation.

Passing this bank confirms these regression cases. It is not a general
measurement of Answerer accuracy.

Prerequisites:
  - The stack must be running (./start.sh)
  - The SST Answerer model and knowledge base must be configured

Usage:
    python3 scripts/run_verification.py                    # human-readable
    python3 scripts/run_verification.py --json             # machine-readable
    python3 scripts/run_verification.py --url http://HOST:PORT --model NAME

Options:
    --url URL        OpenWebUI base URL (default: http://localhost:3000)
    --model NAME     Model name substring to match (default: "SST Answerer")
    --email EMAIL    Login email (default: admin@localhost)
    --password PASS  Login password (default: admin)
    --json           Output results as JSON instead of a table
    --ids ID [...]   Run selected question IDs
    --question-bank PATH
                     Use another compatible question-bank file
    --validate-only  Validate the question bank without contacting Open WebUI
    --fail-under RATE
                     Required pass rate from 0 to 1 (default: 1)
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from sst_question_bank import (
    VALID_TIERS,
    load_question_bank,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTION_BANK = ROOT / "benchmarks" / "sst-question-bank.json"

EXACT_REJECTION = (
    "I couldn't find an answer in the available SST documentation or source "
    "code."
)
SOURCE_ONLY_PREFIX = (
    "Note: This answer is based on source code only — official documentation "
    "for this topic may be missing."
)


def signin(base_url: str, email: str, password: str) -> str:
    """Sign in and return a JWT token."""
    payload = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/v1/auths/signin",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["token"]


def get_models(base_url: str, token: str) -> list[dict]:
    """Fetch available models."""
    req = urllib.request.Request(
        f"{base_url}/api/v1/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("data", data) if isinstance(data, dict) else data


def find_model_id(models: list, name_hint: str) -> str:
    """Find model ID by name substring."""
    for m in models:
        model_name = m.get("name", "") or m.get("id", "")
        if name_hint.lower() in model_name.lower():
            return m.get("id", model_name)
    raise ValueError(
        f"No model matching '{name_hint}'. Available: "
        + ", ".join(m.get("name", m.get("id", "?")) for m in models)
    )


def send_question(
    base_url: str, token: str, model_id: str, question: str
) -> dict:
    """Send a chat completion request and return the response."""
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": question}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def finalize_response(
    base_url: str,
    token: str,
    model_id: str,
    question: str,
    response: dict,
) -> dict:
    """Run the same completed-chat outlet used by the Open WebUI interface."""
    choices = response.get("choices") or []
    if not choices:
        return response
    assistant = dict(choices[0].get("message") or {})
    assistant["sources"] = response.get("sources") or []
    payload = json.dumps(
        {
            "model": model_id,
            "chat_id": "verification-run",
            "id": "verification-message",
            "session_id": "verification-run",
            "metadata": {"verification_run": True},
            "messages": [
                {"role": "user", "content": question},
                assistant,
            ],
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat/completed",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        completed = json.loads(resp.read())
    completed_messages = completed.get("messages") or []
    assistant_messages = [
        message
        for message in completed_messages
        if message.get("role") == "assistant"
    ]
    if assistant_messages:
        response["choices"][0]["message"]["content"] = assistant_messages[-1].get(
            "content", ""
        )
    return response


def citation_ids(content: str) -> set[int]:
    """Return positive citation numbers from visible answer text."""
    return {
        int(match)
        for match in re.findall(r"\[(?:id=)?(\d+)\]", content)
        if int(match) > 0
    }


def _source_items(response: dict):
    """Yield the citation number and path for each retrieved source item."""
    source_ids: dict[str, int] = {}
    for source_group in response.get("sources") or []:
        metadata = (
            source_group.get("metadata")
            or source_group.get("metadatas")
            or []
        )
        documents = (
            source_group.get("document")
            or source_group.get("documents")
            or []
        )
        source = source_group.get("source") or {}
        item_count = max(len(documents), len(metadata))
        if item_count == 0 and source:
            item_count = 1
        for index in range(item_count):
            item = metadata[index] if index < len(metadata) else {}
            source_key = (
                item.get("source")
                or source.get("id")
                or "N/A"
            )
            if source_key in source_ids:
                continue
            source_index = len(source_ids) + 1
            source_ids[source_key] = source_index
            path = (
                item.get("source")
                or item.get("name")
                or source.get("name")
                or "unknown"
            )
            yield source_index, path


def source_paths(response: dict, *, cited_only: bool = False) -> list[str]:
    """Return retrieved source paths, optionally limited to cited items."""
    choices = response.get("choices") or []
    content = (
        choices[0].get("message", {}).get("content", "")
        if choices
        else ""
    )
    cited = citation_ids(content)
    paths: list[str] = []
    for source_index, path in _source_items(response):
        if cited_only and source_index not in cited:
            continue
        if path not in paths:
            paths.append(path)
    return paths


def evaluate_response(
    response: dict, expected_tier: str, key_terms: list[str] | None = None,
    min_key_term_rate: float = 2 / 3,
    expected_evidence_paths: list[str] | None = None,
) -> dict:
    """Check response against expectations. Returns result dict."""
    choices = response.get("choices", [])
    content = ""
    if choices:
        content = choices[0].get("message", {}).get("content", "")

    visible_content = re.sub(
        r"<details[^>]*>.*?</details>", "", content, flags=re.DOTALL
    ).strip()
    content_lower = visible_content.lower()

    has_insufficient = "[INSUFFICIENT_CONTEXT]" in content
    normalized_rejection = visible_content.replace(
        "[INSUFFICIENT_CONTEXT]", ""
    ).strip()
    has_exact_rejection = normalized_rejection == EXACT_REJECTION
    cited = citation_ids(visible_content)
    available_source_ids = {
        source_id for source_id, _path in _source_items(response)
    }
    unresolved_citation_ids = sorted(cited - available_source_ids)
    has_citations = bool(cited)
    has_resolved_citations = has_citations and not unresolved_citation_ids

    # Check which key terms appear in the response
    terms = key_terms or []
    matched_terms = [t for t in terms if t.lower() in content_lower]
    term_hit_rate = len(matched_terms) / len(terms) if terms else 1.0
    has_required_terms = term_hit_rate + 1e-12 >= min_key_term_rate
    cited_paths = source_paths(response, cited_only=True)
    expected_paths = expected_evidence_paths or []

    def path_matches(actual: str, expected: str) -> bool:
        normalized_actual = actual.lower().replace("\\", "/").replace("__", "/")
        normalized_expected = expected.lower().replace("\\", "/")
        return normalized_actual.endswith(normalized_expected)

    matched_evidence_paths = [
        expected
        for expected in expected_paths
        if any(path_matches(actual, expected) for actual in cited_paths)
    ]
    has_expected_evidence = (
        bool(matched_evidence_paths) if expected_paths else True
    )

    if expected_tier == "adequate_docs":
        passed = (
            has_resolved_citations
            and has_required_terms
            and has_expected_evidence
            and not has_insufficient
            and not has_exact_rejection
            and not visible_content.startswith(SOURCE_ONLY_PREFIX)
        )
    elif expected_tier == "total_gap":
        # A vague mention of a "documentation gap" inside a fabricated answer
        # is not an honest rejection. The hard-gated response must be exact.
        passed = has_exact_rejection
    elif expected_tier == "source_only":
        passed = (
            visible_content.startswith(SOURCE_ONLY_PREFIX)
            and has_resolved_citations
            and has_required_terms
            and has_expected_evidence
            and not has_insufficient
        )
    else:
        passed = False

    return {
        "passed": passed,
        "has_citations": has_citations,
        "has_resolved_citations": has_resolved_citations,
        "unresolved_citation_ids": unresolved_citation_ids,
        "has_exact_rejection": has_exact_rejection,
        "has_insufficient_tag": has_insufficient,
        "has_required_terms": has_required_terms,
        "has_expected_evidence": has_expected_evidence,
        "expected_evidence_paths_matched": matched_evidence_paths,
        "key_terms_matched": matched_terms,
        "key_terms_total": len(terms),
        "key_term_hit_rate": round(term_hit_rate, 2),
        "response_length": len(content),
        "response_snippet": content[:200],
    }


def main():
    parser = argparse.ArgumentParser(
        description="SST Answerer Verification Test Runner"
    )
    parser.add_argument("--url", default="http://localhost:3000", help="OpenWebUI URL")
    parser.add_argument("--model", default="SST Answerer", help="Model name to test")
    parser.add_argument("--email", default="admin@localhost")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--ids",
        nargs="+",
        metavar="ID",
        help="run only these question IDs (space- or comma-separated)",
    )
    parser.add_argument(
        "--question-bank",
        default=str(DEFAULT_QUESTION_BANK),
        help="versioned question-bank JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate and summarize the question bank without contacting Open WebUI",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=1.0,
        metavar="RATE",
        help="exit non-zero below this pass rate (default: %(default)s)",
    )
    args = parser.parse_args()
    if not 0 <= args.fail_under <= 1:
        parser.error("--fail-under must be between 0 and 1")

    try:
        question_bank = load_question_bank(args.question_bank)
    except ValueError as exc:
        parser.error(str(exc))
    test_questions = question_bank["questions"]
    if args.ids:
        requested_ids = {
            question_id
            for value in args.ids
            for question_id in value.split(",")
            if question_id
        }
        known_ids = {question["id"] for question in test_questions}
        unknown_ids = sorted(requested_ids - known_ids)
        if unknown_ids:
            parser.error(
                "unknown question ID(s): " + ", ".join(unknown_ids)
            )
        test_questions = [
            question
            for question in test_questions
            if question["id"] in requested_ids
        ]

    if args.validate_only:
        tiers = {
            tier: sum(q["expected_tier"] == tier for q in test_questions)
            for tier in sorted(VALID_TIERS)
        }
        result = {
            "name": question_bank["name"],
            "review_status": question_bank["review_status"],
            "documentation_commit": question_bank["corpus"][
                "documentation_commit"
            ],
            "questions": len(test_questions),
            "tiers": tiers,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                f"{result['name']}: {result['questions']} questions, "
                f"docs {result['documentation_commit']}"
            )
            for tier, count in tiers.items():
                print(f"  {tier}: {count}")
        return

    print(f"Signing in to {args.url}...", file=sys.stderr)
    token = signin(args.url, args.email, args.password)

    models = get_models(args.url, token)
    model_id = find_model_id(models, args.model)
    print(f"Using model: {model_id}", file=sys.stderr)

    results = []
    passed = 0
    failed = 0

    for i, test in enumerate(test_questions, 1):
        print(
            f"[{i}/{len(test_questions)}] {test['expected_tier']:15s} | "
            f"{test['question'][:60]}...",
            file=sys.stderr,
        )
        try:
            response = send_question(args.url, token, model_id, test["question"])
            raw_answer = (
                (response.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            retrieved_paths = source_paths(response)
            raw_cited_paths = source_paths(response, cited_only=True)
            response = finalize_response(
                args.url, token, model_id, test["question"], response
            )
            cited_paths = source_paths(response, cited_only=True)
            result = evaluate_response(
                response,
                test["expected_tier"],
                test.get("key_terms"),
                expected_evidence_paths=(
                    [
                        item["path"]
                        for item in test["documentation_evidence"]
                    ]
                    if test["expected_tier"] == "adequate_docs"
                    else [
                        item["path"]
                        for item in test["source_evidence"]
                    ]
                    if test["expected_tier"] == "source_only"
                    else []
                ),
            )
            final_answer = (
                (response.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            status = "PASS" if result["passed"] else "FAIL"
            if result["passed"]:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            raw_answer = ""
            final_answer = ""
            retrieved_paths = []
            raw_cited_paths = []
            cited_paths = []
            result = {"passed": False, "error": str(e)}
            status = "ERROR"
            failed += 1

        results.append({
            "id": test["id"],
            "persona": test["persona"],
            "use_case": test["use_case"],
            "question": test["question"],
            "expected_tier": test["expected_tier"],
            "difficulty": test.get("difficulty", ""),
            "notes": test["notes"],
            "reference_answer": test.get("reference_answer"),
            "raw_answer": raw_answer,
            "final_answer": final_answer,
            "retrieved_source_paths": retrieved_paths,
            "raw_cited_source_paths": raw_cited_paths,
            "cited_source_paths": cited_paths,
            **result,
        })
        print(f"  → {status}", file=sys.stderr)
        time.sleep(1)  # small delay between requests

    summary = {
        "question_bank": {
            "name": question_bank["name"],
            "review_status": question_bank["review_status"],
            "corpus": question_bank["corpus"],
        },
        "total": len(test_questions),
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / len(test_questions), 3),
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print()
        print("=" * 60)
        print(
            "  SST Answerer verification: "
            f"{passed}/{len(test_questions)} passed "
            f"({summary['pass_rate']:.0%})"
        )
        print(
            "  Corpus: "
            f"{question_bank['corpus']['documentation_commit']}"
        )
        print("=" * 60)
        for r in results:
            status = "✓" if r["passed"] else "✗"
            terms = r.get("key_terms_matched", [])
            total = r.get("key_terms_total", 0)
            term_info = f"  terms:{len(terms)}/{total}" if total else ""
            print(f"  {status} [{r['expected_tier']:15s}] {r['question'][:50]}{term_info}")
            if not r["passed"]:
                snippet = r.get("response_snippet", r.get("error", ""))
                print(f"    → {snippet}")
        print("=" * 60)
        # Per-tier breakdown
        tiers = {"adequate_docs": [], "source_only": [], "total_gap": []}
        for r in results:
            tiers.setdefault(r["expected_tier"], []).append(r["passed"])
        for tier, outcomes in tiers.items():
            if outcomes:
                n = sum(outcomes)
                print(f"  {tier:17s}: {n}/{len(outcomes)} passed")
        print()

    if summary["pass_rate"] < args.fail_under:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
