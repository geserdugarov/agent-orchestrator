# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Line-length enforcement for tracked Markdown/text files.

Python is covered by Ruff (E501, `line-length` in `pyproject.toml`). This
module applies the same limit -- read from that one source of truth -- to
the tracked text/docs files Ruff does not lint, with explicit exemptions
for content that cannot be reflowed: fenced code blocks and single
unbreakable tokens (long URLs/paths), plus binary assets, the lockfile,
and the verbatim LICENSE.
"""
import subprocess
import tomllib
import unittest
from pathlib import Path
from typing import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Ruff already lints Python; scanning it here would double-report and
# ignore Ruff's own inline suppression directives.
_RUFF_OWNED = frozenset((".py",))
# Binary assets carry no line concept.
_BINARY_EXT = frozenset((".png",))
# Machine-generated / verbatim content that is never hand-wrapped.
_IGNORED_NAMES = frozenset(("uv.lock", "LICENSE"))
_LineRuleCases = dict[str, tuple[str, list[int]]]
_TEST_LINE_LIMIT = 120
_LONG_PROSE_REPETITIONS = 40
_LONG_TOKEN_LENGTH = 200
_DIVIDER_WIDTH = 75
_ONE_BELOW_TEST_LIMIT = 119


def _load_limit() -> int:
    config_data = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    return int(config_data["tool"]["ruff"]["line-length"])


LIMIT = _load_limit()


def _tracked_text_files() -> list[tuple[str, Path]]:
    """Return (relpath, path) for tracked files this checker owns."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked_paths = (
        (relative_path, _REPO_ROOT / relative_path)
        for relative_path in filter(None, out.split("\0"))
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
    """Yield (lineno, length) for wrappable lines longer than `limit`.

    Length is counted in Unicode code points, so multi-byte box-drawing
    art is measured by display width rather than byte count. Lines inside
    fenced code blocks are skipped, and a line whose overflow is a single
    unbreakable token (e.g. a long URL) is exempt because no wrapping can
    bring it under the limit.
    """
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
        for line_number, length in over_limit_lines(file_content.decode("utf-8"))
    ]


def _flagged_lines(text: str) -> list[int]:
    return [
        line_number
        for line_number, _ in over_limit_lines(text, limit=_TEST_LINE_LIMIT)
    ]


def _line_rule_cases() -> _LineRuleCases:
    long_prose = "word " * _LONG_PROSE_REPETITIONS
    long_url = "https://example.com/" + "a" * _LONG_TOKEN_LENGTH
    wide_divider = "─" * _DIVIDER_WIDTH
    return {
        "short line": ("plain short line", []),
        "wrappable prose": (long_prose.rstrip(), [1]),
        "fenced block": (f"```\n{long_prose}\n```", []),
        "tilde fence": (f"~~~\n{long_prose}\n~~~", []),
        "unbreakable url": (long_url, []),
        "wide box drawing": (wide_divider, []),
        "exactly at limit": ("a" * _TEST_LINE_LIMIT, []),
        "one over limit": ("a " + "a" * _ONE_BELOW_TEST_LIMIT, [1]),
    }


class TrackedFilesWithinLimitTest(unittest.TestCase):
    def test_no_tracked_text_file_exceeds_limit(self) -> None:
        violations = [
            violation
            for relative_path, path in _tracked_text_files()
            for violation in _file_violations(relative_path, path)
        ]
        self.assertEqual(
            violations,
            [],
            "tracked text lines exceed the line-length limit:\n" + "\n".join(violations),
        )


class OverLimitRuleTest(unittest.TestCase):
    def test_rule_cases(self) -> None:
        for name, (text, expected) in _line_rule_cases().items():
            with self.subTest(case=name):
                self.assertEqual(_flagged_lines(text), expected)


if __name__ == "__main__":
    unittest.main()
