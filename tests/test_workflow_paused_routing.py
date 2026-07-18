# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The `paused` control label is a hard skip on an in-flight issue: while it
is present the orchestrator runs no handler, consumes no slot, and records no
stage evaluation. It shares the `backlog` skip path (see
`github.hard_skip_control_label`), differing only in operator intent.

Removing the label is the whole resume protocol: the next poll picks the issue
back up from durable state (`test_removing_paused_allows_dispatch`). There is no
un-pause command -- `/orchestrator continue` is unrelated, replaying specific
`awaiting_human` parked retry flows rather than clearing `paused`. Applying
`paused` while an agent is mid-run is honored after the run returns, before its
post-agent side effects; those live-guard cases live in
`test_workflow_paused_agent_guard.py` and the per-stage `_paused` modules."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import workflow
from orchestrator.github import (
    BACKLOG_LABEL,
    PAUSED_LABEL,
    hard_skip_control_label,
)

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import _TEST_SPEC


class PausedLabelSkipsProcessingTest(unittest.TestCase):
    """`paused` freezes an already in-flight issue without discarding its
    state: applied to an `implementing` (or any) issue it stops the
    orchestrator from advancing the state machine until a human removes it.
    """

    def test_in_flight_paused_issue_skips_dispatch(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(752, label="implementing")
        issue.labels.append(FakeLabel(PAUSED_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        impl.assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])

    def test_unlabeled_issue_with_paused_skips_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(751)
        issue.labels.append(FakeLabel(PAUSED_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_removing_paused_allows_dispatch(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(753, label="implementing")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        impl.assert_called_once_with(gh, _TEST_SPEC, issue)


class HardSkipControlLabelTest(unittest.TestCase):
    """`hard_skip_control_label` is the single predicate every skip point
    consults; it reports which hard-skip label parked the issue so the skip
    log line names the operator's actual label."""

    def test_returns_none_without_a_hard_skip_label(self) -> None:
        # `community_contribution` is a control label that is not a hard skip:
        # it coexists with the workflow without parking the issue.
        issue = make_issue(760, label="implementing")
        issue.labels.append(FakeLabel("community_contribution"))
        self.assertIsNone(hard_skip_control_label(issue))

    def test_reports_the_present_hard_skip_label(self) -> None:
        for label in (BACKLOG_LABEL, PAUSED_LABEL):
            with self.subTest(label=label):
                issue = make_issue(761, label="implementing")
                issue.labels.append(FakeLabel(label))
                self.assertEqual(hard_skip_control_label(issue), label)


if __name__ == "__main__":
    unittest.main()
