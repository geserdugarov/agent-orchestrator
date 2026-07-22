# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Legacy workflow helper signatures at typed context boundaries."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator import base_sync, workflow
from orchestrator.stages import implementing


class WorkflowCompatibilityAdapterTest(unittest.TestCase):
    def test_base_sync_adapter_accepts_historical_keywords(self) -> None:
        gh = Mock()
        gh.workflow_label.return_value = "validating"
        state = Mock()
        state.get.return_value = "pre-rebase"
        with patch.object(base_sync, "_sync_pr_worktree_context") as run:
            base_sync._sync_pr_worktree_to_base(
                gh=gh,
                spec="spec",
                issue="issue",
                state=state,
                worktree=Path("worktree"),
                pr_number=31,
                behind=2,
            )

        context = run.call_args.args[0]
        self.assertEqual(context.pr_number, 31)
        self.assertEqual(context.behind, 2)
        self.assertEqual(context.label, "validating")
        self.assertEqual(context.pending_pre_rebase_sha, "pre-rebase")

    def test_recovery_adapter_applies_historical_defaults(self) -> None:
        with patch.object(
            base_sync,
            "_recover_pending_auto_base_rebase_context",
            return_value=True,
        ) as recover:
            recovered = base_sync._recover_pending_auto_base_rebase(
                "gh",
                "spec",
                "issue",
                "state",
                Path("worktree"),
                pr_number=41,
                label="validating",
                pending_pre_rebase_sha="before",
            )

        self.assertTrue(recovered)
        context = recover.call_args.args[0]
        self.assertEqual(context.behind, 0)
        self.assertIsNone(context.unparking_consumed_max)

    def test_conflict_route_adapter_builds_typed_context(self) -> None:
        with patch.object(
            base_sync,
            "_route_pr_worktree_conflict_context",
        ) as route:
            base_sync._route_pr_worktree_to_resolving_conflict(
                "gh",
                "spec",
                "issue",
                "state",
                51,
                label="in_review",
                behind=3,
                conflicted_files=["one.py"],
                pr_head_sha="head",
            )

        context = route.call_args.args[0]
        self.assertEqual(context.pr_number, 51)
        self.assertEqual(context.conflicted_files, ["one.py"])
        self.assertEqual(context.pr_head_sha, "head")

    def test_developer_resume_adapter_preserves_options(self) -> None:
        execution = Mock()
        execution.execute.return_value = (Path("worktree"), "result", False)
        with patch.object(
            implementing._DevResumeContext,
            "build",
            return_value=execution,
        ) as build:
            result = workflow._resume_dev_with_text(
                "gh",
                "spec",
                "issue",
                "state",
                "continue",
                stage="fixing",
                pause_guard=True,
            )

        self.assertEqual(result, execution.execute.return_value)
        request = build.call_args.args[0]
        self.assertEqual(request.resume_args, ("state", "continue"))
        self.assertEqual(request.option_fields, {"pause_guard": True})
        self.assertEqual(request.stage, "fixing")

    def test_adapters_report_their_historical_signatures(self) -> None:
        expected_signatures = {
            base_sync._sync_pr_worktree_to_base: (
                "(gh, spec, issue, state, worktree, pr_number, behind)"
            ),
            base_sync._recover_pending_auto_base_rebase: (
                "(gh, spec, issue, state, worktree, *, pr_number, label, "
                "pending_pre_rebase_sha, behind=0, unparking_consumed_max=None)"
            ),
            base_sync._route_pr_worktree_to_resolving_conflict: (
                "(gh, spec, issue, state, pr_number, *, label, behind, "
                "conflicted_files, pr_head_sha)"
            ),
            workflow._resume_dev_with_text: (
                "(gh, spec, issue, *resume_args, stage=None, **option_fields)"
            ),
        }
        for adapter, expected in expected_signatures.items():
            with self.subTest(adapter=adapter.__name__):
                self.assertEqual(
                    str(inspect.signature(adapter)),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
