#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Check the tracked tree for common public-release blockers."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COPYRIGHT = "Copyright Hewlett Packard Enterprise Development LP."
GITHUB_MAX_BYTES = 100 * 1024 * 1024
GITHUB_WARNING_BYTES = 50 * 1024 * 1024

HASH_COMMENT_SUFFIXES = {
    ".bash",
    ".py",
    ".sh",
    ".zsh",
}
SLASH_COMMENT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".cxx",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".java",
    ".js",
    ".jsx",
    ".rs",
    ".ts",
    ".tsx",
}

INTERNAL_PATTERNS = {
    "HPE internal GitHub URL": re.compile(rb"\bgithub\.hpe\.com\b", re.I),
    "HPE SharePoint URL": re.compile(
        rb"\bhttps?://[^\s\"'<>]*hpe(?:-my)?\.sharepoint\.com\b", re.I
    ),
}

SECRET_PATTERNS = {
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "GitHub token": re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"
    ),
    "OpenAI API key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "private key": re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "credential-bearing URL": re.compile(
        rb"\bhttps?://[^/\s:@]+:[^/\s@]+@[A-Za-z0-9.-]+\b"
    ),
}


def release_files() -> list[str]:
    """Return tracked files plus untracked files proposed for the next commit."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
    )
    return [
        value.decode("utf-8", errors="surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
    ]


def expected_comment(path: Path) -> str | None:
    if path.suffix in HASH_COMMENT_SUFFIXES:
        return f"# {COPYRIGHT}"
    if path.suffix in SLASH_COMMENT_SUFFIXES:
        return f"// {COPYRIGHT}"
    return None


def check_copyright(path: Path, data: bytes) -> str | None:
    expected = expected_comment(path)
    if expected is None:
        return None

    lines = data.decode("utf-8", errors="replace").splitlines()
    header_line = 1 if lines and lines[0].startswith("#!") else 0
    if len(lines) <= header_line or lines[header_line] != expected:
        return f"{path.relative_to(ROOT)}: expected '{expected}' near the top"
    return None


def matching_findings(
    relative_path: str, data: bytes, patterns: dict[str, re.Pattern[bytes]]
) -> list[str]:
    findings: list[str] = []
    for label, pattern in patterns.items():
        if pattern.search(data):
            findings.append(f"{relative_path}: possible {label}")
    return findings


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    files = release_files()

    generated_seed = [
        path for path in files if path == "seed/config-seed" or path.startswith("seed/config-seed/")
    ]
    if generated_seed:
        failures.append(
            f"seed/config-seed contains {len(generated_seed)} tracked generated files"
        )

    for relative_path in files:
        path = ROOT / relative_path
        if not path.is_file():
            continue

        size = path.stat().st_size
        if size >= GITHUB_MAX_BYTES:
            failures.append(
                f"{relative_path}: {size / 1024 / 1024:.1f} MiB exceeds GitHub's limit"
            )
        elif size >= GITHUB_WARNING_BYTES:
            warnings.append(
                f"{relative_path}: {size / 1024 / 1024:.1f} MiB triggers GitHub's warning"
            )

        data = path.read_bytes()
        failures.extend(matching_findings(relative_path, data, INTERNAL_PATTERNS))
        failures.extend(matching_findings(relative_path, data, SECRET_PATTERNS))

        copyright_failure = check_copyright(path, data)
        if copyright_failure:
            failures.append(copyright_failure)

    license_names = {"COPYING", "LICENSE", "LICENSE.md", "LICENSE.txt"}
    if not any(path in license_names for path in files):
        failures.append(
            "No root license is tracked; add the approved license before "
            "public release"
        )

    print(f"Audited {len(files)} release-candidate files.")
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("Public-release tree checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
