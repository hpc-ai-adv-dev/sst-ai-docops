# Copyright Hewlett Packard Enterprise Development LP.
from __future__ import annotations

import fnmatch
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
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


refresh = _load_module(
    "refresh_sst_corpus", ROOT / "scripts" / "refresh_sst_corpus.py"
)
import sst_corpus
import sst_question_bank
import seed_safety

question_bank_audit = _load_module(
    "audit_question_bank",
    ROOT / "scripts" / "audit_question_bank.py",
)


class RepositoryMetadataTests(unittest.TestCase):
    def test_public_github_ssh_remote_is_normalized_to_https(self):
        self.assertEqual(
            sst_corpus.public_repository_url(
                "git@github.com:sstsimulator/sst-core.git"
            ),
            "https://github.com/sstsimulator/sst-core.git",
        )
        self.assertEqual(
            sst_corpus.public_repository_url(
                "ssh://git@github.com/sstsimulator/sst-elements.git"
            ),
            "https://github.com/sstsimulator/sst-elements.git",
        )

    def test_non_github_remote_is_preserved(self):
        remote = "ssh://git@example.test/private/sst-core.git"
        self.assertEqual(sst_corpus.public_repository_url(remote), remote)

    def test_corpus_lock_keeps_only_compact_revision_data(self):
        manifest = _small_manifest()
        manifest["collections"]["documentation"]["files"] *= 100
        manifest["collections"]["documentation"]["file_count"] = 100

        lock = sst_corpus.build_corpus_lock(manifest)

        self.assertEqual(
            lock["corpus_sha256"],
            sst_corpus.corpus_sha256(manifest),
        )
        self.assertEqual(lock["collections"]["documentation"]["file_count"], 100)
        self.assertNotIn("files", lock["collections"]["documentation"])
        self.assertEqual(
            lock["index_format_version"],
            manifest["index_format_version"],
        )

    def test_obsolete_corpus_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sst-corpus-lock.json"
            lock = sst_corpus.build_corpus_lock(_small_manifest())
            del lock["index_format_version"]
            path.write_text(json.dumps(lock))

            with self.assertRaisesRegex(
                ValueError,
                "index_format_version",
            ):
                sst_corpus.load_corpus_lock(path)

    def test_corpus_discovery_uses_only_git_tracked_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            docs = repository / "docs"
            docs.mkdir()
            (docs / "tracked.md").write_text("tracked")
            (docs / "untracked.md").write_text("untracked")
            (docs / "ignored.md").write_text("ignored")
            (repository / ".gitignore").write_text("docs/ignored.md\n")
            subprocess.run(
                ["git", "init", "-q", str(repository)],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "add",
                    ".gitignore",
                    "docs/tracked.md",
                ],
                check=True,
            )

            files = list(
                sst_corpus.tracked_files(
                    repository,
                    docs,
                    sst_corpus.DOC_SUFFIXES,
                )
            )

            self.assertEqual(files, [docs / "tracked.md"])

    def test_question_bank_search_uses_only_git_tracked_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            docs = repository / "docs"
            docs.mkdir()
            (docs / "tracked.md").write_text("tracked")
            (docs / "untracked.md").write_text("untracked")
            subprocess.run(
                ["git", "init", "-q", str(repository)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "add", "docs/tracked.md"],
                check=True,
            )

            files = list(question_bank_audit.searchable_files(docs))

            self.assertEqual(files, [docs.resolve() / "tracked.md"])


class IndexedContentTests(unittest.TestCase):
    def test_mdx_tab_labels_are_repeated_outside_code_blocks(self):
        source = """\
<Tabs>
<TabItem value="16.0" label="16.0 Release">
First rule.
Second rule.

```sh
sst --example
```
</TabItem>
</Tabs>
"""

        indexed = sst_corpus.contextualize_mdx_tabs(source)

        self.assertNotIn("<Tabs", indexed)
        self.assertNotIn("<TabItem", indexed)
        self.assertGreaterEqual(
            indexed.count("Documentation tab: 16.0 Release."),
            4,
        )
        self.assertIn("First rule.", indexed)
        self.assertIn("sst --example", indexed)
        self.assertNotIn(
            "Documentation tab: 16.0 Release. sst --example",
            indexed,
        )

    def test_indexed_content_includes_path_and_tab_context(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "guide.mdx"
            path.write_text(
                '<TabItem value="repo" label="Current repository">\n'
                "Current behavior.\n"
                "</TabItem>\n"
            )

            indexed = sst_corpus.indexed_content(
                path,
                "sst-docs/docs/guide.mdx",
            ).decode()

        self.assertTrue(
            indexed.startswith(
                "Repository path: sst-docs/docs/guide.mdx\n\n"
            )
        )
        self.assertIn(
            "Documentation tab: Current repository.",
            indexed,
        )

    def test_obsolete_index_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = Path(temp) / "manifest.json"
            manifest = _small_manifest()
            manifest["index_format_version"] -= 1
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(
                ValueError,
                "index_format_version",
            ):
                sst_corpus.load_manifest(manifest_path)


class QuestionBankValidationTests(unittest.TestCase):
    def setUp(self):
        self.bank = json.loads(
            (
                ROOT / "benchmarks" / "sst-question-bank.json"
            ).read_text()
        )

    def test_current_question_bank_is_valid(self):
        self.assertIs(
            sst_question_bank.validate_question_bank(self.bank),
            self.bank,
        )

    def test_all_three_repository_revisions_are_required(self):
        del self.bank["corpus"]["sst_elements_commit"]

        with self.assertRaisesRegex(
            ValueError,
            r"corpus\.sst_elements_commit is required",
        ):
            sst_question_bank.validate_question_bank(self.bank)

    def test_duplicate_question_ids_are_rejected(self):
        self.bank["questions"][1]["id"] = self.bank["questions"][0]["id"]

        with self.assertRaisesRegex(ValueError, "Duplicate question id"):
            sst_question_bank.validate_question_bank(self.bank)

    def test_malformed_evidence_is_rejected_with_its_location(self):
        del self.bank["questions"][0]["documentation_evidence"][0]["terms"]

        with self.assertRaisesRegex(
            ValueError,
            r"Question 1 documentation_evidence\[1\]\.terms",
        ):
            sst_question_bank.validate_question_bank(self.bank)

    def test_total_gap_cannot_contain_supporting_evidence(self):
        question = next(
            item
            for item in self.bank["questions"]
            if item["expected_tier"] == "total_gap"
        )
        question["documentation_evidence"] = [
            {"path": "unexpected.md", "terms": ["unexpected"]}
        ]

        with self.assertRaisesRegex(
            ValueError,
            "total_gap cannot contain supporting evidence",
        ):
            sst_question_bank.validate_question_bank(self.bank)


class FakeClient:
    def __init__(
        self,
        root: Path,
        *,
        fail_file: str | None = None,
        crash_after_add: bool = False,
    ):
        self.root = root
        self.fail_file = fail_file
        self.crash_after_add = crash_after_add
        self.deleted: list[str] = []
        self.deleted_files: list[str] = []
        self.uploaded: dict[str, dict] = {}
        self.created = 0
        self.calls: list[tuple[str, str]] = []
        self.file_count = 0
        self.knowledge = [
            {"id": "old-docs", "name": "SST Documentation"},
            {"id": "old-source", "name": "SST Source Code"},
        ]
        self.knowledge_files = {
            "old-docs": ["old-doc-file"],
            "old-source": ["old-source-file"],
        }
        self.uploaded.update(
            {
                "old-doc-file": {
                    "id": "old-doc-file",
                    "filename": "sst-docs__old.md",
                    "meta": {"data": {}},
                    "data": {"status": "completed"},
                },
                "old-source-file": {
                    "id": "old-source-file",
                    "filename": "sst-core__old.cc",
                    "meta": {"data": {}},
                    "data": {"status": "completed"},
                },
            }
        )
        self.source_labels: list[str] = []

    def list_knowledge(self):
        return list(self.knowledge)

    def delete_knowledge(self, knowledge_id):
        self.deleted.append(knowledge_id)
        self.knowledge = [
            item for item in self.knowledge
            if item["id"] != knowledge_id
        ]

    def list_knowledge_files(self, knowledge_id):
        return [
            self.uploaded[file_id]
            for file_id in self.knowledge_files.get(knowledge_id, [])
            if file_id in self.uploaded
        ]

    def search_files(self, filename):
        return [
            record
            for record in self.uploaded.values()
            if fnmatch.fnmatch(record.get("filename", ""), filename)
        ]

    def delete_file(self, file_id):
        self.calls.append(("delete_file", file_id))
        self.deleted_files.append(file_id)
        self.uploaded.pop(file_id, None)
        for file_ids in self.knowledge_files.values():
            if file_id in file_ids:
                file_ids.remove(file_id)

    def create_knowledge(self, name, description):
        self.created += 1
        item = {"id": f"new-{self.created}", "name": name}
        self.knowledge.append(item)
        self.knowledge_files[item["id"]] = []
        return item

    def upload_file(
        self,
        path,
        upload_name,
        source_label,
        *,
        metadata=None,
    ):
        self.file_count += 1
        file_id = f"file-{self.file_count}"
        self.calls.append(("upload", file_id))
        self.source_labels.append(source_label)
        self.uploaded[file_id] = {
            "id": file_id,
            "filename": upload_name,
            "meta": {"data": metadata or {}},
            "data": {
                "status": "failed"
                if upload_name == self.fail_file
                else "completed"
            },
        }
        return {"id": file_id}

    def add_file(self, knowledge_id, file_id):
        self.calls.append(("add", file_id))
        if file_id not in self.knowledge_files[knowledge_id]:
            self.knowledge_files[knowledge_id].append(file_id)
        if self.crash_after_add:
            self.crash_after_add = False
            raise RuntimeError("simulated crash after attachment")
        return {"id": knowledge_id, "file_id": file_id}

    def get_file(self, file_id):
        self.calls.append(("get", file_id))
        return self.uploaded[file_id]


def _small_manifest() -> dict:
    return {
        "schema_version": 1,
        "index_format_version": sst_corpus.INDEX_FORMAT_VERSION,
        "repositories": {
            "sst-docs": {"commit": "a" * 40},
            "sst-core": {"commit": "b" * 40},
            "sst-elements": {"commit": "c" * 40},
        },
        "collections": {
            "documentation": {
                "name": "SST Documentation",
                "description": "docs",
                "file_count": 1,
                "total_bytes": 4,
                "files": [
                    {
                        "repository": "sst-docs",
                        "path": "docs/a.md",
                        "upload_name": "sst-docs__docs__a.md",
                        "bytes": 4,
                        "sha256": "unused",
                    }
                ],
            },
            "source": {
                "name": "SST Source Code",
                "description": "source",
                "file_count": 1,
                "total_bytes": 4,
                "files": [
                    {
                        "repository": "sst-core",
                        "path": "src/a.cc",
                        "upload_name": "sst-core__src__a.cc",
                        "bytes": 4,
                        "sha256": "unused",
                    }
                ],
            },
        },
    }


class IngestionSafetyTests(unittest.TestCase):
    def test_old_collections_are_deleted_only_after_all_files_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")
            state_path = root / "state.json"

            client = FakeClient(root)
            state = refresh.ingest(
                client,
                _small_manifest(),
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
            )

            self.assertEqual(state["status"], "ready_to_finalize")
            self.assertEqual(client.deleted, ["old-docs", "old-source"])
            self.assertEqual(state["progress"]["documentation"]["completed"], 1)
            self.assertEqual(state["progress"]["source"]["completed"], 1)
            self.assertLess(
                client.calls.index(("get", "file-1")),
                client.calls.index(("add", "file-1")),
            )

    def test_failed_indexing_preserves_old_collections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")

            client = FakeClient(root, fail_file="sst-core__src__a.cc")
            with self.assertRaises(refresh.ApiError):
                refresh.ingest(
                    client,
                    _small_manifest(),
                    repos,
                    root / "state.json",
                    file_timeout=1,
                    poll_interval=0,
                )
            self.assertEqual(client.deleted, [])

    def test_bounded_ingest_resumes_without_recreating_collections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            repos["sst-core"].mkdir()
            repos["sst-elements"].mkdir()

            manifest = _small_manifest()
            files = []
            for index in range(4):
                relative = f"docs/{index}.md"
                (repos["sst-docs"] / relative).write_text(f"doc {index}")
                files.append(
                    {
                        "repository": "sst-docs",
                        "path": relative,
                        "upload_name": f"sst-docs__docs__{index}.md",
                        "bytes": 5,
                        "sha256": "unused",
                    }
                )
            manifest["collections"]["documentation"]["files"] = files
            manifest["collections"]["documentation"]["file_count"] = len(files)
            manifest["collections"]["source"]["files"] = []
            manifest["collections"]["source"]["file_count"] = 0

            client = FakeClient(root)
            state = refresh.ingest(
                client,
                manifest,
                repos,
                root / "state.json",
                file_timeout=1,
                poll_interval=0,
                max_files=3,
            )

            self.assertEqual(state["status"], "indexing")
            self.assertEqual(
                state["progress"]["documentation"]["completed"], 3
            )
            self.assertEqual(
                state["corpus_sha256"],
                sst_corpus.corpus_sha256(manifest),
            )
            self.assertEqual(client.created, 2)
            self.assertEqual(client.deleted, [])

            saved_state = json.loads((root / "state.json").read_text())
            del saved_state["corpus_sha256"]
            (root / "state.json").write_text(json.dumps(saved_state))
            state = refresh.ingest(
                client,
                manifest,
                repos,
                root / "state.json",
                file_timeout=1,
                poll_interval=0,
                resume=True,
                max_files=3,
            )

            self.assertEqual(state["status"], "ready_to_finalize")
            self.assertEqual(
                state["progress"]["documentation"]["completed"], 4
            )
            self.assertEqual(
                state["corpus_sha256"],
                sst_corpus.corpus_sha256(manifest),
            )
            self.assertEqual(client.created, 2)
            self.assertEqual(client.deleted, ["old-docs", "old-source"])
            self.assertEqual(
                client.source_labels[0], "sst-docs/docs/0.md"
            )
            for file_id in client.uploaded:
                self.assertLess(
                    client.calls.index(("get", file_id)),
                    client.calls.index(("add", file_id)),
                )

    def test_resume_rejects_a_changed_manifest_at_the_same_commits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")
            state_path = root / "state.json"
            client = FakeClient(root)
            manifest = _small_manifest()

            refresh.ingest(
                client,
                manifest,
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                max_files=1,
            )
            changed_manifest = json.loads(json.dumps(manifest))
            changed_manifest["collections"]["documentation"][
                "description"
            ] = "changed"

            with self.assertRaisesRegex(
                ValueError,
                "exact corpus manifest",
            ):
                refresh.ingest(
                    client,
                    changed_manifest,
                    repos,
                    state_path,
                    file_timeout=1,
                    poll_interval=0,
                    resume=True,
                )

    def test_resume_reconciles_attachment_before_progress_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")
            state_path = root / "state.json"
            client = FakeClient(root, crash_after_add=True)

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                refresh.ingest(
                    client,
                    _small_manifest(),
                    repos,
                    state_path,
                    file_timeout=1,
                    poll_interval=0,
                )

            interrupted = json.loads(state_path.read_text())
            self.assertEqual(interrupted["status"], "indexing")
            self.assertEqual(
                interrupted["progress"]["documentation"]["completed"],
                0,
            )
            self.assertIsNotNone(
                interrupted["progress"]["documentation"]["in_flight"]
            )

            state = refresh.ingest(
                client,
                _small_manifest(),
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                resume=True,
            )

            self.assertEqual(state["status"], "ready_to_finalize")
            self.assertEqual(client.file_count, 2)
            self.assertEqual(
                state["progress"]["documentation"]["completed"],
                1,
            )
            self.assertEqual(state["progress"]["source"]["completed"], 1)

    def test_resume_retries_only_a_failed_in_flight_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")
            state_path = root / "state.json"
            client = FakeClient(
                root,
                fail_file="sst-docs__docs__a.md",
            )

            with self.assertRaisesRegex(refresh.ApiError, "indexing failed"):
                refresh.ingest(
                    client,
                    _small_manifest(),
                    repos,
                    state_path,
                    file_timeout=1,
                    poll_interval=0,
                )

            client.fail_file = None
            state = refresh.ingest(
                client,
                _small_manifest(),
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                resume=True,
            )

            self.assertEqual(state["status"], "ready_to_finalize")
            self.assertEqual(client.file_count, 3)
            self.assertIn("file-1", client.deleted_files)
            self.assertEqual(
                client.knowledge_files[state["new_collections"]["documentation"]["id"]],
                ["file-2"],
            )

    def test_finalized_refresh_can_resume_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / "state.json"
            state = {
                "schema_version": 3,
                "status": "finalized",
                "repositories": _small_manifest()["repositories"],
                "corpus_sha256": sst_corpus.corpus_sha256(
                    _small_manifest()
                ),
            }
            state_path.write_text(json.dumps(state))

            resumed = refresh.ingest(
                FakeClient(root),
                _small_manifest(),
                {},
                state_path,
                file_timeout=1,
                poll_interval=0,
                resume=True,
            )

            self.assertEqual(resumed, state)

    def test_ready_state_rewinds_to_an_exact_older_staged_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            repos["sst-core"].mkdir()
            repos["sst-elements"].mkdir()
            manifest = _small_manifest()
            files = []
            for index in range(4):
                relative = f"docs/{index}.md"
                (repos["sst-docs"] / relative).write_text(f"doc {index}")
                files.append(
                    {
                        "repository": "sst-docs",
                        "path": relative,
                        "upload_name": f"sst-docs__docs__{index}.md",
                        "bytes": 5,
                        "sha256": "unused",
                    }
                )
            manifest["collections"]["documentation"]["files"] = files
            manifest["collections"]["documentation"]["file_count"] = len(files)
            manifest["collections"]["source"]["files"] = []
            manifest["collections"]["source"]["file_count"] = 0
            state_path = root / "state.json"
            client = FakeClient(root)

            refresh.ingest(
                client,
                manifest,
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                max_files=2,
            )
            older_snapshot = deepcopy(client)
            refresh.ingest(
                client,
                manifest,
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                resume=True,
            )
            self.assertEqual(
                json.loads(state_path.read_text())["status"],
                "ready_to_finalize",
            )

            resumed = refresh.ingest(
                older_snapshot,
                manifest,
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                resume=True,
            )

            self.assertEqual(resumed["status"], "ready_to_finalize")
            self.assertEqual(
                resumed["progress"]["documentation"]["completed"],
                4,
            )
            self.assertEqual(older_snapshot.file_count, 4)
            self.assertEqual(
                older_snapshot.deleted,
                ["old-docs", "old-source"],
            )

    def test_ready_state_rejects_a_nonprefix_staged_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            repos["sst-core"].mkdir()
            repos["sst-elements"].mkdir()
            manifest = _small_manifest()
            files = []
            for index in range(2):
                relative = f"docs/{index}.md"
                (repos["sst-docs"] / relative).write_text(f"doc {index}")
                files.append(
                    {
                        "repository": "sst-docs",
                        "path": relative,
                        "upload_name": f"sst-docs__docs__{index}.md",
                        "bytes": 5,
                        "sha256": "unused",
                    }
                )
            manifest["collections"]["documentation"]["files"] = files
            manifest["collections"]["documentation"]["file_count"] = len(files)
            state_path = root / "state.json"
            client = FakeClient(root)
            refresh.ingest(
                client,
                manifest,
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
            )
            state = json.loads(state_path.read_text())
            docs_id = state["new_collections"]["documentation"]["id"]
            first_id = client.knowledge_files[docs_id].pop(0)
            client.delete_file(first_id)

            with self.assertRaisesRegex(ValueError, "exact upload prefix"):
                refresh.ingest(
                    client,
                    manifest,
                    repos,
                    state_path,
                    file_timeout=1,
                    poll_interval=0,
                    resume=True,
                )

    def test_ready_state_replays_old_cleanup_from_an_older_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")
            state_path = root / "state.json"
            client = FakeClient(root)
            state = refresh.ingest(
                client,
                _small_manifest(),
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
            )
            client.knowledge.extend(
                [
                    {"id": "old-docs", "name": "SST Documentation"},
                    {"id": "old-source", "name": "SST Source Code"},
                ]
            )
            client.knowledge_files["old-docs"] = ["old-doc-file"]
            client.knowledge_files["old-source"] = ["old-source-file"]
            client.uploaded.update(
                {
                    "old-doc-file": {
                        "id": "old-doc-file",
                        "filename": "sst-docs__old.md",
                        "meta": {"data": {}},
                        "data": {"status": "completed"},
                    },
                    "old-source-file": {
                        "id": "old-source-file",
                        "filename": "sst-core__old.cc",
                        "meta": {"data": {}},
                        "data": {"status": "completed"},
                    },
                }
            )
            state["old_collections"]["documentation"].update(
                {"files": {"completed": 1}, "deleted": True}
            )
            state["old_collections"]["source"].update(
                {"files": {"completed": 1}, "deleted": True}
            )
            state_path.write_text(json.dumps(state))

            resumed = refresh.ingest(
                client,
                _small_manifest(),
                repos,
                state_path,
                file_timeout=1,
                poll_interval=0,
                resume=True,
            )

            self.assertEqual(resumed["status"], "ready_to_finalize")
            self.assertEqual(
                client.deleted_files[-2:],
                ["old-doc-file", "old-source-file"],
            )
            self.assertEqual(
                client.deleted[-2:],
                ["old-docs", "old-source"],
            )

    def test_existing_orphan_and_abandoned_staging_files_are_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repos = {
                "sst-docs": root / "sst-docs",
                "sst-core": root / "sst-core",
                "sst-elements": root / "sst-elements",
            }
            (repos["sst-docs"] / "docs").mkdir(parents=True)
            (repos["sst-core"] / "src").mkdir(parents=True)
            repos["sst-elements"].mkdir()
            (repos["sst-docs"] / "docs/a.md").write_text("docs")
            (repos["sst-core"] / "src/a.cc").write_text("code")

            client = FakeClient(root)
            client.uploaded["orphan-file"] = {
                "id": "orphan-file",
                "filename": "CMakeLists.txt",
                "meta": {"data": {}},
                "data": {"status": "completed"},
            }
            client.knowledge.append(
                {
                    "id": "abandoned",
                    "name": "SST Documentation [refresh obsolete]",
                }
            )
            client.knowledge_files["abandoned"] = ["abandoned-file"]
            client.uploaded["abandoned-file"] = {
                "id": "abandoned-file",
                "filename": "sst-docs__abandoned.md",
                "meta": {"data": {}},
                "data": {"status": "completed"},
            }

            state = refresh.ingest(
                client,
                _small_manifest(),
                repos,
                root / "state.json",
                file_timeout=1,
                poll_interval=0,
                max_files=1,
            )

            self.assertEqual(state["status"], "indexing")
            self.assertIn("orphan-file", client.deleted_files)
            self.assertIn("abandoned-file", client.deleted_files)
            self.assertIn("abandoned", client.deleted)
            self.assertNotIn("old-docs", client.deleted)

    def test_duplicate_canonical_collections_stop_before_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient(root)
            client.knowledge.append(
                {"id": "duplicate-docs", "name": "SST Documentation"}
            )
            client.knowledge_files["duplicate-docs"] = []

            with self.assertRaisesRegex(
                ValueError,
                "multiple canonical knowledge collections",
            ):
                refresh.ingest(
                    client,
                    _small_manifest(),
                    {},
                    root / "state.json",
                    file_timeout=1,
                    poll_interval=0,
                )

            self.assertEqual(client.deleted_files, [])
            self.assertEqual(client.deleted, [])

    def test_unexpected_collection_stops_before_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeClient(root)
            client.knowledge.append(
                {"id": "private", "name": "Private Notes"}
            )
            client.knowledge_files["private"] = []

            with self.assertRaisesRegex(
                ValueError,
                "unexpected knowledge collections.*Private Notes",
            ):
                refresh.ingest(
                    client,
                    _small_manifest(),
                    {},
                    root / "state.json",
                    file_timeout=1,
                    poll_interval=0,
                )

            self.assertEqual(client.deleted_files, [])
            self.assertEqual(client.deleted, [])


class FinalizationTests(unittest.TestCase):
    def _database(self, path: Path, incomplete: bool = False) -> None:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TABLE knowledge "
                "(id TEXT PRIMARY KEY, user_id TEXT, name TEXT, "
                "description TEXT, meta TEXT, updated_at INTEGER)"
            )
            connection.execute(
                "CREATE TABLE knowledge_file "
                "(id TEXT, user_id TEXT, knowledge_id TEXT, file_id TEXT, "
                "created_at INTEGER, updated_at INTEGER)"
            )
            connection.execute(
                "CREATE TABLE file (id TEXT, filename TEXT, data TEXT)"
            )
            connection.execute(
                "CREATE TABLE model "
                "(id TEXT PRIMARY KEY, meta TEXT, updated_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO model VALUES ('sst-answerer', ?, 0)",
                (json.dumps({"knowledge": [{"id": "old"}]}),),
            )
            for index, (key, name, filename) in enumerate(
                [
                    (
                        "documentation",
                        "SST Documentation [refresh]",
                        "sst-docs__docs__a.md",
                    ),
                    (
                        "source",
                        "SST Source Code [refresh]",
                        "sst-core__src__a.cc",
                    ),
                ],
                1,
            ):
                knowledge_id = f"new-{index}"
                file_id = f"file-{index}"
                connection.execute(
                    "INSERT INTO knowledge VALUES (?, 'user', ?, '', NULL, 0)",
                    (knowledge_id, name),
                )
                connection.execute(
                    "INSERT INTO knowledge_file VALUES (?, 'user', ?, ?, 0, 0)",
                    (f"kf-{index}", knowledge_id, file_id),
                )
                status = "processing" if incomplete and index == 2 else "completed"
                connection.execute(
                    "INSERT INTO file VALUES (?, ?, ?)",
                    (file_id, filename, json.dumps({"status": status})),
                )
            connection.execute(
                "INSERT INTO file VALUES ('orphan-file', 'orphan', ?)",
                (json.dumps({"status": "completed"}),),
            )
            connection.execute(
                "INSERT INTO knowledge_file VALUES "
                "('stale-link', 'user', 'missing-knowledge', "
                "'orphan-file', 0, 0)"
            )

    @staticmethod
    def _state() -> dict:
        return {
            "schema_version": 3,
            "status": "ready_to_finalize",
            "old_collections": {
                "documentation": {
                    "id": "old-1",
                    "deleted": True,
                },
                "source": {
                    "id": "old-2",
                    "deleted": True,
                },
            },
            "new_collections": {
                "documentation": {
                    "id": "new-1",
                    "canonical_name": "SST Documentation",
                    "description": "docs",
                    "expected_files": 1,
                },
                "source": {
                    "id": "new-2",
                    "canonical_name": "SST Source Code",
                    "description": "source",
                    "expected_files": 1,
                },
            },
        }

    def test_finalize_validates_counts_and_relinks_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "webui.db"
            self._database(database)
            result = refresh.finalize_staged_seed(
                database,
                self._state(),
                _small_manifest(),
                root / "sst-corpus-lock.json",
            )
            self.assertEqual(result["collections"]["SST Documentation"], 1)
            self.assertEqual(result["removed_dangling_links"], 1)
            self.assertTrue((root / "sst-corpus-lock.json").is_file())
            with closing(sqlite3.connect(database)) as connection, connection:
                names = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM knowledge ORDER BY name"
                    )
                ]
                meta = json.loads(
                    connection.execute(
                        "SELECT meta FROM model WHERE id='sst-answerer'"
                    ).fetchone()[0]
                )
                stale_links = connection.execute(
                    "SELECT COUNT(*) FROM knowledge_file "
                    "WHERE knowledge_id = 'missing-knowledge'"
                ).fetchone()[0]
            self.assertEqual(
                names, ["SST Documentation", "SST Source Code"]
            )
            self.assertEqual(
                {item["id"] for item in meta["knowledge"]},
                {"new-1", "new-2"},
            )
            self.assertEqual(stale_links, 0)

    def test_finalize_rejects_incomplete_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "webui.db"
            self._database(database, incomplete=True)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                refresh.finalize_staged_seed(
                    database,
                    self._state(),
                    _small_manifest(),
                    root / "sst-corpus-lock.json",
                )
            with closing(sqlite3.connect(database)) as connection, connection:
                names = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM knowledge ORDER BY name"
                    )
                ]
            self.assertIn("SST Source Code [refresh]", names)

    def test_finalize_rejects_the_wrong_staged_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "webui.db"
            self._database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE file SET filename = 'sst-docs__docs__wrong.md' "
                    "WHERE id = 'file-1'"
                )

            with self.assertRaisesRegex(ValueError, "filenames differ"):
                refresh.finalize_staged_seed(
                    database,
                    self._state(),
                    _small_manifest(),
                    root / "sst-corpus-lock.json",
                )

            self.assertFalse((root / "sst-corpus-lock.json").exists())

    def test_finalize_removes_verified_empty_old_canonical_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "webui.db"
            self._database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executemany(
                    "INSERT INTO knowledge VALUES "
                    "(?, 'user', ?, '', NULL, 0)",
                    [
                        ("old-1", "SST Documentation"),
                        ("old-2", "SST Source Code"),
                    ],
                )

            result = refresh.finalize_staged_seed(
                database,
                self._state(),
                _small_manifest(),
                root / "sst-corpus-lock.json",
            )

            self.assertEqual(result["removed_old_collection_rows"], 2)
            with closing(sqlite3.connect(database)) as connection:
                old_rows = connection.execute(
                    "SELECT COUNT(*) FROM knowledge "
                    "WHERE id IN ('old-1', 'old-2')"
                ).fetchone()[0]
            self.assertEqual(old_rows, 0)

    def test_finalize_rejects_an_old_row_not_marked_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "webui.db"
            self._database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO knowledge VALUES "
                    "('old-1', 'user', 'SST Documentation', '', NULL, 0)"
                )
            state = self._state()
            state["old_collections"]["documentation"]["deleted"] = False

            with self.assertRaisesRegex(ValueError, "not marked deleted"):
                refresh.finalize_staged_seed(
                    database,
                    state,
                    _small_manifest(),
                    root / "sst-corpus-lock.json",
                )

            self.assertFalse((root / "sst-corpus-lock.json").exists())

    def test_finalize_rejects_an_old_row_with_surviving_file_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "webui.db"
            self._database(database)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO knowledge VALUES "
                    "('old-1', 'user', 'SST Documentation', '', NULL, 0)"
                )
                connection.execute(
                    "INSERT INTO file VALUES "
                    "('old-file', 'old.md', '{\"status\":\"completed\"}')"
                )
                connection.execute(
                    "INSERT INTO knowledge_file VALUES "
                    "('old-link', 'user', 'old-1', 'old-file', 0, 0)"
                )

            with self.assertRaisesRegex(ValueError, "still has 1 file links"):
                refresh.finalize_staged_seed(
                    database,
                    self._state(),
                    _small_manifest(),
                    root / "sst-corpus-lock.json",
                )

            self.assertFalse((root / "sst-corpus-lock.json").exists())


class SeedIntegrityTests(unittest.TestCase):
    def _seed(self, root: Path) -> tuple[Path, Path, Path, dict]:
        database = root / "webui.db"
        vector_root = root / "vector_db"
        vector_root.mkdir()
        vector_database = vector_root / "chroma.sqlite3"
        uploads = root / "uploads"
        uploads.mkdir()
        lock = sst_corpus.build_corpus_lock(_small_manifest())

        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "CREATE TABLE user "
                "(id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT)"
            )
            connection.execute(
                "INSERT INTO user VALUES "
                "('admin-id', 'Admin', 'admin@localhost', 'admin')"
            )
            connection.execute(
                "CREATE TABLE auth "
                "(id TEXT, email TEXT, password TEXT, active INTEGER)"
            )
            connection.execute(
                "INSERT INTO auth VALUES "
                "('admin-id', 'admin@localhost', 'hash', 1)"
            )
            for table in seed_safety.USER_CONTENT_TABLES:
                connection.execute(
                    f"CREATE TABLE {seed_safety._quote_identifier(table)} "
                    "(id TEXT)"
                )
            connection.execute("CREATE TABLE function (id TEXT)")
            connection.execute(
                "INSERT INTO function VALUES ('confidence_gate')"
            )
            connection.execute("CREATE TABLE model (id TEXT)")
            connection.execute(
                "INSERT INTO model VALUES ('sst-answerer')"
            )
            connection.execute(
                "CREATE TABLE knowledge (id TEXT PRIMARY KEY, name TEXT)"
            )
            connection.execute(
                "CREATE TABLE knowledge_file "
                "(knowledge_id TEXT, file_id TEXT)"
            )
            connection.execute(
                "CREATE TABLE file "
                "(id TEXT PRIMARY KEY, filename TEXT, path TEXT, data TEXT)"
            )
            for knowledge_id, name, file_id, filename in (
                (
                    "docs-id",
                    "SST Documentation",
                    "docs-file",
                    "sst-docs__docs__a.md",
                ),
                (
                    "source-id",
                    "SST Source Code",
                    "source-file",
                    "sst-core__src__a.cc",
                ),
            ):
                stored_name = f"{file_id}_{filename}"
                (uploads / stored_name).write_text("content")
                connection.execute(
                    "INSERT INTO knowledge VALUES (?, ?)",
                    (knowledge_id, name),
                )
                connection.execute(
                    "INSERT INTO file VALUES (?, ?, ?, ?)",
                    (
                        file_id,
                        filename,
                        f"/app/backend/data/uploads/{stored_name}",
                        json.dumps({"status": "completed"}),
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_file VALUES (?, ?)",
                    (knowledge_id, file_id),
                )

        with closing(sqlite3.connect(vector_database)) as connection, connection:
            connection.execute(
                "CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)"
            )
            connection.execute(
                "CREATE TABLE segments "
                "(id TEXT PRIMARY KEY, type TEXT, collection TEXT)"
            )
            connection.execute(
                "CREATE TABLE embeddings (segment_id TEXT)"
            )
            connection.execute(
                "CREATE TABLE embeddings_queue (topic TEXT)"
            )
            connection.executemany(
                "INSERT INTO collections VALUES (?, ?)",
                [
                    ("collection-1", "docs-id"),
                    ("collection-2", "source-id"),
                    ("collection-3", "file-docs-file"),
                    ("collection-4", "file-source-file"),
                ],
            )
            for index in range(1, 5):
                segment_id = f"00000000-0000-0000-0000-{index:012d}"
                connection.execute(
                    "INSERT INTO segments VALUES (?, ?, ?)",
                    (
                        segment_id,
                        seed_safety.PERSISTED_VECTOR_SEGMENT,
                        f"collection-{index}",
                    ),
                )
                (vector_root / segment_id).mkdir()
        return database, vector_database, uploads, lock

    def test_seed_integrity_accepts_exact_corpus(self):
        with tempfile.TemporaryDirectory() as temp:
            database, vector_database, uploads, lock = self._seed(Path(temp))
            self.assertEqual(
                refresh.validate_seed_integrity(
                    database,
                    vector_database,
                    uploads,
                    lock,
                ),
                [],
            )

    def test_seed_safety_does_not_create_missing_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "missing-webui.db"
            vector_database = root / "vector_db" / "missing-chroma.sqlite3"

            errors = seed_safety.seed_safety_errors(
                database,
                vector_database,
            )

            self.assertEqual(len(errors), 2)
            self.assertFalse(database.exists())
            self.assertFalse(vector_database.exists())

    def test_seed_safety_rejects_a_captured_gap_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, _uploads, _lock = self._seed(root)
            (root / "gap_log.jsonl").write_text(
                '{"event":"query","query":"private question"}\n'
            )

            errors = seed_safety.seed_safety_errors(
                database,
                vector_database,
            )

            self.assertIn(
                "seed data contains unexpected entries: gap_log.jsonl",
                errors,
            )

    def test_seed_safety_rejects_extra_functions_and_models(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, _uploads, _lock = self._seed(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO function VALUES ('private_function')"
                )
                connection.execute(
                    "INSERT INTO model VALUES ('test-model')"
                )

            errors = seed_safety.seed_safety_errors(
                database,
                vector_database,
            )

            self.assertTrue(
                any(
                    error.startswith(
                        "function IDs must be exactly confidence_gate;"
                    )
                    for error in errors
                )
            )
            self.assertTrue(
                any(
                    error.startswith(
                        "model IDs must be exactly sst-answerer;"
                    )
                    for error in errors
                )
            )

    def test_seed_integrity_rejects_orphan_corpus_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, uploads, lock = self._seed(root)
            (uploads / "orphan_sst-docs__orphan.md").write_text("orphan")
            (
                uploads / "untracked_sst-docs__untracked.md"
            ).write_text("untracked")
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO knowledge VALUES "
                    "('abandoned-knowledge', "
                    "'SST Documentation [refresh abandoned]')"
                )
                connection.execute(
                    "INSERT INTO file VALUES (?, ?, ?, ?)",
                    (
                        "orphan",
                        "sst-docs__orphan.md",
                        "/app/backend/data/uploads/"
                        "orphan_sst-docs__orphan.md",
                        json.dumps({"status": "completed"}),
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_file VALUES "
                    "('missing-knowledge', 'orphan')"
                )
            with closing(
                sqlite3.connect(vector_database)
            ) as connection, connection:
                connection.execute(
                    "INSERT INTO collections VALUES "
                    "('collection-5', 'file-stale')"
                )
                connection.execute(
                    "INSERT INTO collections VALUES "
                    "('collection-6', 'abandoned-vector')"
                )

            errors = refresh.validate_seed_integrity(
                database,
                vector_database,
                uploads,
                lock,
            )

            self.assertTrue(
                any("dangling relationships" in error for error in errors)
            )
            self.assertTrue(
                any("unattached records" in error for error in errors)
            )
            self.assertTrue(
                any("expected 2 corpus file records" in error for error in errors)
            )
            self.assertTrue(
                any("have no file row" in error for error in errors)
            )
            self.assertTrue(
                any("uploads have no file row" in error for error in errors)
            )
            self.assertTrue(
                any(
                    "unexpected knowledge collections" in error
                    for error in errors
                )
            )
            self.assertTrue(
                any(
                    "unexpected non-file vector collections" in error
                    for error in errors
                )
            )

    def test_seed_integrity_rejects_cross_linked_repository_prefixes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, uploads, lock = self._seed(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE knowledge_file SET knowledge_id = 'source-id' "
                    "WHERE file_id = 'docs-file'"
                )
                connection.execute(
                    "UPDATE knowledge_file SET knowledge_id = 'docs-id' "
                    "WHERE file_id = 'source-file'"
                )

            errors = refresh.validate_seed_integrity(
                database,
                vector_database,
                uploads,
                lock,
            )

            self.assertEqual(
                sum("wrong repository prefix" in error for error in errors),
                2,
            )

    def test_seed_integrity_rejects_private_and_orphan_vector_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, uploads, lock = self._seed(root)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("INSERT INTO chat VALUES ('chat-1')")
                connection.execute(
                    "UPDATE user SET name = ?",
                    ("https://github." + "hpe.com/example",),
                )
            with closing(
                sqlite3.connect(vector_database)
            ) as connection, connection:
                connection.execute(
                    "INSERT INTO embeddings VALUES ('missing-segment')"
                )
                connection.execute(
                    "INSERT INTO embeddings_queue VALUES "
                    "('persistent://default/default/missing-collection')"
                )
            (
                vector_database.parent
                / "11111111-1111-1111-1111-111111111111"
            ).mkdir()

            errors = refresh.validate_seed_integrity(
                database,
                vector_database,
                uploads,
                lock,
            )

            self.assertTrue(any("chat contains 1" in error for error in errors))
            self.assertTrue(
                any("internal GitHub URL" in error for error in errors)
            )
            self.assertTrue(
                any("orphan embedding" in error for error in errors)
            )
            self.assertTrue(any("orphan queue topic" in error for error in errors))
            self.assertTrue(
                any("vector data directory" in error for error in errors)
            )

    def test_compact_removes_only_orphan_segment_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, _uploads, _lock = self._seed(root)
            orphan = (
                vector_database.parent
                / "11111111-1111-1111-1111-111111111111"
            )
            orphan.mkdir()

            result = seed_safety.compact_seed(database, vector_database)

            self.assertEqual(result["removed_vector_directories"], 1)
            self.assertFalse(orphan.exists())
            self.assertEqual(
                seed_safety.seed_safety_errors(database, vector_database),
                [],
            )

    def test_compact_removes_file_vectors_without_file_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, vector_database, _uploads, _lock = self._seed(root)
            persisted_segment = "11111111-1111-1111-1111-111111111111"
            metadata_segment = "22222222-2222-2222-2222-222222222222"
            with closing(
                sqlite3.connect(vector_database)
            ) as connection, connection:
                connection.execute(
                    "INSERT INTO collections VALUES "
                    "('stale-collection', 'file-stale-file')"
                )
                connection.executemany(
                    "INSERT INTO segments VALUES (?, ?, 'stale-collection')",
                    [
                        (
                            persisted_segment,
                            seed_safety.PERSISTED_VECTOR_SEGMENT,
                        ),
                        (metadata_segment, "metadata"),
                    ],
                )
                connection.execute(
                    "INSERT INTO embeddings VALUES (?)",
                    (metadata_segment,),
                )
                connection.execute(
                    "INSERT INTO embeddings_queue VALUES "
                    "('persistent://default/default/stale-collection')"
                )
            (vector_database.parent / persisted_segment).mkdir()

            result = seed_safety.compact_seed(database, vector_database)

            self.assertEqual(result["removed_vector_collections"], 1)
            self.assertFalse(
                (vector_database.parent / persisted_segment).exists()
            )
            with closing(sqlite3.connect(vector_database)) as connection:
                stale_collections = connection.execute(
                    "SELECT COUNT(*) FROM collections "
                    "WHERE id = 'stale-collection'"
                ).fetchone()[0]
                stale_segments = connection.execute(
                    "SELECT COUNT(*) FROM segments "
                    "WHERE collection = 'stale-collection'"
                ).fetchone()[0]
                stale_embeddings = connection.execute(
                    "SELECT COUNT(*) FROM embeddings "
                    "WHERE segment_id = ?",
                    (metadata_segment,),
                ).fetchone()[0]
                stale_topics = connection.execute(
                    "SELECT COUNT(*) FROM embeddings_queue "
                    "WHERE topic LIKE '%/stale-collection'"
                ).fetchone()[0]
            self.assertEqual(
                (
                    stale_collections,
                    stale_segments,
                    stale_embeddings,
                    stale_topics,
                ),
                (0, 0, 0, 0),
            )
            self.assertEqual(
                seed_safety.seed_safety_errors(database, vector_database),
                [],
            )


if __name__ == "__main__":
    unittest.main()
