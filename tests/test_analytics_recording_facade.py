# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics recording facade compatibility tests."""

import unittest


from unittest.mock import patch

from tests.analytics_reload_helpers import reload_analytics as _reload

_APPEND_RECORD_MEMBER = 'append_record'


_REPO_SHORT = "o/r"


_STAGE_IMPLEMENTING = "implementing"


_STAGE_ENTER = "stage_enter"


class RecordingFacadeTest(unittest.TestCase):
    """The event-recording implementation lives in
    `orchestrator.analytics._recording`, the opt-in trajectory sink in
    `orchestrator.analytics._trajectories`, and the by-age retention prune
    entry points in `orchestrator.analytics._retention`; the package
    re-exports all three as a facade, each package instance carries its own
    submodules, and the recorders read sink knobs / call sibling recorders
    back off the facade, so a reference held across a `_reload` keeps
    dispatching to the instance its own callers patched.
    """

    def test_recorders_defined_in_recording_module(self) -> None:
        _, analytics = _reload()
        for name in (
            _APPEND_RECORD_MEMBER,
            "build_record",
            "record_agent_exit",
            "record_repo_skill_catalog",
            "record_stage_enter",
            "record_stage_evaluation",
        ):
            with self.subTest(name=name):
                member = getattr(analytics, name)
                self.assertEqual(
                    member.__module__,
                    "orchestrator.analytics._recording",
                )
                self.assertIs(member, getattr(analytics._recording, name))

    def test_trajectory_recorder_defined_in_submodule(self) -> None:
        _, analytics = _reload()
        for name in ("append_trajectory_record",):
            with self.subTest(name=name):
                member = getattr(analytics, name)
                self.assertEqual(
                    member.__module__,
                    "orchestrator.analytics._trajectories",
                )
                self.assertIs(member, getattr(analytics._trajectories, name))

    def test_prune_entry_points_in_retention_module(self) -> None:
        _, analytics = _reload()
        for name in (
            "prune_old_records",
            "prune_trajectory_records",
            "prune_with_retention_logging",
        ):
            with self.subTest(name=name):
                member = getattr(analytics, name)
                self.assertEqual(
                    member.__module__,
                    "orchestrator.analytics._retention",
                )
                self.assertIs(member, getattr(analytics._retention, name))

    def test_internal_append_routes_via_facade(self) -> None:
        # A recorder's internal `append_record` is late-bound through the
        # facade, so patching `analytics.append_record` intercepts it.
        _, analytics = _reload()
        captured: list[dict] = []
        with patch.object(analytics, _APPEND_RECORD_MEMBER, captured.append):
            analytics.record_stage_enter(
                repo=_REPO_SHORT,
                issue=1,
                stage=_STAGE_IMPLEMENTING,
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["event"], _STAGE_ENTER)

    def test_reload_keeps_stale_facade_reference(self) -> None:
        # A holder that imported the package before a `_reload` keeps its own
        # instance: its recorders read the knobs patched on THAT instance, not
        # the freshly reloaded one that now sits in `sys.modules`.
        _, stale = _reload()
        captured_stale: list[dict] = []
        stale_patch = patch.object(stale, _APPEND_RECORD_MEMBER, captured_stale.append)
        stale_patch.start()
        self.addCleanup(stale_patch.stop)
        _, fresh = _reload()
        self.assertIsNot(fresh, stale)
        captured_fresh: list[dict] = []
        with patch.object(fresh, _APPEND_RECORD_MEMBER, captured_fresh.append):
            fresh.record_stage_enter(repo=_REPO_SHORT, issue=2, stage="fixing")
        stale.record_stage_enter(
            repo=_REPO_SHORT,
            issue=1,
            stage=_STAGE_IMPLEMENTING,
        )
        self.assertEqual([rec["issue"] for rec in captured_fresh], [2])
        self.assertEqual([rec["issue"] for rec in captured_stale], [1])
