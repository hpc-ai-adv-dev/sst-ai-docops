# Copyright Hewlett Packard Enterprise Development LP.
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    import pydantic  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **values):
            for cls in reversed(type(self).mro()):
                for key, value in vars(cls).items():
                    if not key.startswith("_") and not callable(value):
                        setattr(self, key, value)
            for key, value in values.items():
                setattr(self, key, value)

    stub.BaseModel = BaseModel
    sys.modules["pydantic"] = stub


gate = _load_module(
    "confidence_gate", ROOT / "seed" / "filters" / "confidence-gate.py"
)
verification = _load_module(
    "run_verification", ROOT / "scripts" / "run_verification.py"
)
sync_seed = _load_module(
    "sync_seed_config", ROOT / "seed" / "sync-seed-config.py"
)


def _request_body(question: str | list[dict]) -> dict:
    return {
        "model": "sst-answerer",
        "messages": [{"role": "user", "content": question}],
    }


def _add_answer(
    body: dict,
    content: str,
    paths: list[str] | None = None,
    scores: list[float] | None = None,
) -> dict:
    message = {"role": "assistant", "content": content}
    if paths is not None:
        message["sources"] = [
            {
                "source": {"name": "SST corpus"},
                "document": [
                    f"retrieved text {index}"
                    for index in range(len(paths))
                ],
                "metadata": [
                    {
                        "source": path,
                        "score": scores[index] if scores else 1.0,
                    }
                    for index, path in enumerate(paths)
                ],
            }
        ]
    body["messages"].append(message)
    return message


class ConfidenceGateTests(unittest.TestCase):
    def setUp(self):
        self.filter = gate.Filter()
        self.filter.valves.gap_log_path = ""
        self.events: list[dict] = []
        self.filter._write_event = self.events.append

    def run_outlet(self, body: dict, **kwargs) -> dict:
        return asyncio.run(
            self.filter.outlet(
                body,
                __user__={"id": "user-1"},
                **kwargs,
            )
        )

    def test_documentation_citation_is_adequate(self):
        body = _request_body("How is this SST feature configured?")
        message = _add_answer(
            body,
            "Use the documented setting [1].",
            ["sst-docs__docs__config.md"],
        )

        self.run_outlet(body)

        self.assertEqual(
            message["content"],
            "Use the documented setting [1].",
        )
        self.assertEqual(
            [event["event"] for event in self.events],
            ["query"],
        )
        self.assertEqual(self.events[0]["tier"], "adequate_docs")

    def test_source_citation_adds_notice_and_records_gap(self):
        body = _request_body("How does this SST API work?")
        body["chat_id"] = "chat-1"
        message = _add_answer(
            body,
            "Call the source-defined API [1].",
            ["sst-core__src__sst__core__api.cc"],
        )

        self.run_outlet(body)

        self.assertTrue(
            message["content"].startswith(gate.SOURCE_ONLY_PREFIX)
        )
        self.assertEqual(
            [event["event"] for event in self.events],
            ["query", "doc_gap_source_only"],
        )
        self.assertEqual(self.events[0]["tier"], "source_only")
        self.assertEqual(self.events[1]["chat_id"], "chat-1")
        self.assertEqual(
            self.events[0]["interaction_id"],
            self.events[1]["interaction_id"],
        )

    def test_custom_corpus_prefixes_can_be_configured(self):
        self.filter.valves.model_ids = "project-answerer"
        self.filter.valves.documentation_prefixes = "project-docs"
        self.filter.valves.source_prefixes = "project-core,project-plugins"
        body = {
            "model": "project-answerer",
            "messages": [
                {"role": "user", "content": "How is this configured?"}
            ],
        }
        message = _add_answer(
            body,
            "Use the project guide [1].",
            ["project-docs__guide.md"],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], "Use the project guide [1].")
        self.assertEqual(self.events[0]["tier"], "adequate_docs")

    def test_overlapping_custom_prefixes_fail_closed(self):
        self.filter.valves.model_ids = "project-answerer"
        self.filter.valves.documentation_prefixes = "project"
        self.filter.valves.source_prefixes = "project-core"
        body = {
            "model": "project-answerer",
            "messages": [
                {"role": "user", "content": "How is this configured?"}
            ],
        }
        message = _add_answer(
            body,
            "Use the implementation [1].",
            ["project-core__implementation.cc"],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_mixed_documentation_and_source_citations_are_documented(self):
        body = _request_body("How does this multi-step SST task work?")
        message = _add_answer(
            body,
            "Start with the guide [1], then use the implementation API [2].",
            [
                "sst-docs__docs__guide.md",
                "sst-elements__src__component.cc",
            ],
        )

        self.run_outlet(body)

        self.assertEqual(
            message["content"],
            "Start with the guide [1], then use the implementation API [2].",
        )
        self.assertEqual(self.events[0]["tier"], "adequate_docs")

    def test_explicit_source_only_answer_with_mixed_citations_is_source_only(
        self,
    ):
        body = _request_body("How does this source-defined SST API work?")
        answer = (
            f"{gate.SOURCE_ONLY_PREFIX}\n\n"
            "The guide gives context [1], and the implementation defines "
            "the API [2]."
        )
        message = _add_answer(
            body,
            answer,
            [
                "sst-docs__docs__guide.md",
                "sst-core__src__sst__core__api.cc",
            ],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], answer)
        self.assertEqual(self.events[0]["tier"], "source_only")

    def test_explicit_source_only_answer_without_source_citation_is_rejected(
        self,
    ):
        body = _request_body("How does this source-defined SST API work?")
        message = _add_answer(
            body,
            f"{gate.SOURCE_ONLY_PREFIX}\n\n"
            "The documentation describes the behavior [1].",
            ["sst-docs__docs__guide.md"],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_only_relevant_source_citations_are_source_only(self):
        body = _request_body("How does this SST API work?")
        message = _add_answer(
            body,
            "Use the nearby guide [1] and source API [2].",
            [
                "sst-docs__docs__unrelated.md",
                "sst-core__src__sst__core__api.cc",
            ],
            [-1.0, 2.0],
        )

        self.run_outlet(body)

        self.assertTrue(
            message["content"].startswith(gate.SOURCE_ONLY_PREFIX)
        )
        self.assertEqual(self.events[0]["tier"], "source_only")

    def test_answer_with_only_low_scoring_citations_is_rejected(self):
        body = _request_body("Is this unsupported SST feature available?")
        message = _add_answer(
            body,
            "A nearby page appears to support this claim [1].",
            ["sst-docs__docs__nearby-topic.md"],
            [-0.1],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_citation_without_native_score_is_rejected(self):
        body = _request_body("Is this unsupported SST feature available?")
        message = _add_answer(
            body,
            "A citation without its reranker score [1].",
            ["sst-docs__docs__nearby-topic.md"],
        )
        del message["sources"][0]["metadata"][0]["score"]

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_duplicate_source_uses_its_best_chunk_score(self):
        body = _request_body("How is this SST feature configured?")
        message = _add_answer(
            body,
            "Use the documented setting [1].",
            [
                "sst-docs__docs__config.md",
                "sst-docs__docs__config.md",
            ],
            [-1.0, 2.0],
        )

        self.run_outlet(body)

        self.assertEqual(
            message["content"],
            "Use the documented setting [1].",
        )
        self.assertEqual(self.events[0]["tier"], "adequate_docs")

    def test_exact_rejection_records_total_gap(self):
        body = _request_body("Is this unsupported behavior available?")
        message = _add_answer(body, gate.EXACT_REJECTION)

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")
        self.assertEqual(self.events[1]["event"], "doc_gap_no_answer")

    def test_legacy_rejection_tag_becomes_exact_rejection(self):
        body = _request_body("Is this unsupported behavior available?")
        message = _add_answer(
            body,
            "A speculative partial answer. [INSUFFICIENT_CONTEXT]",
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_answer_without_citation_becomes_exact_rejection(self):
        body = _request_body("How does this SST feature work?")
        message = _add_answer(
            body,
            "This confident answer has no citation.",
            ["sst-docs__docs__feature.md"],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_unresolved_citation_becomes_exact_rejection(self):
        body = _request_body("How does this SST feature work?")
        message = _add_answer(
            body,
            "This citation does not resolve [2].",
            ["sst-docs__docs__feature.md"],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_unknown_cited_path_becomes_exact_rejection(self):
        body = _request_body("How does this SST feature work?")
        message = _add_answer(
            body,
            "This is not an SST corpus source [1].",
            ["uploaded-notes.txt"],
        )

        self.run_outlet(body)

        self.assertEqual(message["content"], gate.EXACT_REJECTION)
        self.assertEqual(self.events[0]["tier"], "total_gap")

    def test_multimodal_user_text_is_logged(self):
        body = _request_body(
            [
                {"type": "text", "text": "SST checkpointing"},
                {"type": "image_url", "image_url": {"url": "ignored"}},
            ]
        )
        _add_answer(
            body,
            "Use the documented checkpoint behavior [1].",
            ["sst-docs__docs__checkpoint.md"],
        )

        self.run_outlet(body)

        self.assertEqual(self.events[0]["query"], "SST checkpointing")
        self.assertEqual(self.events[0]["tier"], "adequate_docs")

    def test_verification_run_does_not_write_events(self):
        body = _request_body("How is this SST feature configured?")
        body["metadata"] = {"verification_run": True}
        _add_answer(
            body,
            "Use the documented setting [1].",
            ["sst-docs__docs__config.md"],
        )

        self.run_outlet(body)

        self.assertEqual(self.events, [])

    def test_other_model_passes_through_unchanged(self):
        body = {
            "model": "Qwen3-14B-Q4_K_M.gguf",
            "messages": [
                {"role": "user", "content": "What is two plus two?"},
                {"role": "assistant", "content": "Two plus two is four."},
            ],
        }
        original = json.loads(json.dumps(body))

        result = self.run_outlet(
            body,
            __model__={"id": "Qwen3-14B-Q4_K_M.gguf"},
        )

        self.assertEqual(result, original)
        self.assertEqual(self.events, [])

    def test_inlet_is_a_no_op(self):
        body = _request_body("How does SST work?")
        original = json.loads(json.dumps(body))

        result = asyncio.run(self.filter.inlet(body))

        self.assertIs(result, body)
        self.assertEqual(result, original)

    def test_common_source_citation_spellings_are_normalized(self):
        self.assertEqual(
            gate._normalize_citations(
                "Use the guide [source: 1], source [id=2], "
                "and code (source 3)."
            ),
            "Use the guide [1], source [2], and code [3].",
        )

    def test_duplicate_chunks_share_one_citation_number(self):
        body = _request_body(
            "How does the implementation extend the guide?"
        )
        message = _add_answer(
            body,
            "The implementation adds this behavior [2].",
            [
                "sst-docs__docs__guide.md",
                "sst-docs__docs__guide.md",
                "sst-core__src__feature.cc",
            ],
        )

        self.run_outlet(body)

        self.assertTrue(
            message["content"].startswith(gate.SOURCE_ONLY_PREFIX)
        )
        self.assertEqual(self.events[0]["tier"], "source_only")


class VerificationOracleTests(unittest.TestCase):
    @staticmethod
    def response(content: str, sources: list[dict] | None = None) -> dict:
        return {
            "choices": [{"message": {"content": content}}],
            "sources": sources or [],
        }

    def test_answer_missing_reference_terms_fails(self):
        result = verification.evaluate_response(
            self.response("A generic cited answer [1]."),
            "adequate_docs",
            ["SST::Component", "sst-register", "constructor"],
        )
        self.assertFalse(result["passed"])

    def test_vague_gap_language_does_not_pass_total_gap(self):
        result = verification.evaluate_response(
            self.response(
                "This may be a documentation gap, "
                "but try this Kubernetes YAML..."
            ),
            "total_gap",
        )
        self.assertFalse(result["passed"])

    def test_exact_rejection_passes_total_gap(self):
        result = verification.evaluate_response(
            self.response(verification.EXACT_REJECTION),
            "total_gap",
        )
        self.assertTrue(result["passed"])

    def test_unresolved_citation_fails(self):
        result = verification.evaluate_response(
            self.response(
                "Use SST_CONFIG_FILE_PATH and its separator [25]."
            ),
            "adequate_docs",
            ["SST_CONFIG_FILE_PATH", "separator", "~/.sst"],
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["unresolved_citation_ids"], [25])

    def test_resolved_citation_and_two_of_three_terms_pass(self):
        result = verification.evaluate_response(
            self.response(
                "Use SST_CONFIG_FILE_PATH and its separator [1].",
                [
                    {
                        "document": ["retrieved source text"],
                        "metadata": [
                            {
                                "source": (
                                    "sst-core__src__sst__core__env__"
                                    "envquery.cc"
                                )
                            }
                        ],
                    }
                ],
            ),
            "adequate_docs",
            ["SST_CONFIG_FILE_PATH", "separator", "~/.sst"],
        )
        self.assertTrue(result["passed"])
        self.assertTrue(result["has_resolved_citations"])

    def test_expected_evidence_path_must_be_cited(self):
        response = self.response(
            "Use the documented environment setting [1].",
            [
                {
                    "document": ["retrieved documentation"],
                    "metadata": [
                        {
                            "source": (
                                "sst-docs__docs__guides__configuration.md"
                            )
                        }
                    ],
                }
            ],
        )

        matching = verification.evaluate_response(
            response,
            "adequate_docs",
            ["environment setting"],
            expected_evidence_paths=["docs/guides/configuration.md"],
        )
        unrelated = verification.evaluate_response(
            response,
            "adequate_docs",
            ["environment setting"],
            expected_evidence_paths=["docs/guides/other.md"],
        )

        self.assertTrue(matching["passed"])
        self.assertEqual(
            matching["expected_evidence_paths_matched"],
            ["docs/guides/configuration.md"],
        )
        self.assertFalse(unrelated["passed"])
        self.assertFalse(unrelated["has_expected_evidence"])

    def test_source_only_warning_fails_adequate_docs(self):
        result = verification.evaluate_response(
            self.response(
                verification.SOURCE_ONLY_PREFIX
                + "\n\nUse init and sendUntimedData [1].",
                [
                    {
                        "document": ["retrieved source text"],
                        "metadata": [
                            {"source": "sst-core__src__link.cc"}
                        ],
                    }
                ],
            ),
            "adequate_docs",
            ["init", "sendUntimedData"],
        )
        self.assertFalse(result["passed"])

    def test_duplicate_chunks_share_open_webui_citation_numbering(self):
        result = verification.evaluate_response(
            self.response(
                "The source implementation defines this behavior [2].",
                [
                    {
                        "document": [
                            "guide chunk one",
                            "guide chunk two",
                            "source implementation",
                        ],
                        "metadata": [
                            {"source": "sst-docs__docs__guide.md"},
                            {"source": "sst-docs__docs__guide.md"},
                            {"source": "sst-core__src__feature.cc"},
                        ],
                    }
                ],
            ),
            "source_only",
            ["source implementation"],
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["unresolved_citation_ids"], [])


class SeedSynchronizationTests(unittest.TestCase):
    def test_collection_revision_data_is_synchronized_from_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "webui.db"
            lock_path = root / "sst-corpus-lock.json"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    "CREATE TABLE knowledge "
                    "(name TEXT PRIMARY KEY, description TEXT, meta TEXT, "
                    "updated_at INTEGER)"
                )
                for name in ("SST Documentation", "SST Source Code"):
                    conn.execute(
                        "INSERT INTO knowledge VALUES (?, ?, ?, 0)",
                        (name, "Canonical collection.", "{}"),
                    )

            repositories = {
                "sst-core": {
                    "repository": (
                        "https://github.com/sstsimulator/sst-core.git"
                    ),
                    "commit": "a" * 40,
                    "branch": "master",
                }
            }
            lock_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "corpus_sha256": "b" * 64,
                        "repositories": repositories,
                        "collections": {
                            "documentation": {
                                "name": "SST Documentation",
                                "file_count": 10,
                            },
                            "source": {
                                "name": "SST Source Code",
                                "file_count": 20,
                            },
                        },
                    }
                )
            )

            self.assertTrue(
                sync_seed.sync_sst_corpus_metadata(db_path, lock_path)
            )
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute(
                    "SELECT name, description, meta FROM knowledge "
                    "ORDER BY name"
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertIn("sst-core=" + "a" * 40, rows[0][1])
            docs_meta = json.loads(rows[0][2])
            self.assertEqual(
                docs_meta["repositories"]["sst-core"]["repository"],
                "https://github.com/sstsimulator/sst-core.git",
            )
            self.assertEqual(docs_meta["corpus_sha256"], "b" * 64)
            self.assertEqual(docs_meta["file_count"], 10)

    def test_filter_and_model_settings_are_synchronized_without_data_loss(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "webui.db"
            filter_path = Path(temp_dir) / "confidence-gate.py"
            filter_path.write_text("# canonical filter\n")

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    "CREATE TABLE function "
                    "(id TEXT PRIMARY KEY, name TEXT, content TEXT, meta TEXT, "
                    "updated_at INTEGER, is_active INTEGER, is_global INTEGER)"
                )
                conn.execute(
                    "INSERT INTO function VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "confidence_gate",
                        "Legacy Confidence Gate",
                        "# stale\n",
                        json.dumps(
                            {
                                "description": "Legacy workflow labels",
                                "manifest": {"version": 1},
                            }
                        ),
                        0,
                        0,
                        1,
                    ),
                )
                conn.execute(
                    "CREATE TABLE model "
                    "(id TEXT PRIMARY KEY, meta TEXT, params TEXT, "
                    "updated_at INTEGER)"
                )
                conn.execute(
                    "INSERT INTO model VALUES (?, ?, ?, ?)",
                    (
                        "sst-answerer",
                        json.dumps(
                            {
                                "capabilities": {"web_search": True},
                                "builtinTools": {"web_search": True},
                                "filterIds": ["existing-filter"],
                            }
                        ),
                        json.dumps(
                            {
                                "custom_params": {
                                    "keep": "value",
                                    "chat_template_kwargs": {
                                        "existing": 7
                                    },
                                }
                            }
                        ),
                        0,
                    ),
                )
                conn.execute(
                    "CREATE TABLE knowledge "
                    "(id TEXT PRIMARY KEY, user_id TEXT, name TEXT, "
                    "description TEXT, meta TEXT)"
                )
                conn.executemany(
                    "INSERT INTO knowledge VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            "docs-id",
                            "user-1",
                            "SST Documentation",
                            "Current docs",
                            json.dumps({"commit": "docs"}),
                        ),
                        (
                            "code-id",
                            "user-1",
                            "SST Source Code",
                            "Current source",
                            json.dumps({"commit": "source"}),
                        ),
                    ],
                )

            self.assertTrue(
                sync_seed.sync_filter(db_path, filter_path)
            )
            self.assertTrue(sync_seed.constrain_sst_model(db_path))
            self.assertTrue(sync_seed.sync_sst_knowledge(db_path))

            with closing(sqlite3.connect(db_path)) as conn, conn:
                function_row = conn.execute(
                    "SELECT name, content, meta, is_active, is_global "
                    "FROM function WHERE id='confidence_gate'"
                ).fetchone()
                meta = json.loads(
                    conn.execute(
                        "SELECT meta FROM model WHERE id='sst-answerer'"
                    ).fetchone()[0]
                )
                params = json.loads(
                    conn.execute(
                        "SELECT params FROM model WHERE id='sst-answerer'"
                    ).fetchone()[0]
                )

            self.assertEqual(
                function_row[0],
                "SST Answer Outcome Tracker",
            )
            self.assertEqual(function_row[1], "# canonical filter\n")
            function_meta = json.loads(function_row[2])
            self.assertIn("Gap Tracker", function_meta["description"])
            self.assertEqual(
                function_meta["manifest"],
                {"version": 1},
            )
            self.assertEqual(function_row[3], 1)
            self.assertEqual(function_row[4], 0)
            self.assertFalse(meta["capabilities"]["web_search"])
            self.assertTrue(meta["capabilities"]["citations"])
            self.assertFalse(meta["builtinTools"]["code_interpreter"])
            self.assertEqual(len(meta["suggestion_prompts"]), 4)
            self.assertEqual(
                meta["filterIds"],
                ["existing-filter", "confidence_gate"],
            )
            self.assertEqual(params["temperature"], 0)
            self.assertEqual(params["max_tokens"], 700)
            self.assertEqual(
                params["custom_params"]["keep"],
                "value",
            )
            self.assertEqual(
                params["custom_params"]["chat_template_kwargs"][
                    "existing"
                ],
                7,
            )
            self.assertFalse(
                params["custom_params"]["chat_template_kwargs"][
                    "enable_thinking"
                ]
            )
            self.assertEqual(
                [item["id"] for item in meta["knowledge"]],
                ["docs-id", "code-id"],
            )


if __name__ == "__main__":
    unittest.main()
