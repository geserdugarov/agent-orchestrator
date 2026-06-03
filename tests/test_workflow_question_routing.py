# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`question` label bootstrap + dispatcher routing. Handler-behavior
tests for the question stage live in `tests/test_workflow_question.py`;
this module pins only the label-spec / family-aware / dispatcher wiring
that keeps the dispatcher from falling through to pickup or
implementing on a `question`-labeled issue."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import _TEST_SPEC


class QuestionLabelRoutingTest(unittest.TestCase):
    """`question` is a first-class workflow label routed to its own stage
    handler. The behavioral tests for that handler live in
    `tests/test_workflow_question.py`; this class only covers label
    bootstrapping and dispatcher routing.
    """

    def test_question_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("question", WORKFLOW_LABELS)

    def test_question_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("question", names)

    def test_question_label_is_not_family_aware(self) -> None:
        # Open `question` issues touch only their own pinned state, so the
        # label must stay out of `_FAMILY_AWARE_LABELS` -- otherwise the
        # parallel tick path would route it through the single-threaded
        # family bucket and defeat fan-out concurrency.
        self.assertNotIn("question", workflow._FAMILY_AWARE_LABELS)

    def test_dispatcher_routes_question_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(801, label="question")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_question") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
