#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Deterministic SST corpus discovery and revision helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable


DOC_SUFFIXES = {".md", ".mdx"}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".py"}
INDEX_FORMAT_VERSION = 2
TAB_ITEM_PATTERN = re.compile(
    r"<TabItem\b(?P<attributes>[^>]*)>(?P<body>.*?)</TabItem>",
    re.DOTALL | re.IGNORECASE,
)
COLLECTIONS = {
    "documentation": {
        "name": "SST Documentation",
        "description": (
            "Official SST documentation: guides, tutorials, configuration "
            "references, and API documentation."
        ),
    },
    "source": {
        "name": "SST Source Code",
        "description": (
            "Current SST Core and SST Elements C, C++, and Python source. "
            "Citations preserve repository-relative paths."
        ),
    },
}


def _attribute(attributes: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1",
        attributes,
        re.IGNORECASE,
    )
    return match.group(2).strip() if match else ""


def contextualize_mdx_tabs(content: str) -> str:
    """Make Docusaurus tab labels visible in every indexed tab line."""

    def replace_tab(match: re.Match) -> str:
        attributes = match.group("attributes")
        label = (
            _attribute(attributes, "label")
            or _attribute(attributes, "value")
            or "Unlabeled"
        )
        context = f"Documentation tab: {label}."
        output = [f"## {context}"]
        fence = ""
        for line in match.group("body").strip("\n").splitlines():
            stripped = line.lstrip()
            marker = re.match(r"(`{3,}|~{3,})", stripped)
            if marker:
                if not fence:
                    output.append(context)
                    fence = marker.group(1)[0]
                elif marker.group(1).startswith(fence):
                    fence = ""
                output.append(line)
            elif stripped and not fence:
                output.extend((context, line))
            else:
                output.append(line)
        return "\n".join(output)

    contextualized = TAB_ITEM_PATTERN.sub(replace_tab, content)
    return re.sub(
        r"</?Tabs\b[^>]*>",
        "",
        contextualized,
        flags=re.IGNORECASE,
    )


def indexed_content(path: Path, source_label: str) -> bytes:
    """Return the exact path-labeled content sent to Open WebUI."""
    content = path.read_bytes()
    if (
        source_label.startswith("sst-docs/")
        and path.suffix.lower() == ".mdx"
    ):
        content = contextualize_mdx_tabs(content.decode("utf-8")).encode()
    return f"Repository path: {source_label}\n\n".encode() + content


def public_repository_url(remote_url: str) -> str:
    """Normalize public GitHub remotes so corpus refresh needs no SSH key."""
    normalized = remote_url.strip()
    if normalized.startswith("git@github.com:"):
        return "https://github.com/" + normalized.removeprefix(
            "git@github.com:"
        )
    if normalized.startswith("ssh://git@github.com/"):
        return "https://github.com/" + normalized.removeprefix(
            "ssh://git@github.com/"
        )
    if normalized.startswith("git://github.com/"):
        return "https://github.com/" + normalized.removeprefix(
            "git://github.com/"
        )
    return normalized


def run_git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "output", "") or str(exc)
        raise ValueError(f"Git command failed in {repo}: {detail.strip()}") from exc


def git_head(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


def verify_checkout(
    repo: Path,
    *,
    expected_remote: str | None = None,
    check_upstream: bool = True,
) -> dict:
    head = git_head(repo)
    dirty = run_git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise ValueError(f"Tracked changes make corpus non-reproducible: {repo}")

    remote_url = run_git(repo, "remote", "get-url", "origin")
    if expected_remote and remote_url.rstrip("/") != expected_remote.rstrip("/"):
        raise ValueError(
            f"{repo.name} origin is {remote_url}, expected {expected_remote}"
        )

    branch = run_git(repo, "branch", "--show-current")
    upstream = ""
    if check_upstream:
        try:
            upstream = run_git(
                repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
            )
            upstream_head = run_git(repo, "rev-parse", upstream)
        except ValueError as exc:
            raise ValueError(f"{repo.name} has no readable upstream branch") from exc
        if head != upstream_head:
            raise ValueError(
                f"{repo.name} HEAD {head} differs from {upstream} {upstream_head}"
            )

    return {
        "path": str(repo.resolve()),
        "commit": head,
        "branch": branch,
        "upstream": upstream,
        "remote": remote_url,
    }


def fetch_origin(repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "--prune", "origin"],
        check=True,
    )


def default_repositories(root: Path | None = None) -> dict[str, Path]:
    base = root or Path(
        os.environ.get("SST_REPOS_ROOT", "~/dev/sstsimulator")
    ).expanduser()
    return {
        "sst-docs": base / "sst-docs",
        "sst-core": base / "sst-core",
        "sst-elements": base / "sst-elements",
    }


def tracked_files(
    repository: Path,
    root: Path,
    suffixes: set[str],
) -> Iterable[Path]:
    """Yield only Git-tracked corpus files beneath *root*."""
    try:
        pathspec = root.relative_to(repository).as_posix()
    except ValueError as exc:
        raise ValueError(f"Corpus root is outside its repository: {root}") from exc
    try:
        output = subprocess.check_output(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "-z",
                "--",
                pathspec,
            ],
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "output", b"")
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise ValueError(
            f"Cannot list tracked corpus files in {repository}: "
            f"{str(detail or exc).strip()}"
        ) from exc

    paths = [
        repository / os.fsdecode(value)
        for value in output.split(b"\0")
        if value
    ]
    for path in sorted(paths):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in suffixes
            and ".git" not in path.parts
        ):
            yield path


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry(repository: str, repo: Path, path: Path) -> dict:
    relative_path = path.relative_to(repo).as_posix()
    source_label = f"{repository}/{relative_path}"
    upload_name = f"{repository}__{relative_path.replace('/', '__')}"
    indexed = indexed_content(path, source_label)
    return {
        "repository": repository,
        "path": relative_path,
        "upload_name": upload_name,
        "bytes": path.stat().st_size,
        "sha256": _hash(path),
        "indexed_bytes": len(indexed),
        "indexed_sha256": _hash_bytes(indexed),
    }


def build_manifest(repositories: dict[str, Path]) -> dict:
    docs = repositories["sst-docs"]
    core = repositories["sst-core"]
    elements = repositories["sst-elements"]

    checkouts = {
        name: verify_checkout(path)
        for name, path in repositories.items()
    }
    documentation_files = [
        _entry("sst-docs", docs, path)
        for path in tracked_files(docs, docs / "docs", DOC_SUFFIXES)
    ]
    source_files = [
        *(
            _entry("sst-core", core, path)
            for path in tracked_files(core, core / "src", SOURCE_SUFFIXES)
        ),
        *(
            _entry("sst-elements", elements, path)
            for path in tracked_files(
                elements,
                elements / "src",
                SOURCE_SUFFIXES,
            )
        ),
    ]
    source_files.sort(key=lambda item: (item["repository"], item["path"]))

    all_names = [
        item["upload_name"]
        for item in documentation_files + source_files
    ]
    if len(all_names) != len(set(all_names)):
        raise ValueError("Path-preserving upload names are not unique")

    return {
        "schema_version": 1,
        "index_format_version": INDEX_FORMAT_VERSION,
        "selection": {
            "documentation": "sst-docs/docs/**/*.{md,mdx}",
            "source": [
                "sst-core/src/**/*.{c,cc,cpp,h,hpp,py}",
                "sst-elements/src/**/*.{c,cc,cpp,h,hpp,py}",
            ],
        },
        "repositories": {
            name: {
                "repository": public_repository_url(
                    checkouts[name]["remote"]
                ),
                "commit": checkouts[name]["commit"],
                "branch": checkouts[name]["branch"],
            }
            for name in sorted(checkouts)
        },
        "collections": {
            "documentation": {
                **COLLECTIONS["documentation"],
                "file_count": len(documentation_files),
                "total_bytes": sum(item["bytes"] for item in documentation_files),
                "files": documentation_files,
            },
            "source": {
                **COLLECTIONS["source"],
                "file_count": len(source_files),
                "total_bytes": sum(item["bytes"] for item in source_files),
                "files": source_files,
            },
        },
    }


def write_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def corpus_sha256(manifest: dict) -> str:
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_corpus_lock(manifest: dict) -> dict:
    return {
        "schema_version": 1,
        "index_format_version": manifest["index_format_version"],
        "corpus_sha256": corpus_sha256(manifest),
        "repositories": manifest["repositories"],
        "collections": {
            key: {
                "name": collection["name"],
                "file_count": collection["file_count"],
                "total_bytes": collection["total_bytes"],
            }
            for key, collection in manifest["collections"].items()
        },
    }


def write_corpus_lock(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_corpus_lock(manifest), indent=2) + "\n")


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read corpus manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise ValueError("Corpus manifest schema_version must be 1")
    if manifest.get("index_format_version") != INDEX_FORMAT_VERSION:
        raise ValueError(
            "Corpus manifest index_format_version must be "
            f"{INDEX_FORMAT_VERSION}"
        )
    return manifest


def load_corpus_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read corpus lock {path}: {exc}") from exc
    if lock.get("schema_version") != 1:
        raise ValueError("Corpus lock schema_version must be 1")
    if lock.get("index_format_version") != INDEX_FORMAT_VERSION:
        raise ValueError(
            "Corpus lock index_format_version must be "
            f"{INDEX_FORMAT_VERSION}"
        )
    if not lock.get("corpus_sha256"):
        raise ValueError("Corpus lock has no corpus_sha256")
    repositories = lock.get("repositories")
    if not isinstance(repositories, dict) or set(repositories) != {
        "sst-docs",
        "sst-core",
        "sst-elements",
    }:
        raise ValueError(
            "Corpus lock must describe sst-docs, sst-core, and sst-elements"
        )
    for name, metadata in repositories.items():
        if not isinstance(metadata, dict) or not all(
            isinstance(metadata.get(field), str) and metadata[field]
            for field in ("repository", "commit", "branch")
        ):
            raise ValueError(
                f"Corpus lock repository metadata is incomplete for {name}"
            )
    collections = lock.get("collections")
    if not isinstance(collections, dict) or set(collections) != set(COLLECTIONS):
        raise ValueError(
            "Corpus lock must describe documentation and source collections"
        )
    for key, metadata in collections.items():
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != COLLECTIONS[key]["name"]
            or not isinstance(metadata.get("file_count"), int)
            or metadata["file_count"] < 0
            or not isinstance(metadata.get("total_bytes"), int)
            or metadata["total_bytes"] < 0
        ):
            raise ValueError(
                f"Corpus lock collection metadata is invalid for {key}"
            )
    return lock


def validate_manifest_sources(
    manifest: dict, repositories: dict[str, Path]
) -> list[str]:
    errors: list[str] = []
    for name, metadata in manifest["repositories"].items():
        actual = git_head(repositories[name])
        if actual != metadata["commit"]:
            errors.append(
                f"{name} HEAD {actual} differs from manifest {metadata['commit']}"
            )

    for collection in manifest["collections"].values():
        files = collection["files"]
        if collection["file_count"] != len(files):
            errors.append(f"{collection['name']} file_count is inconsistent")
        for item in files:
            path = repositories[item["repository"]] / item["path"]
            if not path.is_file():
                errors.append(f"Missing corpus file: {path}")
                continue
            if path.stat().st_size != item["bytes"] or _hash(path) != item["sha256"]:
                errors.append(f"Corpus file changed after manifest creation: {path}")
                continue
            source_label = f"{item['repository']}/{item['path']}"
            indexed = indexed_content(path, source_label)
            if (
                len(indexed) != item["indexed_bytes"]
                or _hash_bytes(indexed) != item["indexed_sha256"]
            ):
                errors.append(
                    "Indexed corpus content changed after manifest creation: "
                    f"{path}"
                )
    return errors


def validate_corpus_lock(
    lock: dict, repositories: dict[str, Path]
) -> list[str]:
    errors: list[str] = []
    try:
        expected = build_corpus_lock(build_manifest(repositories))
    except ValueError as exc:
        return [str(exc)]

    if lock["corpus_sha256"] != expected["corpus_sha256"]:
        errors.append(
            "corpus lock hash does not match the selected indexed content: "
            f"{lock['corpus_sha256']} != {expected['corpus_sha256']}"
        )
    if lock["repositories"] != expected["repositories"]:
        errors.append("corpus lock repository metadata is stale")
    if lock["collections"] != expected["collections"]:
        errors.append("corpus lock collection metadata is stale")
    return errors


def summary(manifest: dict) -> dict:
    return {
        "corpus_sha256": corpus_sha256(manifest),
        "repositories": {
            name: metadata["commit"]
            for name, metadata in manifest["repositories"].items()
        },
        "collections": {
            collection["name"]: {
                "files": collection["file_count"],
                "bytes": collection["total_bytes"],
            }
            for collection in manifest["collections"].values()
        },
    }
