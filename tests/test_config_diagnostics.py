# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration diagnostic helper tests."""

import unittest


class ConfigDiagnosticsTest(unittest.TestCase):
    """Configuration failures and warnings funnel through two helpers:
    `_config_error` aborts import (SystemExit carrying the message, exit
    code 1) and `_config_warning` emits a non-fatal line to stderr without
    touching stdout. Every import-time validation and dotenv warning path
    routes through these, so the message, exit code, and stream are pinned
    here at the producer level.
    """

    def test_config_error_carries_message_and_code(self) -> None:
        from orchestrator.config import _config_error

        error_context = self.assertRaises(SystemExit)
        with error_context:
            _config_error("orchestrator: bad config")
        # `str(exc)` is what the import-time validation tests assert on; a
        # string code exits the process with status 1.
        self.assertEqual(str(error_context.exception), "orchestrator: bad config")
        self.assertEqual(error_context.exception.code, "orchestrator: bad config")

    def test_config_warning_writes_to_stderr_only(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from orchestrator.config import _config_warning

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            _config_warning("orchestrator: heads up")
        self.assertEqual(err.getvalue(), "orchestrator: heads up\n")
        self.assertEqual(out.getvalue(), "")
