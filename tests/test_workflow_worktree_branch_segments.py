# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from orchestrator import workflow

from tests.worktree_path_test_support import (
    _branch,
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
                seg_a,
                seg_b,
                f"slugs {first_slug!r} and {second_slug!r} both produced {seg_a!r}",
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
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                git_result.returncode,
                0,
                f"slug={repo_slug!r} produced invalid branch {branch!r}: stderr={git_result.stderr!r}",
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
