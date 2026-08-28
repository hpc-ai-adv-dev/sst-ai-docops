#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Plan, ingest, and finalize a fail-safe SST Open WebUI corpus refresh."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
import time
from contextlib import closing
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from sst_corpus import (
    build_manifest,
    corpus_sha256,
    default_repositories,
    indexed_content,
    load_corpus_lock,
    load_manifest,
    summary,
    validate_manifest_sources,
    write_corpus_lock,
    write_manifest,
)
from sst_question_bank import load_question_bank
from seed_safety import compact_seed, seed_safety_errors


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BANK = ROOT / "benchmarks" / "sst-question-bank.json"
DEFAULT_MANIFEST = (
    ROOT / "seed" / ".sst-refresh-work" / "sst-corpus-manifest.json"
)
ENV_FILE = ROOT / "env" / "webui-common.env"
COMPLETED_STATUSES = {"completed", "processed", "success"}
FAILED_STATUSES = {"failed", "error"}


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _env_file_value(name: str) -> str:
    if not ENV_FILE.is_file():
        return ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    return ""


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode()


class OpenWebUIClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 120,
        retries: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
    ):
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    time.sleep(min(2**attempt, 10))
                    continue
                try:
                    detail = json.loads(body).get("detail", body)
                except json.JSONDecodeError:
                    detail = body
                raise ApiError(
                    f"{method} {path} returned HTTP {exc.code}: {detail}",
                    status_code=exc.code,
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 10))
                    continue
                raise ApiError(f"{method} {path} failed: {exc}") from exc
        raise AssertionError("unreachable")

    def authenticate(
        self, email: str, password: str, *, allow_signup: bool = False
    ) -> None:
        try:
            response = self._request(
                "POST",
                "/api/v1/auths/signin",
                data=_json_bytes({"email": email, "password": password}),
            )
        except ApiError:
            if not allow_signup:
                raise
            response = self._request(
                "POST",
                "/api/v1/auths/signup",
                data=_json_bytes(
                    {
                        "name": "Admin",
                        "email": email,
                        "password": password,
                        "profile_image_url": "",
                    }
                ),
            )
        token = response.get("token")
        if not token:
            raise ApiError("Authentication response did not contain a token")
        self.token = token

    def list_knowledge(self) -> list[dict]:
        knowledge: list[dict] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/api/v1/knowledge/?page={page}",
            )
            if isinstance(response, list):
                return response
            items = next(
                (
                    response[key]
                    for key in ("items", "data", "knowledge")
                    if isinstance(response.get(key), list)
                ),
                None,
            )
            if items is None:
                raise ApiError(
                    "Knowledge-list response has an unsupported shape"
                )
            knowledge.extend(items)
            total = response.get("total")
            if not items or (
                isinstance(total, int) and len(knowledge) >= total
            ):
                return knowledge
            page += 1

    def create_knowledge(self, name: str, description: str) -> dict:
        response = self._request(
            "POST",
            "/api/v1/knowledge/create",
            data=_json_bytes(
                {
                    "name": name,
                    "description": description,
                    "access_control": None,
                }
            ),
        )
        if not response.get("id"):
            raise ApiError(f"Knowledge creation failed for {name}: {response}")
        return response

    def delete_knowledge(self, knowledge_id: str) -> None:
        encoded = urllib.parse.quote(knowledge_id, safe="")
        try:
            self._request("DELETE", f"/api/v1/knowledge/{encoded}/delete")
        except ApiError as exc:
            if exc.status_code == 404:
                return
            if exc.status_code == 400 and "not found" in str(exc).lower():
                return
            raise

    def list_knowledge_files(self, knowledge_id: str) -> list[dict]:
        encoded = urllib.parse.quote(knowledge_id, safe="")
        files: list[dict] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/api/v1/knowledge/{encoded}/files?page={page}",
            )
            items = response.get("items")
            if not isinstance(items, list):
                raise ApiError(
                    "Knowledge-file response has an unsupported shape"
                )
            files.extend(items)
            total = response.get("total")
            if not items or (
                isinstance(total, int) and len(files) >= total
            ):
                return files
            page += 1

    def search_files(self, filename: str) -> list[dict]:
        files: list[dict] = []
        skip = 0
        limit = 1000
        encoded = urllib.parse.urlencode(
            {
                "filename": filename,
                "content": "false",
                "skip": skip,
                "limit": limit,
            }
        )
        while True:
            try:
                response = self._request(
                    "GET",
                    f"/api/v1/files/search?{encoded}",
                )
            except ApiError as exc:
                if exc.status_code == 404:
                    return files
                raise
            if not isinstance(response, list):
                raise ApiError("File-search response has an unsupported shape")
            files.extend(response)
            if len(response) < limit:
                return files
            skip += len(response)
            encoded = urllib.parse.urlencode(
                {
                    "filename": filename,
                    "content": "false",
                    "skip": skip,
                    "limit": limit,
                }
            )

    def delete_file(self, file_id: str) -> None:
        encoded = urllib.parse.quote(file_id, safe="")
        try:
            self._request("DELETE", f"/api/v1/files/{encoded}")
        except ApiError as exc:
            if exc.status_code != 404:
                raise

    def upload_file(
        self,
        path: Path,
        upload_name: str,
        source_label: str,
        *,
        metadata: dict | None = None,
    ) -> dict:
        boundary = f"----aidocops-{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(upload_name)[0] or "text/plain"
        parts: list[bytes] = []
        if metadata:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="metadata"\r\n'
                    "Content-Type: application/json\r\n\r\n"
                    f"{json.dumps(metadata)}\r\n"
                ).encode()
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{upload_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        parts.append(indexed_content(path, source_label))
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        response = self._request(
            "POST",
            "/api/v1/files/?process=true",
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if not response.get("id"):
            raise ApiError(f"Upload failed for {upload_name}: {response}")
        return response

    def add_file(self, knowledge_id: str, file_id: str) -> dict:
        knowledge = urllib.parse.quote(knowledge_id, safe="")
        return self._request(
            "POST",
            f"/api/v1/knowledge/{knowledge}/file/add",
            data=_json_bytes({"file_id": file_id}),
        )

    def get_file(self, file_id: str) -> dict:
        encoded = urllib.parse.quote(file_id, safe="")
        return self._request("GET", f"/api/v1/files/{encoded}")


def file_status(file_record: dict) -> str:
    data = file_record.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return str(data.get("status") or file_record.get("status") or "").lower()


def wait_for_file(
    client: OpenWebUIClient,
    file_id: str,
    *,
    timeout: float,
    poll_interval: float,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        record = client.get_file(file_id)
        status = file_status(record)
        if status in COMPLETED_STATUSES:
            return record
        if status in FAILED_STATUSES:
            raise ApiError(f"Open WebUI indexing failed for file {file_id}: {record}")
        if time.monotonic() >= deadline:
            raise ApiError(
                f"Timed out after {timeout:g}s waiting for file {file_id}; "
                f"last status was {status or 'unknown'}"
            )
        time.sleep(poll_interval)


def bank_manifest_errors(bank: dict, manifest: dict) -> list[str]:
    bank_corpus = bank["corpus"]
    expected = {
        "sst-docs": bank_corpus["documentation_commit"],
        "sst-core": bank_corpus["sst_core_commit"],
        "sst-elements": bank_corpus["sst_elements_commit"],
    }
    errors = []
    for name, commit in expected.items():
        actual = manifest["repositories"][name]["commit"]
        if actual != commit:
            errors.append(
                f"question bank pins {name} {commit}, but manifest uses {actual}"
            )
    if bank.get("review_status") != "validated_against_target_corpus":
        errors.append(
            "question bank review_status must be validated_against_target_corpus"
        )
    return errors


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def _file_refresh_metadata(file_record: dict) -> dict:
    meta = file_record.get("meta")
    if not isinstance(meta, dict):
        return {}
    data = meta.get("data")
    return data if isinstance(data, dict) else {}


def _delete_files(
    client: OpenWebUIClient,
    file_ids: list[str],
    progress: dict,
    state_path: Path,
    state: dict,
) -> None:
    completed = int(progress.get("completed", 0))
    for file_id in file_ids[completed:]:
        client.delete_file(file_id)
        completed += 1
        progress["completed"] = completed
        _write_state(state_path, state)


def _reconcile_in_flight(
    client: OpenWebUIClient,
    state: dict,
    state_path: Path,
    key: str,
    collection: dict,
    *,
    file_timeout: float,
    poll_interval: float,
) -> None:
    progress = state["progress"][key]
    in_flight = progress.get("in_flight")
    if not isinstance(in_flight, dict):
        return

    index = int(in_flight["index"])
    completed = int(progress["completed"])
    if index != completed or index >= collection["file_count"]:
        raise ValueError(
            f"invalid in-flight state for {collection['name']}: "
            f"index {index}, completed {completed}"
        )

    item = collection["files"][index]
    item_key = f"{key}:{index}"
    candidates = [
        file_record
        for file_record in client.search_files(item["upload_name"])
        if _file_refresh_metadata(file_record).get("refresh_id")
        == state["refresh_id"]
        and _file_refresh_metadata(file_record).get("corpus_item")
        == item_key
    ]
    failed_candidates = [
        file_record
        for file_record in candidates
        if file_status(file_record) in FAILED_STATUSES
    ]
    for failed in failed_candidates:
        failed_id = failed.get("id")
        if failed_id:
            client.delete_file(failed_id)
    candidates = [
        file_record
        for file_record in candidates
        if file_record not in failed_candidates
    ]
    if not candidates:
        progress["in_flight"] = None
        _write_state(state_path, state)
        return

    attached_ids = {
        file_record["id"]
        for file_record in client.list_knowledge_files(
            state["new_collections"][key]["id"]
        )
    }
    attached = [
        file_record
        for file_record in candidates
        if file_record.get("id") in attached_ids
    ]
    completed_candidates = [
        file_record
        for file_record in candidates
        if file_status(file_record) in COMPLETED_STATUSES
    ]
    selected = (attached or completed_candidates or candidates)[0]

    selected_id = selected["id"]
    for duplicate in candidates:
        duplicate_id = duplicate.get("id")
        if duplicate_id and duplicate_id != selected_id:
            client.delete_file(duplicate_id)

    if selected_id not in attached_ids:
        wait_for_file(
            client,
            selected_id,
            timeout=file_timeout,
            poll_interval=poll_interval,
        )
        client.add_file(state["new_collections"][key]["id"], selected_id)

    progress.update(
        {
            "completed": completed + 1,
            "last_upload": item["upload_name"],
            "in_flight": None,
        }
    )
    _write_state(state_path, state)


def _new_refresh_state(
    client: OpenWebUIClient,
    manifest: dict,
    staging_names: dict[str, str],
) -> dict:
    existing = client.list_knowledge()
    attached_ids: set[str] = set()
    files_by_knowledge: dict[str, list[str]] = {}
    for knowledge in existing:
        knowledge_id = knowledge.get("id")
        if not knowledge_id:
            continue
        file_ids = [
            file_record["id"]
            for file_record in client.list_knowledge_files(knowledge_id)
            if file_record.get("id")
        ]
        files_by_knowledge[knowledge_id] = file_ids
        attached_ids.update(file_ids)

    all_files = {
        file_record["id"]: file_record
        for file_record in client.search_files("*")
        if file_record.get("id")
    }

    abandoned_staging = [
        knowledge
        for knowledge in existing
        if any(
            str(knowledge.get("name") or "").startswith(
                f"{collection['name']} [refresh "
            )
            for collection in manifest["collections"].values()
        )
    ]
    abandoned_ids = {
        knowledge["id"]
        for knowledge in abandoned_staging
        if knowledge.get("id")
    }
    canonical_names = {
        item["name"] for item in manifest["collections"].values()
    }
    unexpected_collections = [
        str(knowledge.get("name") or knowledge.get("id") or "unnamed")
        for knowledge in existing
        if knowledge.get("id") not in abandoned_ids
        and knowledge.get("name") not in canonical_names
    ]
    if unexpected_collections:
        raise ValueError(
            "unexpected knowledge collections in the maintainer seed: "
            + ", ".join(sorted(unexpected_collections))
        )
    cleanup_file_ids = sorted(
        (set(all_files) - attached_ids)
        | {
            file_id
            for knowledge_id in abandoned_ids
            for file_id in files_by_knowledge.get(knowledge_id, [])
        }
    )

    canonical_matches: dict[str, list[dict]] = {
        name: [
            collection
            for collection in existing
            if collection.get("name") == name
        ]
        for name in canonical_names
    }
    duplicate_names = [
        name
        for name, matches in canonical_matches.items()
        if len(matches) > 1
    ]
    if duplicate_names:
        raise ValueError(
            "multiple canonical knowledge collections exist: "
            + ", ".join(sorted(duplicate_names))
        )
    canonical = {
        name: matches[0]
        for name, matches in canonical_matches.items()
        if matches
    }
    return {
        "schema_version": 3,
        "refresh_id": uuid.uuid4().hex,
        "corpus_sha256": corpus_sha256(manifest),
        "status": "cleaning_existing",
        "repositories": manifest["repositories"],
        "staging_names": staging_names,
        "existing_cleanup": {
            "file_ids": cleanup_file_ids,
            "files": {"completed": 0},
            "collection_ids": sorted(abandoned_ids),
            "collections": {"completed": 0},
        },
        "old_collections": {
            key: {
                "id": canonical.get(collection["name"], {}).get("id"),
                "file_ids": files_by_knowledge.get(
                    canonical.get(collection["name"], {}).get("id"),
                    [],
                ),
                "files": {"completed": 0},
                "deleted": False,
            }
            for key, collection in manifest["collections"].items()
        },
        "new_collections": {},
        "progress": {},
    }


def _reconcile_ready_state(
    client: OpenWebUIClient,
    state: dict,
    manifest: dict,
    state_path: Path,
) -> dict:
    existing = {
        item.get("id"): item
        for item in client.list_knowledge()
        if item.get("id")
    }
    attached_ids: set[str] = set()
    all_complete = True
    for key, details in state["new_collections"].items():
        knowledge_id = details["id"]
        collection = existing.get(knowledge_id)
        if collection is None:
            raise ValueError(
                f"saved staged collection is missing: {knowledge_id}"
            )
        if collection.get("name") != details["staging_name"]:
            raise ValueError(
                f"saved staged collection has an unexpected name: "
                f"{knowledge_id}"
            )
        records = client.list_knowledge_files(knowledge_id)
        file_ids = [record.get("id") for record in records]
        filenames = [record.get("filename") for record in records]
        if any(not file_id for file_id in file_ids) or any(
            not filename for filename in filenames
        ):
            raise ValueError(
                f"saved staged collection has incomplete file records: "
                f"{knowledge_id}"
            )
        if len(set(file_ids)) != len(file_ids) or len(set(filenames)) != len(
            filenames
        ):
            raise ValueError(
                f"saved staged collection has duplicate files: {knowledge_id}"
            )
        expected = [
            item["upload_name"]
            for item in manifest["collections"][key]["files"]
        ]
        completed = len(filenames)
        if completed > len(expected) or set(filenames) != set(
            expected[:completed]
        ):
            raise ValueError(
                f"saved staged collection is not an exact upload prefix: "
                f"{knowledge_id}"
            )
        attached_ids.update(file_ids)
        state["progress"][key].update(
            {
                "completed": completed,
                "last_upload": expected[completed - 1] if completed else None,
                "in_flight": None,
            }
        )
        all_complete = all_complete and completed == len(expected)

    all_files = {
        record["id"]: record
        for record in client.search_files("*")
        if record.get("id")
    }
    unattached_refresh_files = {
        file_id
        for file_id, record in all_files.items()
        if _file_refresh_metadata(record).get("refresh_id")
        == state["refresh_id"]
        and file_id not in attached_ids
    }
    if unattached_refresh_files:
        raise ValueError(
            "saved staged data contains unattached files from this refresh"
        )

    cleanup = state["existing_cleanup"]
    surviving_cleanup_files = set(cleanup["file_ids"]) & set(all_files)
    surviving_cleanup_collections = set(cleanup["collection_ids"]) & set(
        existing
    )
    if surviving_cleanup_files or surviving_cleanup_collections:
        raise ValueError(
            "saved staged data predates completion of its initial cleanup"
        )

    old_files_survive = False
    old_collections_survive = False
    for key, details in state["old_collections"].items():
        old_id = details.get("id")
        if not old_id:
            continue
        old_collection = existing.get(old_id)
        canonical_name = manifest["collections"][key]["name"]
        if old_collection is None:
            surviving_old_files = set(details["file_ids"]) & set(all_files)
            if surviving_old_files:
                raise ValueError(
                    f"{canonical_name} files survived without their collection"
                )
            details["files"]["completed"] = len(details["file_ids"])
            details["deleted"] = True
            continue
        if old_collection.get("name") != canonical_name:
            raise ValueError(
                f"saved old collection has an unexpected name: {old_id}"
            )
        surviving_old_files = {
            record.get("id")
            for record in client.list_knowledge_files(old_id)
            if record.get("id")
        }
        stored_old_files = set(details["file_ids"]) & set(all_files)
        if surviving_old_files != stored_old_files:
            raise ValueError(
                f"{canonical_name} has unattached old file records"
            )
        completed = len(details["file_ids"]) - len(surviving_old_files)
        if surviving_old_files != set(details["file_ids"][completed:]):
            raise ValueError(
                f"{canonical_name} cleanup is not an exact deletion prefix"
            )
        if not all_complete and completed:
            raise ValueError(
                f"cannot resume because {canonical_name} is not intact"
            )
        details["files"]["completed"] = completed
        details["deleted"] = False
        old_files_survive = old_files_survive or bool(surviving_old_files)
        old_collections_survive = True

    if not all_complete:
        state["status"] = "indexing"
    elif old_files_survive:
        state["status"] = "cleaning_old_files"
    elif old_collections_survive:
        state["status"] = "removing_old_collections"
    else:
        state["status"] = "ready_to_finalize"
    _write_state(state_path, state)
    return state


def ingest(
    client: OpenWebUIClient,
    manifest: dict,
    repositories: dict[str, Path],
    state_path: Path,
    *,
    file_timeout: float,
    poll_interval: float,
    resume: bool = False,
    max_files: int | None = None,
) -> dict:
    if max_files is not None and max_files < 1:
        raise ValueError("max_files must be at least 1")

    commits = manifest["repositories"]
    suffix = f"{commits['sst-docs']['commit'][:8]}-{commits['sst-core']['commit'][:8]}"
    staging_names = {
        key: f"{collection['name']} [refresh {suffix}]"
        for key, collection in manifest["collections"].items()
    }
    manifest_hash = corpus_sha256(manifest)

    if resume and state_path.is_file():
        state = json.loads(state_path.read_text())
        if state.get("schema_version") != 3:
            raise ValueError(
                "cannot resume an obsolete refresh state; restart the staged "
                "refresh"
            )
        if state.get("status") not in {
            "cleaning_existing",
            "creating_collections",
            "indexing",
            "cleaning_old_files",
            "removing_old_collections",
            "ready_to_finalize",
            "finalized",
        }:
            raise ValueError(
                f"cannot resume refresh in state {state.get('status')}"
            )
        if state.get("repositories") != commits:
            raise ValueError("resume state does not match the corpus manifest")
        saved_hash = state.get("corpus_sha256")
        if saved_hash and saved_hash != manifest_hash:
            raise ValueError(
                "resume state does not match the exact corpus manifest"
            )
        if not saved_hash and state.get("status") == "finalized":
            raise ValueError(
                "finalized resume state has no exact corpus manifest hash"
            )
        if not saved_hash:
            for key, progress in state.get("progress", {}).items():
                collection = manifest["collections"].get(key)
                if collection is None:
                    raise ValueError(
                        "resume state contains an unknown corpus collection"
                    )
                completed = int(progress.get("completed", 0))
                if completed < 0 or completed > collection["file_count"]:
                    raise ValueError(
                        "resume progress does not match the corpus manifest"
                    )
                expected_last = (
                    collection["files"][completed - 1]["upload_name"]
                    if completed
                    else None
                )
                if progress.get("last_upload") != expected_last:
                    raise ValueError(
                        "resume progress does not match the corpus manifest"
                    )
                in_flight = progress.get("in_flight")
                if isinstance(in_flight, dict):
                    index = int(in_flight.get("index", -1))
                    if (
                        index != completed
                        or index >= collection["file_count"]
                        or in_flight.get("upload_name")
                        != collection["files"][index]["upload_name"]
                    ):
                        raise ValueError(
                            "in-flight upload does not match the corpus manifest"
                        )
                details = state.get("new_collections", {}).get(key)
                if (
                    isinstance(details, dict)
                    and details.get("expected_files")
                    != collection["file_count"]
                ):
                    raise ValueError(
                        "staged collection does not match the corpus manifest"
                    )
            state["corpus_sha256"] = manifest_hash
            _write_state(state_path, state)
        if state.get("status") == "finalized":
            return state
        if state.get("status") == "ready_to_finalize":
            state = _reconcile_ready_state(
                client,
                state,
                manifest,
                state_path,
            )
            if state.get("status") == "ready_to_finalize":
                return state
    else:
        state = _new_refresh_state(client, manifest, staging_names)
        _write_state(state_path, state)

    if state["status"] == "cleaning_existing":
        cleanup = state["existing_cleanup"]
        _delete_files(
            client,
            cleanup["file_ids"],
            cleanup["files"],
            state_path,
            state,
        )
        completed = int(cleanup["collections"].get("completed", 0))
        for knowledge_id in cleanup["collection_ids"][completed:]:
            client.delete_knowledge(knowledge_id)
            completed += 1
            cleanup["collections"]["completed"] = completed
            _write_state(state_path, state)
        state["status"] = "creating_collections"
        _write_state(state_path, state)

    if state["status"] == "creating_collections":
        for key, collection in manifest["collections"].items():
            if key in state["new_collections"]:
                continue
            description = (
                f"{collection['description']} Corpus commits: "
                f"sst-docs={commits['sst-docs']['commit']}, "
                f"sst-core={commits['sst-core']['commit']}, "
                f"sst-elements={commits['sst-elements']['commit']}."
            )
            matches = [
                knowledge
                for knowledge in client.list_knowledge()
                if knowledge.get("name") == staging_names[key]
            ]
            if len(matches) > 1:
                raise ValueError(
                    f"multiple staged collections named {staging_names[key]}"
                )
            created = (
                matches[0]
                if matches
                else client.create_knowledge(staging_names[key], description)
            )
            state["new_collections"][key] = {
                "id": created["id"],
                "staging_name": staging_names[key],
                "canonical_name": collection["name"],
                "description": description,
                "expected_files": collection["file_count"],
            }
            state["progress"][key] = {
                "completed": 0,
                "last_upload": None,
                "in_flight": None,
            }
            _write_state(state_path, state)

        state["status"] = "indexing"
        _write_state(state_path, state)

    existing_ids = {
        item.get("id") for item in client.list_knowledge()
    }
    missing = [
        details["id"]
        for details in state["new_collections"].values()
        if details["id"] not in existing_ids
    ]
    if missing:
        raise ValueError(
            "cannot continue because staged collections are missing: "
            + ", ".join(missing)
        )

    indexed_this_run = 0
    if state["status"] == "indexing":
        for key, collection in manifest["collections"].items():
            _reconcile_in_flight(
                client,
                state,
                state_path,
                key,
                collection,
                file_timeout=file_timeout,
                poll_interval=poll_interval,
            )
            knowledge_id = state["new_collections"][key]["id"]
            total = collection["file_count"]
            completed = int(state["progress"][key]["completed"])
            for index, item in enumerate(
                collection["files"][completed:],
                start=completed,
            ):
                if (
                    max_files is not None
                    and indexed_this_run >= max_files
                ):
                    _write_state(state_path, state)
                    return state
                source = repositories[item["repository"]] / item["path"]
                source_label = f"{item['repository']}/{item['path']}"
                item_key = f"{key}:{index}"
                state["progress"][key]["in_flight"] = {
                    "index": index,
                    "upload_name": item["upload_name"],
                    "file_id": None,
                }
                _write_state(state_path, state)
                uploaded = client.upload_file(
                    source,
                    item["upload_name"],
                    source_label,
                    metadata={
                        "refresh_id": state["refresh_id"],
                        "corpus_item": item_key,
                        "repository_path": source_label,
                    },
                )
                state["progress"][key]["in_flight"]["file_id"] = uploaded["id"]
                _write_state(state_path, state)
                # Current Open WebUI requires extraction to populate file.data
                # before a file can be attached to a knowledge collection.
                wait_for_file(
                    client,
                    uploaded["id"],
                    timeout=file_timeout,
                    poll_interval=poll_interval,
                )
                # The add endpoint synchronously indexes the extracted content
                # into the target knowledge collection before returning.
                client.add_file(knowledge_id, uploaded["id"])
                completed += 1
                indexed_this_run += 1
                state["progress"][key].update(
                    {
                        "completed": completed,
                        "last_upload": item["upload_name"],
                        "in_flight": None,
                    }
                )
                _write_state(state_path, state)
                if completed % 10 == 0 or completed == total:
                    print(
                        f"[{collection['name']}] {completed}/{total} "
                        f"{item['path']}",
                        file=sys.stderr,
                    )

        state["status"] = "cleaning_old_files"
        _write_state(state_path, state)

    if state["status"] == "cleaning_old_files":
        for details in state["old_collections"].values():
            _delete_files(
                client,
                details["file_ids"],
                details["files"],
                state_path,
                state,
            )
        state["status"] = "removing_old_collections"
        _write_state(state_path, state)

    if state["status"] == "removing_old_collections":
        for details in state["old_collections"].values():
            old_id = details.get("id")
            if old_id and not details.get("deleted"):
                client.delete_knowledge(old_id)
                details["deleted"] = True
                _write_state(state_path, state)

    state["status"] = "ready_to_finalize"
    _write_state(state_path, state)
    return state


def _decode_json(value: str | dict | None) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(value)


def finalize_staged_seed(
    database: Path,
    state: dict,
    manifest: dict,
    lock_destination: Path,
) -> dict:
    if state.get("status") not in {"ready_to_finalize", "finalized"}:
        raise ValueError(
            f"Refresh state is {state.get('status')}, not ready_to_finalize"
        )

    collection_results = {}
    removed_dangling_links = 0
    removed_old_collection_rows = 0
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        removed_dangling_links = connection.execute(
            "SELECT COUNT(*) FROM knowledge_file "
            "WHERE knowledge_id NOT IN (SELECT id FROM knowledge) "
            "OR file_id NOT IN (SELECT id FROM file)"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM knowledge_file "
            "WHERE knowledge_id NOT IN (SELECT id FROM knowledge) "
            "OR file_id NOT IN (SELECT id FROM file)"
        )
        for key, details in state.get("old_collections", {}).items():
            old_id = details.get("id")
            if not old_id:
                continue
            if not details.get("deleted"):
                raise ValueError(
                    f"old collection is not marked deleted: {old_id}"
                )
            collection = manifest["collections"].get(key)
            if collection is None:
                raise ValueError(
                    f"refresh state contains an unknown old collection: {key}"
                )
            row = connection.execute(
                "SELECT name FROM knowledge WHERE id = ?",
                (old_id,),
            ).fetchone()
            if row is None:
                continue
            canonical_name = collection["name"]
            if row[0] != canonical_name:
                raise ValueError(
                    f"old collection identity mismatch for {old_id}: "
                    f"expected {canonical_name}, found {row[0]}"
                )
            surviving_links = connection.execute(
                "SELECT COUNT(*) FROM knowledge_file kf "
                "JOIN file f ON f.id = kf.file_id "
                "WHERE kf.knowledge_id = ?",
                (old_id,),
            ).fetchone()[0]
            if surviving_links:
                raise ValueError(
                    f"old canonical collection still has {surviving_links} "
                    f"file links: {old_id}"
                )
            connection.execute(
                "DELETE FROM knowledge WHERE id = ? AND name = ?",
                (old_id, canonical_name),
            )
            removed_old_collection_rows += 1

        model_row = connection.execute(
            "SELECT meta FROM model WHERE id = 'sst-answerer'"
        ).fetchone()
        if model_row is None:
            raise ValueError("staged database has no sst-answerer model")
        model_meta = _decode_json(model_row[0])
        knowledge_for_model = []

        for key, details in state["new_collections"].items():
            knowledge_id = details["id"]
            row = connection.execute(
                "SELECT user_id, name, description, meta "
                "FROM knowledge WHERE id = ?",
                (knowledge_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"staged collection is missing: {knowledge_id}")
            file_rows = connection.execute(
                "SELECT f.filename, CASE "
                "WHEN COALESCE(json_extract(f.data, '$.status'), '') "
                "NOT IN ('completed', 'processed', 'success') THEN 1 ELSE 0 END "
                "FROM knowledge_file kf JOIN file f ON f.id = kf.file_id "
                "WHERE kf.knowledge_id = ?",
                (knowledge_id,),
            ).fetchall()
            filenames = [filename for filename, _incomplete in file_rows]
            count = len(filenames)
            incomplete = sum(
                int(is_incomplete or 0)
                for _filename, is_incomplete in file_rows
            )
            expected = details["expected_files"]
            if count != expected or incomplete:
                raise ValueError(
                    f"{details['canonical_name']} staged validation failed: "
                    f"{count}/{expected} files, {incomplete or 0} incomplete"
                )
            expected_filenames = {
                item["upload_name"]
                for item in manifest["collections"][key]["files"]
            }
            actual_filenames = set(filenames)
            if (
                len(actual_filenames) != len(filenames)
                or actual_filenames != expected_filenames
            ):
                missing = sorted(expected_filenames - actual_filenames)
                unexpected = sorted(actual_filenames - expected_filenames)
                duplicates = len(filenames) - len(actual_filenames)
                raise ValueError(
                    f"{details['canonical_name']} staged filenames differ "
                    f"from the upload plan: {len(missing)} missing, "
                    f"{len(unexpected)} unexpected, {duplicates} duplicate"
                )

            collision = connection.execute(
                "SELECT id FROM knowledge WHERE name = ? AND id != ?",
                (details["canonical_name"], knowledge_id),
            ).fetchone()
            if collision:
                raise ValueError(
                    f"old canonical collection still exists: {collision[0]}"
                )

            corpus_metadata = {
                "corpus_schema_version": manifest["schema_version"],
                "repositories": manifest["repositories"],
                "file_count": expected,
            }
            connection.execute(
                "UPDATE knowledge SET name = ?, description = ?, meta = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    details["canonical_name"],
                    details["description"],
                    json.dumps(corpus_metadata),
                    int(time.time()),
                    knowledge_id,
                ),
            )
            knowledge_for_model.append(
                {
                    "id": knowledge_id,
                    "user_id": row[0],
                    "name": details["canonical_name"],
                    "description": details["description"],
                    "meta": corpus_metadata,
                    "type": "collection",
                }
            )
            collection_results[details["canonical_name"]] = count

        model_meta["knowledge"] = knowledge_for_model
        connection.execute(
            "UPDATE model SET meta = ?, updated_at = ? WHERE id = 'sst-answerer'",
            (json.dumps(model_meta), int(time.time())),
        )
        connection.commit()

    write_corpus_lock(manifest, lock_destination)
    state["status"] = "finalized"
    return {
        "database": str(database),
        "collections": collection_results,
        "corpus_lock": str(lock_destination),
        "removed_dangling_links": removed_dangling_links,
        "removed_old_collection_rows": removed_old_collection_rows,
    }


def validate_seed_integrity(
    database: Path,
    vector_database: Path,
    uploads: Path,
    lock: dict,
) -> list[str]:
    errors = seed_safety_errors(database, vector_database)
    expected_collections = {
        details["name"]: int(details["file_count"])
        for details in lock["collections"].values()
    }
    expected_corpus_files = sum(expected_collections.values())

    with closing(sqlite3.connect(database)) as connection:
        all_knowledge_rows = connection.execute(
            "SELECT id, name FROM knowledge"
        ).fetchall()
        knowledge_rows = [
            row
            for row in all_knowledge_rows
            if row[1] in expected_collections
        ]
        unexpected_knowledge = [
            f"{name} ({knowledge_id})"
            for knowledge_id, name in all_knowledge_rows
            if name not in expected_collections
        ]
        if unexpected_knowledge:
            errors.append(
                "unexpected knowledge collections: "
                + ", ".join(sorted(unexpected_knowledge))
            )
        by_name: dict[str, list[str]] = {}
        for knowledge_id, name in knowledge_rows:
            by_name.setdefault(name, []).append(knowledge_id)

        for name, expected in expected_collections.items():
            ids = by_name.get(name, [])
            if len(ids) != 1:
                errors.append(
                    f"{name}: expected one canonical collection, found {len(ids)}"
                )
                continue
            actual = connection.execute(
                "SELECT COUNT(*) FROM knowledge_file WHERE knowledge_id = ?",
                (ids[0],),
            ).fetchone()[0]
            if actual != expected:
                errors.append(
                    f"{name}: expected {expected} files, found {actual}"
                )
            filename_rows = connection.execute(
                "SELECT f.filename FROM knowledge_file kf "
                "JOIN file f ON f.id = kf.file_id "
                "WHERE kf.knowledge_id = ?",
                (ids[0],),
            ).fetchall()
            filenames = [row[0] for row in filename_rows]
            if len(set(filenames)) != len(filenames):
                errors.append(f"{name}: duplicate filenames are linked")
            allowed_prefixes = (
                ("sst-docs__",)
                if name == "SST Documentation"
                else ("sst-core__", "sst-elements__")
            )
            wrong_collection = [
                filename
                for filename in filenames
                if not str(filename).startswith(allowed_prefixes)
            ]
            if wrong_collection:
                errors.append(
                    f"{name}: {len(wrong_collection)} files have the wrong "
                    "repository prefix"
                )

        stale_links = connection.execute(
            "SELECT COUNT(*) FROM knowledge_file kf "
            "LEFT JOIN knowledge k ON k.id = kf.knowledge_id "
            "LEFT JOIN file f ON f.id = kf.file_id "
            "WHERE k.id IS NULL OR f.id IS NULL"
        ).fetchone()[0]
        if stale_links:
            errors.append(
                f"knowledge_file contains {stale_links} dangling relationships"
            )

        duplicate_links = connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT knowledge_id, file_id, COUNT(*) AS copies "
            "FROM knowledge_file GROUP BY knowledge_id, file_id "
            "HAVING copies > 1)"
        ).fetchone()[0]
        if duplicate_links:
            errors.append(
                f"knowledge_file contains {duplicate_links} duplicate relationships"
            )

        unattached_files = connection.execute(
            "SELECT COUNT(*) FROM file f WHERE NOT EXISTS ("
            "SELECT 1 FROM knowledge_file kf "
            "JOIN knowledge k ON k.id = kf.knowledge_id "
            "WHERE kf.file_id = f.id)"
        ).fetchone()[0]
        if unattached_files:
            errors.append(
                f"file table contains {unattached_files} unattached records"
            )

        corpus_rows = connection.execute(
            "SELECT f.id, f.filename, f.path, "
            "COUNT(k.id), GROUP_CONCAT(k.name), "
            "COALESCE(json_extract(f.data, '$.status'), '') "
            "FROM file f "
            "LEFT JOIN knowledge_file kf ON kf.file_id = f.id "
            "LEFT JOIN knowledge k ON k.id = kf.knowledge_id "
            "WHERE f.filename LIKE 'sst-docs__%' "
            "OR f.filename LIKE 'sst-core__%' "
            "OR f.filename LIKE 'sst-elements__%' "
            "GROUP BY f.id, f.filename, f.path, f.data"
        ).fetchall()
        if len(corpus_rows) != expected_corpus_files:
            errors.append(
                "expected "
                f"{expected_corpus_files} corpus file records, "
                f"found {len(corpus_rows)}"
            )
        corpus_filenames = [row[1] for row in corpus_rows]
        if len(set(corpus_filenames)) != len(corpus_filenames):
            errors.append("corpus file records contain duplicate filenames")

        corpus_file_ids: set[str] = set()
        for (
            file_id,
            filename,
            _stored_path,
            link_count,
            knowledge_names,
            status,
        ) in corpus_rows:
            corpus_file_ids.add(file_id)
            if link_count != 1 or knowledge_names not in expected_collections:
                errors.append(
                    f"{filename}: expected one canonical knowledge link, "
                    f"found {link_count} ({knowledge_names or 'none'})"
                )
            if str(status).lower() not in COMPLETED_STATUSES:
                errors.append(
                    f"{filename}: file status is {status or 'missing'}"
                )

        all_file_rows = connection.execute(
            "SELECT id, path FROM file"
        ).fetchall()
        all_file_ids = {row[0] for row in all_file_rows}
        if len(all_file_rows) != expected_corpus_files:
            errors.append(
                f"expected {expected_corpus_files} total file records, "
                f"found {len(all_file_rows)}"
            )
        expected_upload_names = {Path(row[1]).name for row in all_file_rows}
        for _file_id, stored_path in all_file_rows:
            if not (uploads / Path(stored_path).name).is_file():
                errors.append(
                    f"file record {_file_id} has no uploaded file"
                )

    stored_uploads = {
        path.name
        for path in uploads.iterdir()
        if path.is_file()
    }
    orphan_uploads = stored_uploads - expected_upload_names
    if orphan_uploads:
        errors.append(
            f"{len(orphan_uploads)} uploads have no file row"
        )

    with closing(sqlite3.connect(vector_database)) as connection:
        vector_collections = {
            row[0] for row in connection.execute("SELECT name FROM collections")
        }

    canonical_ids = {
        ids[0]
        for ids in by_name.values()
        if len(ids) == 1
    }
    missing_knowledge_vectors = canonical_ids - vector_collections
    if missing_knowledge_vectors:
        errors.append(
            "missing canonical vector collections: "
            + ", ".join(sorted(missing_knowledge_vectors))
        )

    file_vector_ids = {
        name.removeprefix("file-")
        for name in vector_collections
        if name.startswith("file-")
    }
    missing_file_vectors = corpus_file_ids - file_vector_ids
    if missing_file_vectors:
        errors.append(
            f"{len(missing_file_vectors)} corpus files lack file-vector collections"
        )
    orphan_file_vectors = file_vector_ids - all_file_ids
    if orphan_file_vectors:
        errors.append(
            f"{len(orphan_file_vectors)} file-vector collections have no file row"
        )
    allowed_non_file_vectors = canonical_ids | {"knowledge-bases"}
    unexpected_vectors = {
        name
        for name in vector_collections
        if not name.startswith("file-")
        and name not in allowed_non_file_vectors
    }
    if unexpected_vectors:
        errors.append(
            "unexpected non-file vector collections: "
            + ", ".join(sorted(unexpected_vectors))
        )

    return errors


def _repositories(args) -> dict[str, Path]:
    return {
        name: path.expanduser().resolve()
        for name, path in default_repositories(args.sst_root).items()
    }


def command_plan(args) -> None:
    repositories = _repositories(args)
    manifest = build_manifest(repositories)
    bank = load_question_bank(args.bank)
    errors = bank_manifest_errors(bank, manifest)
    if errors:
        raise ValueError("; ".join(errors))
    if args.output:
        write_manifest(manifest, args.output)
    print(json.dumps(summary(manifest), indent=2))


def command_ingest(args) -> None:
    repositories = _repositories(args)
    manifest = load_manifest(args.manifest)
    source_errors = validate_manifest_sources(manifest, repositories)
    bank = load_question_bank(args.bank)
    source_errors.extend(bank_manifest_errors(bank, manifest))
    if source_errors:
        raise ValueError("; ".join(source_errors))

    client = OpenWebUIClient(
        args.url, timeout=args.http_timeout, retries=args.retries
    )
    client.authenticate(args.email, args.password, allow_signup=args.allow_signup)
    state = ingest(
        client,
        manifest,
        repositories,
        args.state,
        file_timeout=args.file_timeout,
        poll_interval=args.poll_interval,
        resume=args.resume,
        max_files=args.max_files,
    )
    print(
        json.dumps(
            {
                "status": state["status"],
                "progress": state["progress"],
            },
            indent=2,
        )
    )


def command_finalize(args) -> None:
    manifest = load_manifest(args.manifest)
    state = json.loads(args.state.read_text())
    result = finalize_staged_seed(
        args.database,
        state,
        manifest,
        args.lock_destination,
    )
    _write_state(args.state, state)
    print(json.dumps(result, indent=2))


def command_validate(args) -> None:
    lock = load_corpus_lock(args.lock)
    errors = validate_seed_integrity(
        args.database,
        args.vector_database,
        args.uploads,
        lock,
    )
    if errors:
        raise ValueError("; ".join(errors))
    print(
        json.dumps(
            {
                "valid": True,
                "database": str(args.database),
                "vector_database": str(args.vector_database),
                "uploads": str(args.uploads),
                "corpus_sha256": lock["corpus_sha256"],
            },
            indent=2,
        )
    )


def command_compact(args) -> None:
    result = compact_seed(args.database, args.vector_database)
    print(json.dumps(result, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--sst-root", type=Path)
    plan.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    plan.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    plan.set_defaults(handler=command_plan)

    ingest_parser = subparsers.add_parser("ingest")
    ingest_parser.add_argument("--sst-root", type=Path)
    ingest_parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    ingest_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ingest_parser.add_argument("--state", type=Path, required=True)
    ingest_parser.add_argument("--url", default="http://localhost:3000")
    ingest_parser.add_argument(
        "--email",
        default=os.environ.get("WEBUI_ADMIN_EMAIL")
        or _env_file_value("WEBUI_ADMIN_EMAIL"),
    )
    ingest_parser.add_argument(
        "--password",
        default=os.environ.get("WEBUI_ADMIN_PASSWORD")
        or _env_file_value("WEBUI_ADMIN_PASSWORD"),
    )
    ingest_parser.add_argument("--allow-signup", action="store_true")
    ingest_parser.add_argument("--http-timeout", type=float, default=120)
    ingest_parser.add_argument("--file-timeout", type=float, default=900)
    ingest_parser.add_argument("--poll-interval", type=float, default=0.1)
    ingest_parser.add_argument("--retries", type=int, default=4)
    ingest_parser.add_argument("--resume", action="store_true")
    ingest_parser.add_argument("--max-files", type=int)
    ingest_parser.set_defaults(handler=command_ingest)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--database", type=Path, required=True)
    finalize.add_argument("--state", type=Path, required=True)
    finalize.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    finalize.add_argument("--lock-destination", type=Path, required=True)
    finalize.set_defaults(handler=command_finalize)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--database", type=Path, required=True)
    validate.add_argument("--vector-database", type=Path, required=True)
    validate.add_argument("--uploads", type=Path, required=True)
    validate.add_argument("--lock", type=Path, required=True)
    validate.set_defaults(handler=command_validate)

    compact = subparsers.add_parser("compact")
    compact.add_argument("--database", type=Path, required=True)
    compact.add_argument("--vector-database", type=Path, required=True)
    compact.set_defaults(handler=command_compact)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (
        ApiError,
        OSError,
        ValueError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
