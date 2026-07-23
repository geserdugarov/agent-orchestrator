# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing full spec reader behavior."""

from __future__ import annotations

import unittest

from tests import implementing_full_spec_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
CLAUDE_ARGS = support.CLAUDE_ARGS
CLAUDE_SPEC = support.CLAUDE_SPEC
CODEX_ARGS = support.CODEX_ARGS
CODEX_SPEC = support.CODEX_SPEC
DEV_AGENT_KEY = support.DEV_AGENT_KEY
DEV_SESSION_ID = support.DEV_SESSION_ID
_FullSpecFixtureMixin = support._FullSpecFixtureMixin
workflow = support.workflow


class FullSpecSessionReaderTest(unittest.TestCase, _FullSpecFixtureMixin):
    def test_read_dev_session_round_trips_full_spec(self) -> None:
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={
                    DEV_AGENT_KEY: CODEX_SPEC,
                    DEV_SESSION_ID: "sid-y",
                },
            )
        )
        self.assertEqual(spec, CODEX_SPEC)
        self.assertEqual(backend, BACKEND_CODEX)
        self.assertEqual(args, CODEX_ARGS)
        self.assertEqual(sid, "sid-y")

    def test_read_dev_session_legacy_codex_session_id(self) -> None:
        # Even with a custom DEV_AGENT_SPEC in config, a legacy
        # codex_session_id-only state must yield codex with no args.
        self._enter(
            self._patch_dev_config(
                CLAUDE_SPEC,
                BACKEND_CLAUDE,
                CLAUDE_ARGS,
            )
        )
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={"codex_session_id": "legacy-sid"},
            )
        )
        self.assertEqual(spec, BACKEND_CODEX)
        self.assertEqual(backend, BACKEND_CODEX)
        self.assertEqual(args, ())
        self.assertEqual(sid, "legacy-sid")

    def test_unseeded_dev_session_uses_config(self) -> None:
        self._enter(
            self._patch_dev_config(
                CLAUDE_SPEC,
                BACKEND_CLAUDE,
                CLAUDE_ARGS,
            )
        )
        spec, backend, args, sid = workflow._read_dev_session(workflow.PinnedState())
        self.assertEqual(spec, CLAUDE_SPEC)
        self.assertEqual(backend, BACKEND_CLAUDE)
        self.assertEqual(args, CLAUDE_ARGS)
        self.assertIsNone(sid)
