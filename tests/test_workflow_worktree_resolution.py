# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator import workflow

from tests.worktree_path_test_support import (
    _migration_spec,
    _state,
)

BASE_BRANCH = "main"
MIGRATION_REPO_SLUG = "geserdugarov/agent-orchestrator"
MIGRATION_TARGET_ROOT = Path("/tmp/x")
ALICE_REPO_SLUG = "alice/repo"
LOCK_SUFFIX_SLUG = "owner/foo.lock"
DOUBLE_DOT_SLUG = "owner/foo..bar"
BRANCH_KEY = "branch"
LEGACY_BRANCH = "orchestrator/issue-7"
NAMESPACED_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-7"
STAGE_LAYOUT_ISSUE_NUMBER = 11
SHARED_BRANCH_ISSUE_NUMBER = 15
PR_NUMBER = 42


class ResolveBranchNamePinnedTest(unittest.TestCase):
    """In-flight issues that were already in the orchestrator before
    branches were slug-namespaced have `state["branch"]` pinned to the
    legacy `orchestrator/issue-<n>` value and a live PR open against
    that head. `_resolve_branch_name` honors the pinned value so the
    orchestrator stays anchored on the existing PR -- otherwise we
    would (a) fail to find the PR by branch on lookup, (b) push to a
    brand-new slug-namespaced branch, and (c) orphan the original.
    Fresh issues with no pinned branch fall back to the new namespaced
    form so the cross-repo collision the slug-namespacing fixes does
    not regress for new work.
    """

    def test_pinned_legacy_branch_is_honored(self) -> None:
        spec = _migration_spec()
        state = _state({BRANCH_KEY: LEGACY_BRANCH})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            LEGACY_BRANCH,
        )

    def test_no_pinned_uses_namespaced_default(self) -> None:
        spec = _migration_spec()
        state = _state({})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            NAMESPACED_BRANCH,
        )

    def test_outside_namespace_pin_is_ignored(
        self,
    ) -> None:
        # A corrupted / foreign pinned `branch` value must not redirect
        # the resolver at an arbitrary ref -- the `orchestrator/` prefix
        # check keeps `_cleanup_terminal_branch`'s "orchestrator-owned
        # namespace" invariant intact.
        spec = _migration_spec()
        state = _state({BRANCH_KEY: "feature/foreign-branch"})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            NAMESPACED_BRANCH,
        )

    def test_pinned_namespaced_branch_round_trips(self) -> None:
        # Once the resolver computed and persisted the new form, a later
        # tick honors it unchanged.
        spec = _migration_spec()
        state = _state(
            {
                BRANCH_KEY: "orchestrator/geserdugarov__agent-orchestrator/issue-9",
            }
        )
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 9),
            "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        )

    def test_non_string_pinned_branch_falls_back(self) -> None:
        spec = _migration_spec()
        for bad in (None, PR_NUMBER, [LEGACY_BRANCH]):
            state = _state({BRANCH_KEY: bad})
            self.assertEqual(
                workflow._resolve_branch_name(state, spec, 7),
                NAMESPACED_BRANCH,
                f"bad pinned value {bad!r} did not fall back",
            )


class ResolveBranchNamePrMigrationTest(unittest.TestCase):
    def test_unpinned_legacy_pr_uses_legacy_ref(self) -> None:
        # Pre-slug-namespacing in-flight PR: pinned state recorded
        # `pr_number` but no `branch` (the early implementations did
        # not always persist `branch`). The live PR head is on the
        # legacy `orchestrator/issue-N` ref because that is the only
        # form the orchestrator ever produced before this change. The
        # resolver MUST infer that ref so the next tick anchors on
        # the existing PR; without the fallback it would target the
        # new slug-namespaced branch, push there, open a duplicate
        # PR, and orphan the original.
        spec = _migration_spec()
        state = _state({"pr_number": PR_NUMBER})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            LEGACY_BRANCH,
        )

    def test_pinned_legacy_pr_honors_pin(self) -> None:
        # Belt-and-suspenders: a legacy in-flight PR that DID persist
        # `branch` (the consistent half of the pre-slug-namespacing
        # behavior) is still resolved via the pinned value, not via
        # the pr_number fallback -- the two cases agree on the legacy
        # form, but the pinned-value path is more specific.
        spec = _migration_spec()
        state = _state(
            {
                "pr_number": PR_NUMBER,
                BRANCH_KEY: LEGACY_BRANCH,
            }
        )
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            LEGACY_BRANCH,
        )

    def test_fresh_pr_namespaced_pin_wins(self) -> None:
        # A PR opened AFTER slug-namespacing landed has both
        # `pr_number` and the namespaced `branch` set. The
        # pr_number-fallback must not override the pinned value, or
        # every new PR would silently route through the legacy ref.
        spec = _migration_spec()
        state = _state(
            {
                "pr_number": PR_NUMBER,
                BRANCH_KEY: NAMESPACED_BRANCH,
            }
        )
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            NAMESPACED_BRANCH,
        )


if __name__ == "__main__":
    unittest.main()
