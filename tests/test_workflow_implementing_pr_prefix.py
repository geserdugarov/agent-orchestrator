# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr prefix behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

DEFAULT_REVISION_RANGE = support.DEFAULT_REVISION_RANGE
FAKE_WORKTREE = support.FAKE_WORKTREE
FEATURE_PREFIX = support.FEATURE_PREFIX
GIT_ERROR_ISSUE = support.GIT_ERROR_ISSUE
GIT_HELPER = support.GIT_HELPER
REMOTE_ROUTING_ISSUE = support.REMOTE_ROUTING_ISSUE
TEST_TARGET_ROOT = support.TEST_TARGET_ROOT
_GitRecorder = support._GitRecorder
_SubjectPrefixFixtureMixin = support._SubjectPrefixFixtureMixin
_TEST_SPEC = support._TEST_SPEC
branch_publication = support.branch_publication
config = support.config
make_issue = support.make_issue
patch = support.patch
workflow = support.workflow
worktree_lifecycle = support.worktree_lifecycle


class InferSubjectPrefixTest(unittest.TestCase, _SubjectPrefixFixtureMixin):
    """`_infer_subject_prefix` reads recent base-branch history and reuses a
    dominant repo-local prefix; otherwise it falls back to `fix` for
    bug-labelled issues and `feat` everywhere else."""

    def test_dominant_repo_local_prefix_is_reused(self) -> None:
        # Events repo: `event:` dominates, so the fallback honors it.
        self.assertEqual(
            self._infer("event: gala\nevent: meetup\nfeat: tooling\n"),
            "event",
        )

    def test_repo_local_prefix_overrides_bug_label(self) -> None:
        # The repo's own style wins even for a bug-labelled issue -- a repo
        # that doesn't use `fix:` shouldn't suddenly get one.
        self.assertEqual(
            self._infer("event: gala\nevent: meetup\n", bug=True),
            "event",
        )

    def test_conventional_history_keeps_feat_default(self) -> None:
        # When the dominant prefix is itself a Conventional type, defer to
        # the bug/feat heuristic rather than echoing the history prefix.
        self.assertEqual(self._infer("feat: a\nfix: b\nfeat: c\n"), FEATURE_PREFIX)

    def test_conventional_history_bug_label_uses_fix(self) -> None:
        self.assertEqual(self._infer("feat: a\nfeat: b\n", bug=True), "fix")

    def test_empty_history_falls_back_to_feat(self) -> None:
        self.assertEqual(self._infer(""), FEATURE_PREFIX)

    def test_unprefixed_history_falls_back_to_feat(self) -> None:
        # History with no `<prefix>:` subjects yields no dominant prefix.
        self.assertEqual(self._infer("initial commit\nmore work\n"), FEATURE_PREFIX)


class InferSubjectPrefixGitRoutingTest(
    unittest.TestCase,
    _SubjectPrefixFixtureMixin,
):
    def test_git_error_falls_back_without_crashing(self) -> None:
        issue = make_issue(GIT_ERROR_ISSUE, title="do a thing")
        git = _GitRecorder(returncode=1, stderr="fatal: bad revision")
        with patch.object(branch_publication, GIT_HELPER, git):
            prefix = workflow._infer_subject_prefix(_TEST_SPEC, FAKE_WORKTREE, issue)
        self.assertEqual(prefix, FEATURE_PREFIX)

    def test_reads_per_spec_base_and_remote(self) -> None:
        private_spec = config.RepoSpec(
            slug="acme/widget",
            target_root=TEST_TARGET_ROOT,
            base_branch="master",
            remote_name="private",
        )
        git = _GitRecorder("event: x\n")
        with patch.object(branch_publication, GIT_HELPER, git):
            workflow._infer_subject_prefix(private_spec, FAKE_WORKTREE, make_issue(REMOTE_ROUTING_ISSUE))
        args, _cwd = git.calls[0]
        # The history log targets `<remote>/<base>`, honoring the spec.
        self.assertIn("private/master", args)
        self.assertNotIn("origin/main", args)


class FirstCommitSubjectBaseBranchTest(unittest.TestCase):
    """`_first_commit_subject` must compare against `spec.base_branch`, not
    the global `config.BASE_BRANCH`. With `REPOS=...|...|master` and the
    legacy `BASE_BRANCH=main`, the global default would point at the wrong
    remote and either fail or include unrelated commits."""

    def test_uses_per_spec_base_branch(self) -> None:
        master_spec = config.RepoSpec(
            slug="acme/legacy",
            target_root=TEST_TARGET_ROOT,
            base_branch="master",
        )
        git = _GitRecorder("feat: hello\n")
        with patch.object(branch_publication, GIT_HELPER, git):
            subj = workflow._first_commit_subject(
                master_spec,
                FAKE_WORKTREE,
            )
        self.assertEqual(subj, "feat: hello")
        self.assertEqual(len(git.calls), 1)
        args, _cwd = git.calls[0]
        # The third positional arg to _git is the rev range; it must
        # reference master (the spec's base_branch), not the cached `main`.
        self.assertIn("origin/master..HEAD", args)
        self.assertNotIn(DEFAULT_REVISION_RANGE, args)

    def test_default_spec_still_uses_main(self) -> None:
        # Sanity check: legacy single-repo deployments keep using `main`
        # because `_TEST_SPEC.base_branch` is `main`.
        git = _GitRecorder()
        with patch.object(branch_publication, GIT_HELPER, git):
            workflow._first_commit_subject(_TEST_SPEC, FAKE_WORKTREE)
        args, _cwd = git.calls[0]
        self.assertIn(DEFAULT_REVISION_RANGE, args)

    def test_uses_per_spec_remote_name(self) -> None:
        # Multi-remote target clones (e.g. public `origin` + private fork
        # `private`) need the rev range to reference the configured remote.
        private_spec = config.RepoSpec(
            slug="acme/widget",
            target_root=TEST_TARGET_ROOT,
            base_branch="main",
            remote_name="private",
        )
        git = _GitRecorder("feat: hi\n")
        with patch.object(branch_publication, GIT_HELPER, git):
            workflow._first_commit_subject(
                private_spec,
                FAKE_WORKTREE,
            )
        args, _cwd = git.calls[0]
        self.assertIn("private/main..HEAD", args)
        self.assertNotIn(DEFAULT_REVISION_RANGE, args)


class HasNewCommitsRemoteNameTest(unittest.TestCase):
    """`_has_new_commits` must compare against `spec.remote_name`, not the
    hardcoded `origin`. With REPOS configured to drive a non-default remote
    (e.g. `private`), the rev-list base reference has to honor that or the
    handler will read stale commits from the wrong upstream."""

    def test_rev_list_references_per_spec_remote(self) -> None:
        git = _GitRecorder("0\n")
        private_spec = config.RepoSpec(
            slug="acme/widget",
            target_root=TEST_TARGET_ROOT,
            base_branch="main",
            remote_name="private",
        )
        with patch.object(worktree_lifecycle, GIT_HELPER, git):
            workflow._has_new_commits(private_spec, FAKE_WORKTREE)
        args, _cwd = git.calls[0]
        self.assertIn("private/main..HEAD", args)
        self.assertNotIn(DEFAULT_REVISION_RANGE, args)
