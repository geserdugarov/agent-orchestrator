# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop analytics-retention wiring tests."""

import tempfile
import unittest
from unittest.mock import patch

from tests import main_helpers as _helpers


class AnalyticsRetentionLoopWiringTest(unittest.TestCase):
    """`main._run_tick` calls `analytics.prune_with_retention_logging`
    once per tick so retention is actually applied. The wrapper itself
    (exception swallow, log message, no-GitHub-writes guarantee) is
    tested at the analytics boundary; the
    tests here only verify the wiring: main calls the wrapper exactly
    once per polling iteration regardless of repo count.
    """

    def test_single_repo_prunes_each_tick(self) -> None:
        # The legacy single-repo path stays in-thread and must still
        # call the prune wrapper so retention is actually applied.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR),
                patch.object(
                    main_mod.analytics,
                    "prune_with_retention_logging",
                ) as prune,
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)
                prune.assert_called_once_with()

            self.assertEqual(rc, 0)

    def test_multi_repo_prunes_once_per_tick(self) -> None:
        # The multi-repo path fans repo ticks out across a thread pool;
        # the wrapper runs once at the end (not once per repo) so the
        # observability sink is processed exactly once per polling
        # iteration regardless of how many repos are configured.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop"),
                }
            ) as main_mod,
        ):
            clients = _helpers._ClientFactory()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR),
                patch.object(
                    main_mod.analytics,
                    "prune_with_retention_logging",
                ) as prune,
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)
                prune.assert_called_once_with()

            self.assertEqual(rc, 0)
