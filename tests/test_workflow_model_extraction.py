# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`_configured_model` extracts the analytics fallback model from an
`extra_args` tuple, accepting both the split (`-m gpt-5`) and `=`-glued
(`--model=opus-4`) shapes and ignoring flags that don't match the
backend's CLI surface."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow


class ConfiguredModelExtractionTest(unittest.TestCase):
    """`_configured_model` is the tiny shim that converts an `extra_args`
    tuple into the model fallback `usage.parse_agent_usage` consumes.
    Both the split (`-m gpt-5`) and `=`-glued (`--model=opus-4`) shapes
    must survive because `shlex.split` produces either depending on the
    operator's quoting.
    """

    def test_codex_dash_m_split_form(self) -> None:
        self.assertEqual(
            workflow._configured_model("codex", ("-m", "gpt-5-codex")),
            "gpt-5-codex",
        )

    def test_codex_dash_m_equals_form(self) -> None:
        self.assertEqual(
            workflow._configured_model("codex", ("-m=gpt-5-codex",)),
            "gpt-5-codex",
        )

    def test_claude_long_flag_split_form(self) -> None:
        self.assertEqual(
            workflow._configured_model(
                "claude", ("--model", "claude-opus-4-7"),
            ),
            "claude-opus-4-7",
        )

    def test_claude_long_flag_equals_form(self) -> None:
        self.assertEqual(
            workflow._configured_model(
                "claude", ("--model=claude-opus-4-7",),
            ),
            "claude-opus-4-7",
        )

    def test_returns_none_when_flag_absent(self) -> None:
        # No `-m` / `--model` in the spec -- the parser keeps its
        # "unknown" handling rather than receiving an empty string.
        self.assertIsNone(workflow._configured_model("codex", ()))
        self.assertIsNone(
            workflow._configured_model("claude", ("--effort", "high")),
        )

    def test_codex_ignores_claude_flag(self) -> None:
        # `--model` is a claude flag; for a codex spec the helper must
        # not pick it up. If an operator typed the wrong flag for the
        # wrong backend, the analytics fallback stays empty rather than
        # mislabeling.
        self.assertIsNone(
            workflow._configured_model(
                "codex", ("--model", "gpt-5-codex"),
            ),
        )

    def test_trailing_flag_without_value_returns_none(self) -> None:
        # Defensive: a stray `-m` at the end of extra_args (which a
        # bad spec could produce) must not raise.
        self.assertIsNone(workflow._configured_model("codex", ("-m",)))
