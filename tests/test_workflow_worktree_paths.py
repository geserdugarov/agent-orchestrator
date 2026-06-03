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


if __name__ == "__main__":
    unittest.main()
