# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Line scanning helpers for tracked non-Python text files."""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Iterator


_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUFF_OWNED = frozenset((".py",))
_BINARY_EXT = frozenset((".png",))
_IGNORED_NAMES = frozenset(("uv.lock", "LICENSE"))


def _load_limit() -> int:
    config_data = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    return int(config_data["tool"]["ruff"]["line-length"])


LIMIT = _load_limit()


def _tracked_text_files() -> list[tuple[str, Path]]:
    tracked_output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked_paths = (
        (relative_path, _REPO_ROOT / relative_path)
        for relative_path in filter(None, tracked_output.split("\0"))
    )
    return [
        (relative_path, path)
        for relative_path, path in tracked_paths
        if path.suffix not in _RUFF_OWNED | _BINARY_EXT
        and path.name not in _IGNORED_NAMES
    ]


def _line_is_exempt(line: str, limit: int) -> bool:
    longest_token = max(map(len, line.split()), default=0)
    return len(line) <= limit or longest_token > limit


def _unfenced_lines(text: str) -> Iterator[tuple[int, str]]:
    in_fence = False
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if raw_line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            yield line_number, raw_line


def over_limit_lines(
    text: str,
    limit: int = LIMIT,
) -> Iterator[tuple[int, int]]:
    """Yield line numbers and lengths for wrappable over-limit text."""
    for line_number, raw_line in _unfenced_lines(text):
        line = raw_line.rstrip()
        if _line_is_exempt(line, limit):
            continue
        yield line_number, len(line)


def _file_violations(relative_path: str, path: Path) -> list[str]:
    file_content = path.read_bytes()
    if b"\x00" in file_content:
        return []
    return [
        f"{relative_path}:{line_number} ({length} > {LIMIT} chars)"
        for line_number, length in over_limit_lines(
            file_content.decode("utf-8"),
        )
    ]


def _flagged_lines(text: str, limit: int) -> list[int]:
    return [
        line_number
        for line_number, _ in over_limit_lines(text, limit=limit)
    ]
