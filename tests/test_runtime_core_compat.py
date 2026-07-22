# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Compatibility contracts introduced by the runtime-core split."""
from __future__ import annotations

import inspect
import importlib
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

from orchestrator import (
    __version__ as imported_version,
    _github_labels,
    _github_pull_requests,
)
from orchestrator.github import GitHubClient, PinnedState
from orchestrator.scheduler import IssueScheduler, SubmissionRequest
from orchestrator.state_machine import WorkflowLabel, coerce_workflow_label

_REPO_SLUG = "owner/repo"
_ISSUE_NUMBER = 7
_VALIDATING_LABEL = "validating"
_EXPECTED_SUBMIT_SIGNATURE = (
    "(repo_slug, issue_number, fn, *, family=False, cap_exempt=False, "
    "per_repo_cap=None)"
)
_STATIC_HELPERS = (
    ("workflow_label", _github_labels.workflow_label),
    ("pr_has_label", _github_pull_requests.pr_has_label),
    ("pr_state", _github_pull_requests.pr_state),
    ("pr_is_mergeable", _github_pull_requests.pr_is_mergeable),
)
_ORCHESTRATOR_PACKAGE = importlib.import_module("orchestrator")


class PackageExportTest(unittest.TestCase):
    def test_version_import_surface(self) -> None:
        self.assertEqual(_ORCHESTRATOR_PACKAGE.__version__, imported_version)
        self.assertIn("__version__", _ORCHESTRATOR_PACKAGE.__dir__())

    def test_wildcard_import_exposes_only_the_version(self) -> None:
        command = "from orchestrator import *; print(__version__)"
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), imported_version)
        self.assertEqual(_ORCHESTRATOR_PACKAGE.__all__, ("__version__",))


class PinnedStateCompatibilityTest(unittest.TestCase):
    def test_keywords_share_data_attribute(self) -> None:
        state_data = {"branch": "orchestrator/issue-7"}
        descriptive_state = PinnedState(state_data=state_data)
        legacy_state = PinnedState(data=state_data)

        self.assertIs(descriptive_state.data, state_data)
        self.assertIs(legacy_state.state_data, state_data)

    def test_data_assignment_updates_internal_state(self) -> None:
        pinned_state = PinnedState()
        replacement = {"review_round": 2}

        pinned_state.data = replacement

        self.assertIs(pinned_state.state_data, replacement)

    def test_invalid_keywords(self) -> None:
        with self.assertRaises(TypeError):
            PinnedState(state_data={}, data={})
        with self.assertRaises(TypeError):
            PinnedState(payload={})


class GitHubStaticHelperCompatibilityTest(unittest.TestCase):
    def test_static_helper_identity(self) -> None:
        github_client = GitHubClient.__new__(GitHubClient)
        for attribute_name, module_function in _STATIC_HELPERS:
            with self.subTest(attribute_name=attribute_name):
                self.assertIs(
                    getattr(GitHubClient, attribute_name),
                    module_function,
                )
                self.assertIs(
                    getattr(github_client, attribute_name),
                    module_function,
                )


class SchedulerSubmissionCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = IssueScheduler(global_cap=1, per_repo_cap=1)

    def tearDown(self) -> None:
        self.scheduler.shutdown(wait=True)

    def test_typed_submission_request_dispatches(self) -> None:
        worker = MagicMock()
        request = SubmissionRequest(_REPO_SLUG, _ISSUE_NUMBER, worker)

        self.assertTrue(self.scheduler.submit(request))
        self.scheduler.shutdown(wait=True)
        worker.assert_called_once_with()

    def test_all_keyword_legacy_call_dispatches(self) -> None:
        worker = MagicMock()

        accepted = self.scheduler.submit(
            repo_slug=_REPO_SLUG,
            issue_number=_ISSUE_NUMBER,
            fn=worker,
        )

        self.assertTrue(accepted)
        self.scheduler.shutdown(wait=True)
        worker.assert_called_once_with()

    def test_legacy_signature_remains_introspectable(self) -> None:
        self.assertEqual(
            str(inspect.signature(self.scheduler.submit)),
            _EXPECTED_SUBMIT_SIGNATURE,
        )

    def test_typed_request_rejects_additional_fields(self) -> None:
        request = SubmissionRequest(_REPO_SLUG, _ISSUE_NUMBER, MagicMock())
        with self.assertRaises(TypeError):
            self.scheduler.submit(request, family=True)
        with self.assertRaises(TypeError):
            self.scheduler.submit(request, "unexpected", MagicMock())


class WorkflowLabelInputCompatibilityTest(unittest.TestCase):
    def test_descriptive_and_legacy_keywords_coerce(self) -> None:
        expected_label = WorkflowLabel.VALIDATING
        self.assertIs(
            coerce_workflow_label(label_name=_VALIDATING_LABEL),
            expected_label,
        )
        self.assertIs(
            coerce_workflow_label(value=_VALIDATING_LABEL),
            expected_label,
        )

    def test_invalid_keywords(self) -> None:
        with self.assertRaises(TypeError):
            coerce_workflow_label(_VALIDATING_LABEL, value="done")
        with self.assertRaises(TypeError):
            coerce_workflow_label(label=_VALIDATING_LABEL)


if __name__ == "__main__":
    unittest.main()
