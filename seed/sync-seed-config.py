#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Synchronize reviewed source configuration into the seeded OpenWebUI DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


def sync_filter(db_path: Path, filter_path: Path) -> bool:
    """Update the seeded confidence gate from its canonical source file."""
    content = filter_path.read_text()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        columns = {
            column[1]
            for column in conn.execute("PRAGMA table_info(function)").fetchall()
        }
        selected_column = "meta" if "meta" in columns else "1"
        row = conn.execute(
            f"SELECT {selected_column} FROM function WHERE id = ?",
            ("confidence_gate",),
        ).fetchone()
        if row is None:
            print(
                "[demo] WARNING: confidence_gate is absent from the seed DB",
                file=sys.stderr,
            )
            return False

        assignments = ["content = ?"]
        values: list[object] = [content]
        if "name" in columns:
            assignments.append("name = ?")
            values.append("SST Answer Outcome Tracker")
        if "meta" in columns:
            meta = json.loads(row[0]) if row[0] else {}
            meta.update(
                {
                    "description": (
                        "Verifies grounded SST answers, labels source-only "
                        "documentation gaps, and records outcomes for the "
                        "Gap Tracker."
                    ),
                    "author": "open-webui-demo",
                }
            )
            assignments.append("meta = ?")
            values.append(json.dumps(meta))
        if "is_active" in columns:
            assignments.append("is_active = 1")
        if "is_global" in columns:
            assignments.append("is_global = 0")
        assignments.append("updated_at = ?")
        values.extend((int(time.time()), "confidence_gate"))
        conn.execute(
            f"UPDATE function SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
    return True


def constrain_sst_model(db_path: Path) -> bool:
    """Constrain the focused, offline SST Q&A model and its generation."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute(
            "SELECT meta FROM model WHERE id = ?", ("sst-answerer",)
        ).fetchone()
        if row is None:
            print(
                "[demo] WARNING: sst-answerer is absent from the seed DB",
                file=sys.stderr,
            )
            return False

        meta = json.loads(row[0])
        capabilities = meta.setdefault("capabilities", {})
        capabilities.update(
            {
                "file_context": True,
                "citations": True,
                "status_updates": True,
                "vision": False,
                "file_upload": False,
                "web_search": False,
                "image_generation": False,
                "code_interpreter": False,
                "builtin_tools": False,
            }
        )
        meta["builtinTools"] = {
            name: False
            for name in (
                "time",
                "memory",
                "chats",
                "notes",
                "knowledge",
                "channels",
                "web_search",
                "image_generation",
                "code_interpreter",
            )
        }
        filter_ids = meta.get("filterIds")
        if not isinstance(filter_ids, list):
            filter_ids = []
        if "confidence_gate" not in filter_ids:
            filter_ids.append("confidence_gate")
        meta["filterIds"] = filter_ids
        meta["suggestion_prompts"] = [
            {
                "title": ["Build an SST component", "and register it"],
                "content": (
                    "How do I create, build, register, and use a new SST "
                    "component?"
                ),
            },
            {
                "title": ["Checkpoint a simulation", "every hour"],
                "content": (
                    "How do I run SST and checkpoint my simulation every hour?"
                ),
            },
            {
                "title": ["Partition across MPI ranks", "from Python"],
                "content": (
                    "How can an SST Python configuration script manually "
                    "partition components across MPI ranks?"
                ),
            },
            {
                "title": ["Document component parameters", "and defaults"],
                "content": (
                    "How should I set and document default parameter values "
                    "for an SST component?"
                ),
            },
        ]
        columns = {
            column[1]
            for column in conn.execute("PRAGMA table_info(model)").fetchall()
        }
        if "params" in columns:
            params_row = conn.execute(
                "SELECT params FROM model WHERE id = ?", ("sst-answerer",)
            ).fetchone()
            params = json.loads(params_row[0]) if params_row and params_row[0] else {}
            custom_params = params.get("custom_params")
            if not isinstance(custom_params, dict):
                custom_params = {}
            chat_template_kwargs = custom_params.get("chat_template_kwargs")
            if not isinstance(chat_template_kwargs, dict):
                chat_template_kwargs = {}
            chat_template_kwargs["enable_thinking"] = False
            custom_params["chat_template_kwargs"] = chat_template_kwargs
            params.update(
                {
                    "temperature": 0,
                    "seed": 1,
                    "max_tokens": 700,
                    "custom_params": custom_params,
                }
            )
            conn.execute(
                "UPDATE model SET meta = ?, params = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    json.dumps(meta),
                    json.dumps(params),
                    int(time.time()),
                    "sst-answerer",
                ),
            )
        else:
            conn.execute(
                "UPDATE model SET meta = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meta), int(time.time()), "sst-answerer"),
            )
    return True


def sync_sst_knowledge(db_path: Path) -> bool:
    """Link the SST Answerer to exactly the two canonical SST collections."""
    canonical_names = ("SST Documentation", "SST Source Code")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        model_row = conn.execute(
            "SELECT meta FROM model WHERE id = ?", ("sst-answerer",)
        ).fetchone()
        if model_row is None:
            print(
                "[demo] WARNING: sst-answerer is absent from the seed DB",
                file=sys.stderr,
            )
            return False

        rows = conn.execute(
            "SELECT id, user_id, name, description, meta "
            "FROM knowledge WHERE name IN (?, ?) ORDER BY name",
            canonical_names,
        ).fetchall()
        by_name = {row[2]: row for row in rows}
        if set(by_name) != set(canonical_names):
            missing = sorted(set(canonical_names) - set(by_name))
            print(
                f"[demo] WARNING: canonical SST collections missing: {missing}",
                file=sys.stderr,
            )
            return False

        meta = json.loads(model_row[0])
        knowledge = []
        for name in canonical_names:
            row = by_name[name]
            collection_meta = json.loads(row[4]) if row[4] else None
            knowledge.append(
                {
                    "id": row[0],
                    "user_id": row[1],
                    "name": row[2],
                    "description": row[3],
                    "meta": collection_meta,
                    "type": "collection",
                }
            )
        meta["knowledge"] = knowledge
        conn.execute(
            "UPDATE model SET meta = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), int(time.time()), "sst-answerer"),
        )
    return True


def sync_sst_corpus_metadata(db_path: Path, lock_path: Path) -> bool:
    """Synchronize collection revision data from the compact corpus lock."""
    if not lock_path.is_file():
        print(
            f"[demo] WARNING: corpus lock is absent: {lock_path}",
            file=sys.stderr,
        )
        return False

    lock = json.loads(lock_path.read_text())
    repositories = lock["repositories"]
    collections = lock["collections"]
    commits = ", ".join(
        f"{name}={metadata['commit']}"
        for name, metadata in repositories.items()
    )

    with closing(sqlite3.connect(db_path)) as conn, conn:
        for collection in collections.values():
            name = collection["name"]
            row = conn.execute(
                "SELECT description, meta FROM knowledge WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                print(
                    f"[demo] WARNING: collection is absent: {name}",
                    file=sys.stderr,
                )
                return False
            description = (row[0] or "").split(" Corpus commits:", 1)[0]
            description = f"{description} Corpus commits: {commits}."
            meta = json.loads(row[1]) if row[1] else {}
            meta.update(
                {
                    "corpus_schema_version": lock["schema_version"],
                    "corpus_sha256": lock["corpus_sha256"],
                    "repositories": repositories,
                    "file_count": collection["file_count"],
                }
            )
            conn.execute(
                "UPDATE knowledge SET description = ?, meta = ?, "
                "updated_at = ? WHERE name = ?",
                (description, json.dumps(meta), int(time.time()), name),
            )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("filter_source", type=Path)
    args = parser.parse_args()

    if not args.database.is_file() or not args.filter_source.is_file():
        print("[demo] WARNING: seed synchronization inputs are missing", file=sys.stderr)
        return 1

    try:
        filter_ok = sync_filter(args.database, args.filter_source)
        model_ok = constrain_sst_model(args.database)
        lock_path = args.database.parent / "sst-corpus-lock.json"
        if lock_path.is_file():
            corpus_metadata_ok = sync_sst_corpus_metadata(
                args.database,
                lock_path,
            )
            corpus_metadata_synced = corpus_metadata_ok
        else:
            corpus_metadata_ok = True
            corpus_metadata_synced = False
            print(
                "[demo] Existing data has no corpus lock; "
                "collection revision data left unchanged."
            )
        knowledge_ok = sync_sst_knowledge(args.database)
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"[demo] WARNING: seed synchronization failed: {exc}", file=sys.stderr)
        return 1

    if filter_ok:
        print("[demo] Confidence Gate synchronized from reviewed source.")
    if model_ok:
        print("[demo] Unsupported SST Answerer tools disabled.")
    if corpus_metadata_synced:
        print("[demo] SST corpus revision data synchronized.")
    if knowledge_ok:
        print("[demo] SST Answerer knowledge links synchronized.")
    return (
        0
        if filter_ok and model_ok and corpus_metadata_ok and knowledge_ok
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
