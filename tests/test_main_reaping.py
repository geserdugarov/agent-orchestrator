# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Scheduler completion-queue reaping tests."""

import tempfile
import unittest

from orchestrator import workflow as _workflow
from tests import main_dispatch_execution as _execution
from tests import main_helpers as _helpers


class SchedulerReapingTest(unittest.TestCase):
    def test_single_repo_reaps_once_per_poll(self) -> None:
        with (
            _helpers.reload_main(_helpers._LEGACY_ENV) as main_module,
            _execution.dispatch_context(main_module, [_helpers._LEGACY_REPO]) as dispatch,
        ):
            reap = dispatch.run_and_capture_reap()
            self.assertEqual(reap.call_count, 1)

    def test_multi_repo_reaps_once_per_poll(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_root,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{temp_root}|main\nbeta/two|{temp_root}|develop"),
                },
            ) as main_module,
            _execution.dispatch_context(
                main_module,
                [_helpers._ALPHA_REPO, _helpers._BETA_REPO],
            ) as dispatch,
        ):
            reap = dispatch.run_and_capture_reap()
            self.assertEqual(reap.call_count, 1)

    def test_real_multi_repo_dispatch_reaps_once(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_root,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{temp_root}|main\nbeta/two|{temp_root}|develop"),
                },
            ) as main_module,
            _execution.dispatch_context(
                main_module,
                [_helpers._ALPHA_REPO, _helpers._BETA_REPO],
            ) as dispatch,
        ):
            reap = dispatch.run_real_and_capture_reap(_workflow)
            self.assertEqual(reap.call_count, 1)
