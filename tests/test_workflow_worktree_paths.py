# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from orchestrator import config, workflow
from orchestrator.github import PinnedState

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


def _spec(repo_slug: str) -> config.RepoSpec:
    return config.RepoSpec(
        slug=repo_slug,
        target_root=Path(f"/tmp/{workflow._sanitize_slug(repo_slug)}-target"),
        base_branch=BASE_BRANCH,
    )


def _branch(repo_slug: str, issue_number: int = 1) -> str:
    return workflow._branch_name(_spec(repo_slug), issue_number)


def _migration_spec() -> config.RepoSpec:
    return config.RepoSpec(
        slug=MIGRATION_REPO_SLUG,
        target_root=MIGRATION_TARGET_ROOT,
        base_branch=BASE_BRANCH,
    )


def _state(state_data=None) -> PinnedState:
    return PinnedState(comment_id=None, data=dict(state_data or {}))


class WorktreePathSlugNamespaceTest(unittest.TestCase):
    """Two repos with the same issue number must produce distinct worktree
    paths, otherwise simultaneous orchestration of both would have them
    fighting over the same `WORKTREES_DIR/issue-N` checkout. The slug
    sanitizer also has to produce a single filesystem-safe segment
    (no `/`, no leading `.`) since it becomes a directory name.
    """

    def test_distinct_slugs_same_number_never_collide(self) -> None:
        spec_a = _spec(ALICE_REPO_SLUG)
        spec_b = _spec("bob/repo")
        path_a = workflow._worktree_path(spec_a, 7)
        path_b = workflow._worktree_path(spec_b, 7)

        self.assertNotEqual(path_a, path_b)
        # Both must live under WORKTREES_DIR with the issue-N leaf.
        self.assertEqual(path_a.name, "issue-7")
        self.assertEqual(path_b.name, "issue-7")
        self.assertEqual(path_a.parent.parent, config.WORKTREES_DIR)
        self.assertEqual(path_b.parent.parent, config.WORKTREES_DIR)

    def test_decompose_path_also_namespaced_by_slug(self) -> None:
        spec_a = _spec(ALICE_REPO_SLUG)
        spec_b = _spec("bob/repo")
        self.assertNotEqual(
            workflow._decompose_worktree_path(spec_a, 7),
            workflow._decompose_worktree_path(spec_b, 7),
        )

    def test_stages_share_repo_namespace(self) -> None:
        # `WORKTREES_DIR/<slug>/issue-N` and `WORKTREES_DIR/<slug>/decompose-N`
        # share the per-repo subdirectory so cleanup on the parent dir
        # also reaps the decomposer scratch.
        spec = _spec("owner/name")
        impl = workflow._worktree_path(spec, STAGE_LAYOUT_ISSUE_NUMBER)
        dec = workflow._decompose_worktree_path(spec, STAGE_LAYOUT_ISSUE_NUMBER)
        self.assertEqual(impl.parent, dec.parent)


class SanitizeSlugTest(unittest.TestCase):
    def test_sanitize_slug_replaces_owner_separator(self) -> None:
        self.assertEqual(workflow._sanitize_slug("owner/name"), "owner__name")

    def test_sanitize_slug_is_a_single_segment(self) -> None:
        # A directory name with `/` would split into nested directories,
        # defeating the point of namespacing.
        for raw in (
            "owner/name",
            "deep/owner/name",
            "name-only",
            "weird name with spaces",
        ):
            cleaned = workflow._sanitize_slug(raw)
            self.assertNotIn("/", cleaned, f"slug={raw!r} -> {cleaned!r}")

    def test_sanitize_slug_no_leading_dot(self) -> None:
        # Hidden directories (.foo) hide the worktree from a casual
        # operator inspection; escape leading dots.
        self.assertFalse(workflow._sanitize_slug(".dotfile/repo").startswith("."))
        self.assertFalse(workflow._sanitize_slug("./repo").startswith("."))

    def test_sanitize_slug_strips_unsafe_chars(self) -> None:
        cleaned = workflow._sanitize_slug("owner@#$/name with spaces")
        # No path separator, no shell-special chars; only [A-Za-z0-9_.-]
        for ch in cleaned:
            self.assertTrue(
                ch.isalnum() or ch in "_.-",
                f"unexpected char {ch!r} in {cleaned!r}",
            )

    def test_sanitize_slug_empty_input_falls_back(self) -> None:
        # Empty would collapse `WORKTREES_DIR/<slug>/issue-N` into
        # `WORKTREES_DIR/issue-N`, reintroducing the cross-repo collision.
        self.assertNotEqual(workflow._sanitize_slug(""), "")
        self.assertNotEqual(workflow._sanitize_slug(""), ".")

    def test_default_repo_spec_path_format(self) -> None:
        # Anchor the documented `<owner>__<name>/issue-N` layout.
        spec = config.RepoSpec(
            slug=MIGRATION_REPO_SLUG,
            target_root=MIGRATION_TARGET_ROOT,
            base_branch=BASE_BRANCH,
        )
        path = workflow._worktree_path(spec, 9)
        self.assertEqual(
            path,
            config.WORKTREES_DIR / "geserdugarov__agent-orchestrator" / "issue-9",
        )


class BranchNameSlugNamespaceTest(unittest.TestCase):
    """Two RepoSpecs that share the same `target_root` (a single local
    clone with multiple remotes) would otherwise collide on
    `orchestrator/issue-N` because git refuses to check the same branch
    out in two worktrees of one repo. `_branch_name` includes the
    sanitized slug so each spec lives on its own branch.
    """

    def test_same_number_distinct_slugs_make_branches(self) -> None:
        spec_a = config.RepoSpec(
            slug="geserdugarov/lance-open-source",
            target_root=Path("/tmp/shared-clone"),
            base_branch=BASE_BRANCH,
        )
        spec_b = config.RepoSpec(
            slug="geserdugarov/lance-private",
            target_root=Path("/tmp/shared-clone"),
            base_branch=BASE_BRANCH,
        )

        self.assertNotEqual(
            workflow._branch_name(spec_a, SHARED_BRANCH_ISSUE_NUMBER),
            workflow._branch_name(spec_b, SHARED_BRANCH_ISSUE_NUMBER),
        )

    def test_branch_name_format(self) -> None:
        spec = config.RepoSpec(
            slug=MIGRATION_REPO_SLUG,
            target_root=MIGRATION_TARGET_ROOT,
            base_branch=BASE_BRANCH,
        )
        self.assertEqual(
            workflow._branch_name(spec, 9),
            "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        )

    def test_branch_name_keeps_orchestrator_prefix(self) -> None:
        # `_cleanup_terminal_branch` relies on the `orchestrator/` prefix
        # to constrain what branches it is willing to delete.
        for repo_slug in (ALICE_REPO_SLUG, "bob/repo", "weird name/x"):
            spec = config.RepoSpec(
                slug=repo_slug,
                target_root=MIGRATION_TARGET_ROOT,
                base_branch=BASE_BRANCH,
            )
            self.assertTrue(
                workflow._branch_name(spec, PR_NUMBER).startswith("orchestrator/"),
                repo_slug,
            )


class SanitizeBranchSegmentTest(unittest.TestCase):
    """`_sanitize_branch_segment` must produce a string `git
    check-ref-format` accepts -- not just a filesystem-safe segment.
    The filesystem-only `_sanitize_slug` happily yields
    `owner__foo.lock` / `owner__foo..bar` / `owner__foo.` for valid
    configured `REPOS` slugs, but git rejects all three (reserved
    `.lock` suffix, `..` anywhere, trailing dot). Without the
    git-ref-safe variant, fresh issues for those repos would fail at
    `git worktree add -b ...` before any PR could be created.
    """

    def test_dot_lock_suffix_is_rewritten(self) -> None:
        # `.lock` is replaced by `_lock`, then a `__h<digest>`
        # injectivity suffix is appended because the ref-only rewrite
        # is information-lossy (`foo.lock` and `foo_lock` would
        # otherwise collide).
        out = workflow._sanitize_branch_segment(LOCK_SUFFIX_SLUG)
        self.assertTrue(
            out.startswith("owner__foo_lock__h"),
            f"unexpected sanitized form: {out!r}",
        )
        # 16-hex-char suffix after the marker, full segment is
        # git-ref-safe.
        self.assertRegex(out, r"^owner__foo_lock__h[0-9a-f]{16}$")

    def test_double_dot_collapses_to_underscore(self) -> None:
        out = workflow._sanitize_branch_segment(DOUBLE_DOT_SLUG)
        self.assertRegex(out, r"^owner__foo_bar__h[0-9a-f]{16}$")
        # Triple+ dot runs collapse to a single `_` too.
        out3 = workflow._sanitize_branch_segment("a/...b")
        self.assertRegex(out3, r"^a___b__h[0-9a-f]{16}$")

    def test_trailing_dot_is_rewritten(self) -> None:
        out = workflow._sanitize_branch_segment("owner/foo.")
        self.assertRegex(out, r"^owner__foo___h[0-9a-f]{16}$")

    def test_ordinary_slugs_round_trip(self) -> None:
        # The common case (no .lock, no .., no trailing dot) must
        # produce the same sanitized form as `_sanitize_slug` so the
        # branch and the worktree path stay readable in tandem. No
        # injectivity suffix is appended because the filesystem-safe
        # form is already git-ref-safe.
        for repo_slug in (
            MIGRATION_REPO_SLUG,
            ALICE_REPO_SLUG,
            "acme/widget-private",
        ):
            self.assertEqual(
                workflow._sanitize_branch_segment(repo_slug),
                workflow._sanitize_slug(repo_slug),
                repo_slug,
            )

    def test_distinct_slugs_produce_distinct_branches(self) -> None:
        # Injectivity regression: two slugs whose ref-only rewrites
        # collapse to the same shape (e.g. `foo.lock` <-> `foo_lock`)
        # must still produce distinct branch segments, otherwise two
        # `REPOS` entries sharing a `target_root` would collide on
        # the same branch and the slug-namespacing fix would regress
        # for those slug shapes.
        ambiguous_pairs = [
            (LOCK_SUFFIX_SLUG, "owner/foo_lock"),
            (DOUBLE_DOT_SLUG, "owner/foo_bar"),
            ("owner/foo.", "owner/foo_"),
            ("owner/foo...bar", "owner/foo_bar"),
            ("owner/...", "owner/__"),
        ]
        for first_slug, second_slug in ambiguous_pairs:
            seg_a = workflow._sanitize_branch_segment(first_slug)
            seg_b = workflow._sanitize_branch_segment(second_slug)
            self.assertNotEqual(
                seg_a, seg_b,
                f"slugs {first_slug!r} and {second_slug!r} both produced "
                f"{seg_a!r}",
            )

    def test_hash_suffix_is_deterministic(self) -> None:
        # The injectivity suffix is content-derived, so a given slug
        # always produces the same branch -- a stage handler must be
        # able to recompute the branch on every tick without needing
        # to read prior state.
        repo_slug = LOCK_SUFFIX_SLUG
        self.assertEqual(
            workflow._sanitize_branch_segment(repo_slug),
            workflow._sanitize_branch_segment(repo_slug),
        )


class BranchRefFormatTest(unittest.TestCase):
    def test_git_accepts_pathological_slug_branch(
        self,
    ) -> None:
        # Verify against the actual git binary: every branch the
        # sanitizer produces for a known-pathological slug must pass
        # `git check-ref-format --branch`. This is the bug the
        # filesystem-only sanitizer would smuggle through to the
        # first `git worktree add`.
        pathological = [
            LOCK_SUFFIX_SLUG,
            DOUBLE_DOT_SLUG,
            "owner/foo.",
            "owner/.foo",
            "owner/foo.lock.lock",
            "owner/.lock",
            "owner/foo...bar",
            "a/b.lock",
            # Inputs whose ambiguous siblings the injectivity suffix
            # must distinguish -- still git-ref-safe.
            "owner/foo_lock",
            "owner/foo_bar",
            "owner/foo_",
        ]
        for repo_slug in pathological:
            branch = _branch(repo_slug, 1)
            git_result = subprocess.run(
                ["git", "check-ref-format", "--branch", branch],
                capture_output=True, text=True,
            )
            self.assertEqual(
                git_result.returncode, 0,
                f"slug={repo_slug!r} produced invalid branch "
                f"{branch!r}: stderr={git_result.stderr!r}",
            )

    def test_branch_name_uses_branch_safe_segment(self) -> None:
        # `_branch_name` itself must route through the branch-safe
        # sanitizer, not the filesystem-only one -- regression guard
        # for the bug.
        self.assertRegex(
            _branch(LOCK_SUFFIX_SLUG, 7),
            r"^orchestrator/owner__foo_lock__h[0-9a-f]{16}/issue-7$",
        )
        self.assertRegex(
            _branch(DOUBLE_DOT_SLUG, 7),
            r"^orchestrator/owner__foo_bar__h[0-9a-f]{16}/issue-7$",
        )


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
        state = _state({
            BRANCH_KEY: "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        })
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
        state = _state({
            "pr_number": PR_NUMBER,
            BRANCH_KEY: LEGACY_BRANCH,
        })
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
        state = _state({
            "pr_number": PR_NUMBER,
            BRANCH_KEY: NAMESPACED_BRANCH,
        })
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            NAMESPACED_BRANCH,
        )


if __name__ == "__main__":
    unittest.main()
