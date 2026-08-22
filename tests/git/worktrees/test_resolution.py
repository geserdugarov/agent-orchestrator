# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pinned and legacy branch resolution, and the names an issue can be on."""

from __future__ import annotations

import unittest

from orchestrator.git.worktrees import paths

from tests.git.worktrees.path_test_support import (
    BRANCH_KEY,
    LEGACY_BRANCH,
    NAMESPACED_BRANCH,
    PR_NUMBER,
    _migration_spec,
    _state,
)

PR_NUMBER_KEY = "pr_number"


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
            paths._resolve_branch_name(state, spec, 7),
            LEGACY_BRANCH,
        )

    def test_no_pinned_uses_namespaced_default(self) -> None:
        spec = _migration_spec()
        state = _state({})
        self.assertEqual(
            paths._resolve_branch_name(state, spec, 7),
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
            paths._resolve_branch_name(state, spec, 7),
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
            paths._resolve_branch_name(state, spec, 9),
            "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        )

    def test_non_string_pinned_branch_falls_back(self) -> None:
        spec = _migration_spec()
        for bad in (None, PR_NUMBER, [LEGACY_BRANCH]):
            state = _state({BRANCH_KEY: bad})
            self.assertEqual(
                paths._resolve_branch_name(state, spec, 7),
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
        state = _state({PR_NUMBER_KEY: PR_NUMBER})
        self.assertEqual(
            paths._resolve_branch_name(state, spec, 7),
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
                PR_NUMBER_KEY: PR_NUMBER,
                BRANCH_KEY: LEGACY_BRANCH,
            }
        )
        self.assertEqual(
            paths._resolve_branch_name(state, spec, 7),
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
                PR_NUMBER_KEY: PR_NUMBER,
                BRANCH_KEY: NAMESPACED_BRANCH,
            }
        )
        self.assertEqual(
            paths._resolve_branch_name(state, spec, 7),
            NAMESPACED_BRANCH,
        )


class IssueBranchNamesTest(unittest.TestCase):
    """Every name this orchestrator could have published one issue under.

    Asked by callers that DELETE a recorded branch, so what matters is that
    the answer is the exact pair and nothing shaped like it: another
    repository's branch for an issue with the same number is in the same
    `orchestrator/` namespace and ends in the same `/issue-<n>` tail, and two
    specs sharing a `target_root` is the case namespacing exists for.
    """

    def test_it_names_the_current_and_legacy_forms(self) -> None:
        self.assertEqual(
            paths._issue_branch_names(_migration_spec(), 7),
            (NAMESPACED_BRANCH, LEGACY_BRANCH),
        )

    def test_another_repos_branch_is_not_listed(self) -> None:
        names = paths._issue_branch_names(_migration_spec(), 7)

        self.assertNotIn("orchestrator/other-repository/issue-7", names)

    def test_the_resolver_uses_a_name_it_lists(self) -> None:
        # The two are one fact: a branch the resolver would publish this issue
        # on has to be one the ownership test recognizes, or the reclamation
        # refuses to delete what the publication just made.
        spec = _migration_spec()

        self.assertIn(
            paths._resolve_branch_name(_state(), spec, 7),
            paths._issue_branch_names(spec, 7),
        )


if __name__ == "__main__":
    unittest.main()
