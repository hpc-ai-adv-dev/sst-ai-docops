#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Validate and compact an Open WebUI seed before distribution."""

from __future__ import annotations

import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


ADMIN_NAME = "Admin"
ADMIN_EMAIL = "admin@localhost"
PERSISTED_VECTOR_SEGMENT = "urn:chroma:segment/vector/hnsw-local-persisted"
ALLOWED_SEED_ENTRIES = {
    "cache",
    "sst-corpus-lock.json",
    "uploads",
    "vector_db",
    "webui.db",
}

USER_CONTENT_TABLES = (
    "access_grant",
    "api_key",
    "channel",
    "channel_file",
    "channel_member",
    "channel_webhook",
    "chat",
    "chat_file",
    "chat_message",
    "chatidtag",
    "document",
    "feedback",
    "folder",
    "group",
    "group_member",
    "memory",
    "message",
    "message_reaction",
    "note",
    "oauth_session",
    "prompt",
    "prompt_history",
    "skill",
    "tag",
    "tool",
)

DATABASE_PATTERNS = {
    "HPE internal GitHub URL": re.compile(r"\bgithub\.hpe\.com\b", re.I),
    "HPE SharePoint URL": re.compile(
        r"\bhttps?://[^\s\"'<>]*hpe(?:-my)?\.sharepoint\.com\b", re.I
    ),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "GitHub token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"
    ),
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "credential-bearing URL": re.compile(
        r"\bhttps?://[^/\s:@]+:[^/\s@]+@[A-Za-z0-9.-]+\b"
    ),
}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> list[tuple[str, str]]:
    quoted = _quote_identifier(table)
    return [
        (str(row[1]), str(row[2] or "").upper())
        for row in connection.execute(f"PRAGMA table_info({quoted})")
    ]


def _integrity_error(
    connection: sqlite3.Connection, label: str
) -> str | None:
    rows = [
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check")
    ]
    if rows != ["ok"]:
        return f"{label} integrity check failed: {'; '.join(rows[:3])}"
    return None


def _webui_safety_errors(connection: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    tables = _table_names(connection)
    required = {
        "auth",
        "file",
        "function",
        "knowledge",
        "knowledge_file",
        "model",
        "user",
        *USER_CONTENT_TABLES,
    }
    missing = sorted(required - tables)
    if missing:
        errors.append(
            "webui database is missing required tables: " + ", ".join(missing)
        )
        return errors

    users = connection.execute(
        "SELECT id, name, email, role FROM user"
    ).fetchall()
    if (
        len(users) != 1
        or users[0][1] != ADMIN_NAME
        or users[0][2] != ADMIN_EMAIL
        or users[0][3] != "admin"
    ):
        errors.append(
            "seed must contain exactly Admin <admin@localhost> with the admin role"
        )
        admin_id = None
    else:
        admin_id = users[0][0]

    auth_rows = connection.execute(
        "SELECT id, email, active FROM auth"
    ).fetchall()
    if (
        admin_id is None
        or len(auth_rows) != 1
        or auth_rows[0][0] != admin_id
        or auth_rows[0][1] != ADMIN_EMAIL
        or int(auth_rows[0][2]) != 1
    ):
        errors.append(
            "seed must contain one active auth row matching admin@localhost"
        )

    for table in USER_CONTENT_TABLES:
        count = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
        ).fetchone()[0]
        if count:
            errors.append(
                f"{table} contains {count} user-generated row"
                + ("" if count == 1 else "s")
                )

    expected_configuration = {
        "function": {"confidence_gate"},
        "model": {"sst-answerer"},
    }
    for table, expected_ids in expected_configuration.items():
        actual_ids = {
            str(row[0])
            for row in connection.execute(
                f"SELECT id FROM {_quote_identifier(table)}"
            )
        }
        if actual_ids != expected_ids:
            errors.append(
                f"{table} IDs must be exactly "
                + ", ".join(sorted(expected_ids))
                + "; found "
                + (", ".join(sorted(actual_ids)) or "none")
            )

    if admin_id is not None:
        for table in sorted(tables):
            columns = {name for name, _kind in _table_columns(connection, table)}
            if "user_id" not in columns or table in USER_CONTENT_TABLES:
                continue
            count = connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)} "
                "WHERE user_id IS NOT NULL AND user_id != ?",
                (admin_id,),
            ).fetchone()[0]
            if count:
                errors.append(
                    f"{table} contains {count} row"
                    + ("" if count == 1 else "s")
                    + " owned by a non-admin user"
                )

    for table in sorted(tables):
        for column, kind in _table_columns(connection, table):
            if table == "auth" and column == "password":
                continue
            if not any(
                marker in kind
                for marker in ("CHAR", "CLOB", "JSON", "TEXT", "VARCHAR")
            ):
                continue
            query = (
                f"SELECT {_quote_identifier(column)} "
                f"FROM {_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)} IS NOT NULL"
            )
            findings: set[str] = set()
            for (value,) in connection.execute(query):
                text = (
                    value.decode("utf-8", errors="replace")
                    if isinstance(value, bytes)
                    else str(value)
                )
                for label, pattern in DATABASE_PATTERNS.items():
                    if pattern.search(text):
                        findings.add(label)
            for label in sorted(findings):
                errors.append(f"{table}.{column} contains a possible {label}")

    integrity = _integrity_error(connection, "webui database")
    if integrity:
        errors.append(integrity)
    return errors


def _vector_state(
    connection: sqlite3.Connection,
) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    tables = _table_names(connection)
    required = {"collections", "embeddings", "embeddings_queue", "segments"}
    missing = sorted(required - tables)
    if missing:
        return (
            [
                "vector database is missing required tables: "
                + ", ".join(missing)
            ],
            set(),
        )

    collection_ids = {
        str(row[0])
        for row in connection.execute("SELECT id FROM collections")
    }
    segment_rows = connection.execute(
        "SELECT id, type, collection FROM segments"
    ).fetchall()
    segment_ids = {str(row[0]) for row in segment_rows}
    persisted_segments = {
        str(segment_id)
        for segment_id, segment_type, _collection_id in segment_rows
        if segment_type == PERSISTED_VECTOR_SEGMENT
    }

    orphan_segments = [
        str(segment_id)
        for segment_id, _segment_type, collection_id in segment_rows
        if str(collection_id) not in collection_ids
    ]
    if orphan_segments:
        errors.append(
            f"vector database contains {len(orphan_segments)} orphan segment"
            + ("" if len(orphan_segments) == 1 else "s")
        )

    orphan_embeddings = connection.execute(
        "SELECT COUNT(*) FROM embeddings e "
        "LEFT JOIN segments s ON s.id = e.segment_id "
        "WHERE s.id IS NULL"
    ).fetchone()[0]
    if orphan_embeddings:
        errors.append(
            f"vector database contains {orphan_embeddings} orphan embedding"
            + ("" if orphan_embeddings == 1 else "s")
        )

    topic_rows = connection.execute(
        "SELECT DISTINCT topic FROM embeddings_queue"
    ).fetchall()
    orphan_topics = 0
    for (topic_value,) in topic_rows:
        topic = str(topic_value)
        collection_id = topic.rsplit("/", 1)[-1]
        if (
            not topic.startswith("persistent://")
            or not collection_id
            or collection_id not in collection_ids
        ):
            orphan_topics += 1
    if orphan_topics:
        errors.append(
            f"vector database contains {orphan_topics} orphan queue topic"
            + ("" if orphan_topics == 1 else "s")
        )

    if len(segment_ids) != len(segment_rows):
        errors.append("vector database contains duplicate segment IDs")

    integrity = _integrity_error(connection, "vector database")
    if integrity:
        errors.append(integrity)
    return errors, persisted_segments


def seed_safety_errors(
    database: Path,
    vector_database: Path,
    *,
    allow_orphan_segment_directories: bool = False,
) -> list[str]:
    """Return distribution-safety and vector-consistency errors."""
    errors: list[str] = []
    if not database.is_file():
        errors.append(f"webui database is missing: {database}")
    if not vector_database.is_file():
        errors.append(f"vector database is missing: {vector_database}")
    if errors:
        return errors

    unexpected_entries = sorted(
        path.name
        for path in database.parent.iterdir()
        if path.name not in ALLOWED_SEED_ENTRIES
    )
    if unexpected_entries:
        errors.append(
            "seed data contains unexpected entries: "
            + ", ".join(unexpected_entries)
        )

    with closing(sqlite3.connect(database)) as connection:
        errors.extend(_webui_safety_errors(connection))

    with closing(sqlite3.connect(vector_database)) as connection:
        vector_errors, persisted_segments = _vector_state(connection)
        errors.extend(vector_errors)

    vector_root = vector_database.parent
    directory_names = {
        path.name
        for path in vector_root.iterdir()
        if path.is_dir()
    }
    missing_directories = persisted_segments - directory_names
    if missing_directories:
        errors.append(
            f"{len(missing_directories)} persisted vector segment"
            + ("" if len(missing_directories) == 1 else "s")
            + " have no data directory"
        )

    orphan_directories = directory_names - persisted_segments
    if orphan_directories and not allow_orphan_segment_directories:
        count = len(orphan_directories)
        noun = "directory" if count == 1 else "directories"
        verb = "has" if count == 1 else "have"
        errors.append(
            f"{count} vector data {noun} {verb} no persisted segment"
        )
    return errors


def _remove_stale_file_vectors(
    database: Path,
    vector_database: Path,
) -> tuple[int, list[str]]:
    with closing(sqlite3.connect(database)) as connection:
        valid_file_ids = {
            str(row[0])
            for row in connection.execute("SELECT id FROM file")
        }

    with closing(sqlite3.connect(vector_database)) as connection, connection:
        stale_collections = [
            (str(collection_id), str(name))
            for collection_id, name in connection.execute(
                "SELECT id, name FROM collections WHERE name LIKE 'file-%'"
            )
            if str(name).removeprefix("file-") not in valid_file_ids
        ]
        if not stale_collections:
            return 0, []

        connection.execute(
            "CREATE TEMP TABLE aidocops_stale_collections "
            "(id TEXT PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO aidocops_stale_collections VALUES (?)",
            [(collection_id,) for collection_id, _name in stale_collections],
        )
        stale_segments = [
            (str(segment_id), str(segment_type))
            for segment_id, segment_type in connection.execute(
                "SELECT id, type FROM segments WHERE collection IN "
                "(SELECT id FROM aidocops_stale_collections)"
            )
        ]
        connection.execute(
            "CREATE TEMP TABLE aidocops_stale_segments "
            "(id TEXT PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO aidocops_stale_segments VALUES (?)",
            [(segment_id,) for segment_id, _kind in stale_segments],
        )

        tables = _table_names(connection)
        if "embedding_fulltext_search" in tables:
            connection.execute(
                "DELETE FROM embedding_fulltext_search WHERE rowid IN ("
                "SELECT e.id FROM embeddings e "
                "JOIN aidocops_stale_segments s ON s.id = e.segment_id)"
            )
        for table in ("embedding_metadata", "embedding_metadata_array"):
            if table in tables:
                connection.execute(
                    f"DELETE FROM {_quote_identifier(table)} WHERE id IN ("
                    "SELECT e.id FROM embeddings e "
                    "JOIN aidocops_stale_segments s ON s.id = e.segment_id)"
                )
        connection.execute(
            "DELETE FROM embeddings WHERE segment_id IN "
            "(SELECT id FROM aidocops_stale_segments)"
        )
        if "max_seq_id" in tables:
            connection.execute(
                "DELETE FROM max_seq_id WHERE segment_id IN "
                "(SELECT id FROM aidocops_stale_segments)"
            )
        if "segment_metadata" in tables:
            connection.execute(
                "DELETE FROM segment_metadata WHERE segment_id IN "
                "(SELECT id FROM aidocops_stale_segments)"
            )
        connection.execute(
            "DELETE FROM embeddings_queue WHERE topic IN ("
            "SELECT 'persistent://default/default/' || id "
            "FROM aidocops_stale_collections)"
        )
        connection.execute(
            "DELETE FROM segments WHERE id IN "
            "(SELECT id FROM aidocops_stale_segments)"
        )
        if "collection_metadata" in tables:
            connection.execute(
                "DELETE FROM collection_metadata WHERE collection_id IN "
                "(SELECT id FROM aidocops_stale_collections)"
            )
        connection.execute(
            "DELETE FROM collections WHERE id IN "
            "(SELECT id FROM aidocops_stale_collections)"
        )

    persisted_segments = [
        segment_id
        for segment_id, segment_type in stale_segments
        if segment_type == PERSISTED_VECTOR_SEGMENT
    ]
    return len(stale_collections), persisted_segments


def compact_seed(database: Path, vector_database: Path) -> dict:
    """Remove stale file vectors and directories, then VACUUM the seed."""
    errors = seed_safety_errors(
        database,
        vector_database,
        allow_orphan_segment_directories=True,
    )
    if errors:
        raise ValueError("; ".join(errors))

    removed_collections, stale_segment_directories = (
        _remove_stale_file_vectors(database, vector_database)
    )
    removed: list[str] = []
    for name in stale_segment_directories:
        path = vector_database.parent / name
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(name)

    with closing(sqlite3.connect(vector_database)) as connection:
        _errors, persisted_segments = _vector_state(connection)

    for path in vector_database.parent.iterdir():
        if not path.is_dir() or path.name in persisted_segments:
            continue
        try:
            uuid.UUID(path.name)
        except ValueError as exc:
            raise ValueError(
                f"unexpected non-segment directory in vector database: {path.name}"
            ) from exc
        shutil.rmtree(path)
        removed.append(path.name)

    for path in (database, vector_database):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("VACUUM")

    errors = seed_safety_errors(database, vector_database)
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "database": str(database),
        "vector_database": str(vector_database),
        "removed_vector_collections": removed_collections,
        "removed_vector_directories": len(removed),
    }
