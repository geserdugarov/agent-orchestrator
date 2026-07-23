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
import unittest

from tests.line_length_test_support import (
    _file_violations,
    _flagged_lines,
    _tracked_text_files,
)


_LineRuleCases = dict[str, tuple[str, list[int]]]
_TEST_LINE_LIMIT = 120
_LONG_PROSE_REPETITIONS = 40
_LONG_TOKEN_LENGTH = 200
_DIVIDER_WIDTH = 75
_ONE_BELOW_TEST_LIMIT = 119


def _line_rule_cases() -> _LineRuleCases:
    long_prose = "word " * _LONG_PROSE_REPETITIONS
    long_url = "".join(("https://example.com/", "a" * _LONG_TOKEN_LENGTH))
    wide_divider = "─" * _DIVIDER_WIDTH
    return {
        "short line": ("plain short line", []),
        "wrappable prose": (long_prose.rstrip(), [1]),
        "fenced block": (f"```\n{long_prose}\n```", []),
        "tilde fence": (f"~~~\n{long_prose}\n~~~", []),
        "unbreakable url": (long_url, []),
        "wide box drawing": (wide_divider, []),
        "exactly at limit": ("a" * _TEST_LINE_LIMIT, []),
        "one over limit": ("".join(("a ", "a" * _ONE_BELOW_TEST_LIMIT)), [1]),
    }


class TrackedFilesWithinLimitTest(unittest.TestCase):
    def test_no_tracked_text_file_exceeds_limit(self) -> None:
        violations = [
            violation
            for relative_path, path in _tracked_text_files()
            for violation in _file_violations(relative_path, path)
        ]
        violation_report = "\n".join(violations)
        self.assertEqual(
            violations,
            [],
            f"tracked text lines exceed the line-length limit:\n{violation_report}",
        )


class OverLimitRuleTest(unittest.TestCase):
    def test_rule_cases(self) -> None:
        for name, (text, expected) in _line_rule_cases().items():
            with self.subTest(case=name):
                self.assertEqual(
                    _flagged_lines(text, _TEST_LINE_LIMIT),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
