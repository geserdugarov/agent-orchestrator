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

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Ruff already lints Python; scanning it here would double-report and
# ignore Ruff's own inline suppression directives.
_RUFF_OWNED = {".py"}
# Binary assets carry no line concept.
_BINARY_EXT = {".png"}
# Machine-generated / verbatim content that is never hand-wrapped.
_IGNORED_NAMES = {"uv.lock", "LICENSE"}


def _load_limit() -> int:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return int(data["tool"]["ruff"]["line-length"])


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
    files = []
    for rel in filter(None, out.split("\0")):
        path = _REPO_ROOT / rel
        if path.suffix in _RUFF_OWNED or path.suffix in _BINARY_EXT:
            continue
        if path.name in _IGNORED_NAMES:
            continue
        files.append((rel, path))
    return files


def over_limit_lines(text: str, limit: int = LIMIT):
    """Yield (lineno, length) for wrappable lines longer than `limit`.

    Length is counted in Unicode code points, so multi-byte box-drawing
    art is measured by display width rather than byte count. Lines inside
    fenced code blocks are skipped, and a line whose overflow is a single
    unbreakable token (e.g. a long URL) is exempt because no wrapping can
    bring it under the limit.
    """
    in_fence = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = raw.rstrip()
        if len(line) <= limit:
            continue
        if max((len(token) for token in line.split()), default=0) > limit:
            continue
        yield lineno, len(line)


class TrackedFilesWithinLimitTest(unittest.TestCase):
    def test_no_tracked_text_file_exceeds_limit(self) -> None:
        violations = []
        for rel, path in _tracked_text_files():
            data = path.read_bytes()
            if b"\x00" in data:  # binary without a flagged extension
                continue
            text = data.decode("utf-8")
            for lineno, length in over_limit_lines(text):
                violations.append(f"{rel}:{lineno} ({length} > {LIMIT} chars)")
        self.assertEqual(
            violations,
            [],
            "tracked text lines exceed the line-length limit:\n" + "\n".join(violations),
        )


class OverLimitRuleTest(unittest.TestCase):
    def _flagged(self, text: str) -> list[int]:
        return [lineno for lineno, _ in over_limit_lines(text, limit=120)]

    def test_rule_cases(self) -> None:
        long_prose = "word " * 40  # ~200 chars, all short tokens: wrappable
        long_url = "https://example.com/" + "a" * 200  # one unbreakable token
        wide_divider = "─" * 75  # 75 code points, 225 bytes: under limit
        cases = {
            "short line": ("plain short line", []),
            "wrappable prose": (long_prose.rstrip(), [1]),
            "fenced block": (f"```\n{long_prose}\n```", []),
            "tilde fence": (f"~~~\n{long_prose}\n~~~", []),
            "unbreakable url": (long_url, []),
            "wide box drawing": (wide_divider, []),
            "exactly at limit": ("a" * 120, []),
            "one over limit": ("a " + "a" * 119, [1]),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self._flagged(text), expected)


if __name__ == "__main__":
    unittest.main()
