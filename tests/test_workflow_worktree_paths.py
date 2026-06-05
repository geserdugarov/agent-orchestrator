# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow


class WorktreePathSlugNamespaceTest(unittest.TestCase):
    """Two repos with the same issue number must produce distinct worktree
    paths, otherwise simultaneous orchestration of both would have them
    fighting over the same `WORKTREES_DIR/issue-N` checkout. The slug
    sanitizer also has to produce a single filesystem-safe segment
    (no `/`, no leading `.`) since it becomes a directory name.
    """

    def _spec(self, slug: str) -> config.RepoSpec:
        return config.RepoSpec(
            slug=slug,
            target_root=Path(f"/tmp/{workflow._sanitize_slug(slug)}-target"),
            base_branch="main",
        )

    def test_same_issue_number_different_slugs_no_collision(self) -> None:
        spec_a = self._spec("alice/repo")
        spec_b = self._spec("bob/repo")
        path_a = workflow._worktree_path(spec_a, 7)
        path_b = workflow._worktree_path(spec_b, 7)

        self.assertNotEqual(path_a, path_b)
        # Both must live under WORKTREES_DIR with the issue-N leaf.
        self.assertEqual(path_a.name, "issue-7")
        self.assertEqual(path_b.name, "issue-7")
        self.assertEqual(path_a.parent.parent, config.WORKTREES_DIR)
        self.assertEqual(path_b.parent.parent, config.WORKTREES_DIR)

    def test_decompose_path_also_namespaced_by_slug(self) -> None:
        spec_a = self._spec("alice/repo")
        spec_b = self._spec("bob/repo")
        self.assertNotEqual(
            workflow._decompose_worktree_path(spec_a, 7),
            workflow._decompose_worktree_path(spec_b, 7),
        )

    def test_implement_and_decompose_share_repo_namespace(self) -> None:
        # `WORKTREES_DIR/<slug>/issue-N` and `WORKTREES_DIR/<slug>/decompose-N`
        # share the per-repo subdirectory so cleanup on the parent dir
        # also reaps the decomposer scratch.
        spec = self._spec("owner/name")
        impl = workflow._worktree_path(spec, 11)
        dec = workflow._decompose_worktree_path(spec, 11)
        self.assertEqual(impl.parent, dec.parent)

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
            slug="geserdugarov/agent-orchestrator",
            target_root=Path("/tmp/x"),
            base_branch="main",
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

    def test_same_issue_number_different_slugs_distinct_branches(self) -> None:
        spec_a = config.RepoSpec(
            slug="geserdugarov/lance-open-source",
            target_root=Path("/tmp/shared-clone"),
            base_branch="main",
        )
        spec_b = config.RepoSpec(
            slug="geserdugarov/lance-private",
            target_root=Path("/tmp/shared-clone"),
            base_branch="main",
        )

        self.assertNotEqual(
            workflow._branch_name(spec_a, 15),
            workflow._branch_name(spec_b, 15),
        )

    def test_branch_name_format(self) -> None:
        spec = config.RepoSpec(
            slug="geserdugarov/agent-orchestrator",
            target_root=Path("/tmp/x"),
            base_branch="main",
        )
        self.assertEqual(
            workflow._branch_name(spec, 9),
            "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        )

    def test_branch_name_keeps_orchestrator_prefix(self) -> None:
        # `_cleanup_terminal_branch` relies on the `orchestrator/` prefix
        # to constrain what branches it is willing to delete.
        for slug in ("alice/repo", "bob/repo", "weird name/x"):
            spec = config.RepoSpec(
                slug=slug,
                target_root=Path("/tmp/x"),
                base_branch="main",
            )
            self.assertTrue(
                workflow._branch_name(spec, 42).startswith("orchestrator/"),
                slug,
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

    def _branch(self, slug: str, n: int = 1) -> str:
        spec = config.RepoSpec(
            slug=slug, target_root=Path("/tmp/x"), base_branch="main",
        )
        return workflow._branch_name(spec, n)

    def test_dot_lock_suffix_is_rewritten(self) -> None:
        # `.lock` is replaced by `_lock`, then a `__h<digest>`
        # injectivity suffix is appended because the ref-only rewrite
        # is information-lossy (`foo.lock` and `foo_lock` would
        # otherwise collide).
        out = workflow._sanitize_branch_segment("owner/foo.lock")
        self.assertTrue(
            out.startswith("owner__foo_lock__h"),
            f"unexpected sanitized form: {out!r}",
        )
        # 16-hex-char suffix after the marker, full segment is
        # git-ref-safe.
        self.assertRegex(out, r"^owner__foo_lock__h[0-9a-f]{16}$")

    def test_double_dot_anywhere_collapses_to_underscore(self) -> None:
        out = workflow._sanitize_branch_segment("owner/foo..bar")
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
        for slug in (
            "geserdugarov/agent-orchestrator",
            "alice/repo",
            "acme/widget-private",
        ):
            self.assertEqual(
                workflow._sanitize_branch_segment(slug),
                workflow._sanitize_slug(slug),
                slug,
            )

    def test_distinct_slugs_produce_distinct_branches(self) -> None:
        # Injectivity regression: two slugs whose ref-only rewrites
        # collapse to the same shape (e.g. `foo.lock` <-> `foo_lock`)
        # must still produce distinct branch segments, otherwise two
        # `REPOS` entries sharing a `target_root` would collide on
        # the same branch and the slug-namespacing fix would regress
        # for those slug shapes.
        ambiguous_pairs = [
            ("owner/foo.lock", "owner/foo_lock"),
            ("owner/foo..bar", "owner/foo_bar"),
            ("owner/foo.", "owner/foo_"),
            ("owner/foo...bar", "owner/foo_bar"),
            ("owner/...", "owner/__"),
        ]
        for a, b in ambiguous_pairs:
            seg_a = workflow._sanitize_branch_segment(a)
            seg_b = workflow._sanitize_branch_segment(b)
            self.assertNotEqual(
                seg_a, seg_b,
                f"slugs {a!r} and {b!r} both produced {seg_a!r}",
            )

    def test_hash_suffix_is_deterministic(self) -> None:
        # The injectivity suffix is content-derived, so a given slug
        # always produces the same branch -- a stage handler must be
        # able to recompute the branch on every tick without needing
        # to read prior state.
        slug = "owner/foo.lock"
        self.assertEqual(
            workflow._sanitize_branch_segment(slug),
            workflow._sanitize_branch_segment(slug),
        )

    def test_check_ref_format_accepts_branch_for_pathological_slugs(
        self,
    ) -> None:
        # Verify against the actual git binary: every branch the
        # sanitizer produces for a known-pathological slug must pass
        # `git check-ref-format --branch`. This is the bug the
        # filesystem-only sanitizer would smuggle through to the
        # first `git worktree add`.
        import subprocess
        pathological = [
            "owner/foo.lock",
            "owner/foo..bar",
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
        for slug in pathological:
            branch = self._branch(slug, 1)
            r = subprocess.run(
                ["git", "check-ref-format", "--branch", branch],
                capture_output=True, text=True,
            )
            self.assertEqual(
                r.returncode, 0,
                f"slug={slug!r} produced invalid branch "
                f"{branch!r}: stderr={r.stderr!r}",
            )

    def test_branch_name_uses_branch_safe_segment(self) -> None:
        # `_branch_name` itself must route through the branch-safe
        # sanitizer, not the filesystem-only one -- regression guard
        # for the bug.
        self.assertRegex(
            self._branch("owner/foo.lock", 7),
            r"^orchestrator/owner__foo_lock__h[0-9a-f]{16}/issue-7$",
        )
        self.assertRegex(
            self._branch("owner/foo..bar", 7),
            r"^orchestrator/owner__foo_bar__h[0-9a-f]{16}/issue-7$",
        )


class ResolveBranchNameLegacyMigrationTest(unittest.TestCase):
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

    def _spec(self):
        from orchestrator.github import PinnedState  # noqa: F401
        return config.RepoSpec(
            slug="geserdugarov/agent-orchestrator",
            target_root=Path("/tmp/x"),
            base_branch="main",
        )

    def _state(self, data=None):
        from orchestrator.github import PinnedState
        return PinnedState(comment_id=None, data=dict(data or {}))

    def test_pinned_legacy_branch_is_honored(self) -> None:
        spec = self._spec()
        state = self._state({"branch": "orchestrator/issue-7"})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            "orchestrator/issue-7",
        )

    def test_no_pinned_branch_falls_back_to_namespaced_default(self) -> None:
        spec = self._spec()
        state = self._state({})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            "orchestrator/geserdugarov__agent-orchestrator/issue-7",
        )

    def test_pinned_branch_outside_orchestrator_namespace_is_ignored(
        self,
    ) -> None:
        # A corrupted / foreign pinned `branch` value must not redirect
        # the resolver at an arbitrary ref -- the `orchestrator/` prefix
        # check keeps `_cleanup_terminal_branch`'s "orchestrator-owned
        # namespace" invariant intact.
        spec = self._spec()
        state = self._state({"branch": "feature/foreign-branch"})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            "orchestrator/geserdugarov__agent-orchestrator/issue-7",
        )

    def test_pinned_namespaced_branch_round_trips(self) -> None:
        # Once the resolver computed and persisted the new form, a later
        # tick honors it unchanged.
        spec = self._spec()
        state = self._state({
            "branch": "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        })
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 9),
            "orchestrator/geserdugarov__agent-orchestrator/issue-9",
        )

    def test_non_string_pinned_branch_falls_back(self) -> None:
        spec = self._spec()
        for bad in (None, 42, ["orchestrator/issue-7"]):
            state = self._state({"branch": bad})
            self.assertEqual(
                workflow._resolve_branch_name(state, spec, 7),
                "orchestrator/geserdugarov__agent-orchestrator/issue-7",
                f"bad pinned value {bad!r} did not fall back",
            )

    def test_legacy_pr_without_pinned_branch_uses_legacy_ref(self) -> None:
        # Pre-slug-namespacing in-flight PR: pinned state recorded
        # `pr_number` but no `branch` (the early implementations did
        # not always persist `branch`). The live PR head is on the
        # legacy `orchestrator/issue-N` ref because that is the only
        # form the orchestrator ever produced before this change. The
        # resolver MUST infer that ref so the next tick anchors on
        # the existing PR; without the fallback it would target the
        # new slug-namespaced branch, push there, open a duplicate
        # PR, and orphan the original.
        spec = self._spec()
        state = self._state({"pr_number": 42})
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            "orchestrator/issue-7",
        )

    def test_legacy_pr_with_pinned_branch_still_honors_pinned(self) -> None:
        # Belt-and-suspenders: a legacy in-flight PR that DID persist
        # `branch` (the consistent half of the pre-slug-namespacing
        # behavior) is still resolved via the pinned value, not via
        # the pr_number fallback -- the two cases agree on the legacy
        # form, but the pinned-value path is more specific.
        spec = self._spec()
        state = self._state({
            "pr_number": 42,
            "branch": "orchestrator/issue-7",
        })
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            "orchestrator/issue-7",
        )

    def test_fresh_pr_with_pinned_namespaced_branch_wins(self) -> None:
        # A PR opened AFTER slug-namespacing landed has both
        # `pr_number` and the namespaced `branch` set. The
        # pr_number-fallback must not override the pinned value, or
        # every new PR would silently route through the legacy ref.
        spec = self._spec()
        state = self._state({
            "pr_number": 42,
            "branch": "orchestrator/geserdugarov__agent-orchestrator/issue-7",
        })
        self.assertEqual(
            workflow._resolve_branch_name(state, spec, 7),
            "orchestrator/geserdugarov__agent-orchestrator/issue-7",
        )


if __name__ == "__main__":
    unittest.main()
