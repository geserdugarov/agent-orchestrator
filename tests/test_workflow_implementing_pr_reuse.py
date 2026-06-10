# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""PR reuse for `_on_commits` and the Conventional-Commits / PR-title helpers
that derive the PR title from the first commit subject (with per-spec base
branch and remote)."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import branch_publication, config, workflow, worktree_lifecycle

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class OnCommitsPRReuseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_existing_open_pr_is_reused(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        existing = FakePR(number=42, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-4")
        gh.existing_open_pr["orchestrator/geserdugarov__agent-orchestrator/issue-4"] = existing

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        # No new PR opened, no sparkles comment posted.
        self.assertEqual(gh.opened_prs, [])
        self.assertFalse(any(":sparkles: PR opened" in body
                             for _, body in gh.posted_comments))
        self.assertIn((4, "validating"), gh.label_history)
        self.assertEqual(gh.pinned_data(4).get("pr_number"), 42)

    def test_legacy_pinned_branch_anchors_pr_lookup_and_push(self) -> None:
        # Regression: an in-flight issue that was already running before
        # branches were slug-namespaced has `state["branch"]` pinned to
        # the legacy `orchestrator/issue-<n>` form and a live PR whose
        # head is that legacy ref. The orchestrator must keep using the
        # pinned branch -- otherwise the PR lookup misses, a fresh
        # slug-namespaced branch gets pushed, and a duplicate PR opens
        # against the new branch while the original PR is orphaned.
        LEGACY = "orchestrator/issue-4"
        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        existing = FakePR(number=42, head_branch=LEGACY)
        gh.existing_open_pr[LEGACY] = existing
        # Pinned state mirrors what an issue picked up before this
        # change would carry.
        gh.seed_state(4, branch=LEGACY)

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        # PR lookup hit the legacy ref -- no duplicate PR opened, no
        # `:sparkles: PR opened` comment.
        self.assertEqual(gh.opened_prs, [])
        self.assertFalse(any(":sparkles: PR opened" in body
                             for _, body in gh.posted_comments))
        self.assertEqual(gh.pinned_data(4).get("pr_number"), 42)
        # Push targeted the legacy branch, not the new namespaced one.
        push_call = mocks["_push_branch"].call_args
        self.assertEqual(push_call.args[2], LEGACY)
        # State stays pinned to the legacy branch.
        self.assertEqual(gh.pinned_data(4).get("branch"), LEGACY)

    def test_on_commits_persists_branch_for_branchless_resume_state(self) -> None:
        # Regression: a state that lacks `branch` going into `_on_commits`
        # (the awaiting-human resume path skips the fresh-spawn
        # `state.set("branch", ...)` block) would, before this fix, leave
        # `pr_number` persisted with `branch` absent. The next tick's
        # `_resolve_branch_name` then takes the legacy-PR fallback and
        # routes validation / base-sync / cleanup to
        # `orchestrator/issue-N` while the live PR is actually on the
        # slug-namespaced ref this push just published. `_on_commits`
        # must persist the pushed branch alongside `pr_number` so the
        # resolver recovers it directly.
        gh = FakeGitHubClient()
        issue = make_issue(11, label="implementing")
        # Pending human comment that triggers the awaiting-human resume.
        issue.comments.append(
            FakeComment(id=2100, body="please retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        # State carries `awaiting_human=True` and a dev session id but
        # NO `branch` -- the pre-existing shape for a relabel-from-
        # question or any park whose pre-spawn site never persisted
        # `branch`. `pr_number` is also absent because no PR exists
        # yet; the resume produces the first commit.
        gh.seed_state(
            11,
            awaiting_human=True,
            dev_agent="claude",
            dev_session_id="dev-sess",
            last_action_comment_id=2000,
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="done"),
            # Resume path: no recovered-worktree shortcut, post-agent
            # check sees the new commit.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        # A PR was opened and persisted to state.
        self.assertEqual(len(gh.opened_prs), 1)
        data = gh.pinned_data(11)
        self.assertEqual(data.get("pr_number"), gh.opened_prs[0].number)
        # The branch was persisted alongside `pr_number` so the next
        # tick's `_resolve_branch_name` recovers the slug-namespaced
        # form directly instead of mis-inferring the legacy ref.
        self.assertEqual(
            data.get("branch"),
            "orchestrator/geserdugarov__agent-orchestrator/issue-11",
        )


class ConventionalCommitPromptTest(unittest.TestCase):
    """Both implement and fix prompts must teach the agent the repo's
    Conventional-Commits convention (the prefixes and the `git log` step
    that lets the agent confirm the local style)."""

    def test_implement_prompt_mentions_conventional_commits(self) -> None:
        issue = make_issue(7, title="add a thing", body="please add a thing")
        prompt = workflow._build_implement_prompt(issue, comments_text="")

        self.assertIn("git log", prompt)
        self.assertIn("Conventional Commits", prompt)
        # The exact prefixes from the issue body must be listed so the agent
        # picks one rather than inventing a custom type.
        for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:"):
            self.assertIn(prefix, prompt)
        # Subject-only commits, no extended body and no Co-Authored-By trailer.
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)

    def test_fix_prompt_mentions_conventional_commits(self) -> None:
        prompt = workflow._build_fix_prompt("please fix the typo")

        self.assertIn("git log", prompt)
        self.assertIn("Conventional Commits", prompt)
        for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:"):
            self.assertIn(prefix, prompt)
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)

    def test_pr_comment_followup_mentions_conventional_commits(self) -> None:
        comments = [FakeComment(id=42, body="please rename foo to bar",
                                user=FakeUser("alice"))]
        prompt = workflow._build_pr_comment_followup(comments)

        self.assertIn("git log", prompt)
        self.assertIn("Conventional Commits", prompt)
        for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:"):
            self.assertIn(prefix, prompt)
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)


class ForegroundOnlyPromptTest(unittest.TestCase):
    """Every prompt that can lead to a commit must spell out the one-shot
    execution model: the session dies when the model ends its turn, so a
    backgrounded build/test ("Miri is running, I'll continue when it
    completes") is never observed and the issue parks forever."""

    MARKER = "NEVER start a background job"

    def test_dev_facing_prompts_carry_foreground_only_note(self) -> None:
        issue = make_issue(7, title="add a thing", body="please add a thing")
        comments = [FakeComment(id=42, body="please rename foo to bar",
                                user=FakeUser("alice"))]
        prompts = {
            "implement": workflow._build_implement_prompt(
                issue, comments_text=""),
            "fix": workflow._build_fix_prompt("please fix the typo"),
            "pr_comment_followup": workflow._build_pr_comment_followup(
                comments),
            "documentation": workflow._build_documentation_prompt(
                _TEST_SPEC, issue, comments_text=""),
            "conflict": workflow._build_conflict_resolution_prompt(
                "origin/main", ["a.rs"]),
            "user_content_change": workflow._build_user_content_change_prompt(
                issue, comments_text=""),
        }
        for name, prompt in prompts.items():
            with self.subTest(prompt=name):
                self.assertIn(self.MARKER, prompt)


class ConventionalPrTitleTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_on_commits` derives the PR title from the agent's first commit
    subject when it already follows the Conventional-Commits convention,
    and falls back to a `<type>: <issue title>` form otherwise."""

    def _seeded(self, *, issue_number: int = 30, label_name: str = "") -> tuple:
        gh = FakeGitHubClient()
        issue = make_issue(
            issue_number,
            label="implementing",
            title="add a sparkly thing",
        )
        if label_name:
            issue.labels.append(FakeLabel(label_name))
        gh.add_issue(issue)
        return gh, issue

    def test_pr_title_uses_conventional_first_commit_subject(self) -> None:
        gh, issue = self._seeded(issue_number=30)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="feat: add a sparkly thing",
        )

        self.assertEqual(len(gh.opened_prs), 1)
        pr = gh.opened_prs[0]
        # First-commit subject is preserved verbatim, no extra prefix.
        self.assertEqual(pr.title, "feat: add a sparkly thing")
        # Traceability still in body.
        self.assertIn(f"Resolves #{issue.number}", pr.body)

    def test_pr_title_uses_scoped_conventional_first_commit_subject(self) -> None:
        gh, issue = self._seeded(issue_number=31)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            # Conventional Commits also allow `<type>(<scope>): ...` and
            # `<type>!:` for breaking changes; both must be accepted.
            first_commit_subject="fix(api)!: drop legacy endpoint",
        )

        self.assertEqual(gh.opened_prs[0].title, "fix(api)!: drop legacy endpoint")

    def test_pr_title_falls_back_to_feat_for_unconventional_commit(self) -> None:
        gh, issue = self._seeded(issue_number=32)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="updated stuff",
        )

        pr = gh.opened_prs[0]
        # Fallback uses `feat:` (no bug label) and the issue title.
        self.assertEqual(pr.title, "feat: add a sparkly thing")
        self.assertIn(f"Resolves #{issue.number}", pr.body)

    def test_pr_title_falls_back_to_fix_for_bug_labelled_issue(self) -> None:
        gh, issue = self._seeded(issue_number=33, label_name="bug")

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="fixed it",
        )

        # Bug label tips the fallback to `fix:`.
        self.assertEqual(gh.opened_prs[0].title, "fix: add a sparkly thing")

    def test_pr_title_fallback_when_no_commit_subject_available(self) -> None:
        gh, issue = self._seeded(issue_number=34)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="",
        )

        self.assertEqual(gh.opened_prs[0].title, "feat: add a sparkly thing")

    def test_pr_title_uses_conventional_issue_title_in_fallback(self) -> None:
        # Issue title already conventional -> use it directly so we don't
        # produce a doubled `feat: feat: ...` form.
        gh = FakeGitHubClient()
        issue = make_issue(
            35, label="implementing", title="docs: clarify the README"
        )
        gh.add_issue(issue)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="some unconventional commit",
        )

        self.assertEqual(gh.opened_prs[0].title, "docs: clarify the README")


class ConventionalSubjectHelperTest(unittest.TestCase):
    """Direct coverage for the regex helper, since the convention list grew
    beyond what the prompts spell out."""

    def test_accepts_basic_types(self) -> None:
        for subject in (
            "feat: add thing",
            "fix: bug",
            "chore: bump dep",
            "docs: tweak",
            "refactor: rename foo",
            "test: cover edge case",
            "perf: speed it up",
            "ci: fix workflow",
        ):
            self.assertTrue(
                workflow._is_conventional_subject(subject),
                f"expected conventional: {subject!r}",
            )

    def test_accepts_scope_and_breaking(self) -> None:
        self.assertTrue(workflow._is_conventional_subject("feat(api): foo"))
        self.assertTrue(workflow._is_conventional_subject("fix!: bar"))
        self.assertTrue(workflow._is_conventional_subject("feat(api)!: baz"))

    def test_rejects_non_conventional(self) -> None:
        for subject in (
            "",
            "Add a thing",
            "wip: thing",
            "feat:",            # no subject after colon
            "feat:   ",         # whitespace-only subject
            "Feat: cap type",   # types must be lowercase
            "  feat: leading", # leading whitespace not accepted
        ):
            self.assertFalse(
                workflow._is_conventional_subject(subject),
                f"expected non-conventional: {subject!r}",
            )


class FirstCommitSubjectBaseBranchTest(unittest.TestCase):
    """`_first_commit_subject` must compare against `spec.base_branch`, not
    the global `config.BASE_BRANCH`. With `REPOS=...|...|master` and the
    legacy `BASE_BRANCH=main`, the global default would point at the wrong
    remote and either fail or include unrelated commits."""

    def _capture_git(self, stdout: str = "feat: x\n"):
        from unittest.mock import MagicMock

        captured: list[tuple] = []

        def fake_git(*args, cwd):
            captured.append((args, cwd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = stdout
            r.stderr = ""
            return r

        return fake_git, captured

    def test_uses_per_spec_base_branch(self) -> None:
        master_spec = config.RepoSpec(
            slug="acme/legacy",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="master",
        )
        fake_git, captured = self._capture_git("feat: hello\n")
        with patch.object(branch_publication, "_git", fake_git):
            subj = workflow._first_commit_subject(
                master_spec, Path("/tmp/wt-not-real")
            )
        self.assertEqual(subj, "feat: hello")
        self.assertEqual(len(captured), 1)
        args, _cwd = captured[0]
        # The third positional arg to _git is the rev range; it must
        # reference master (the spec's base_branch), not the cached `main`.
        self.assertIn("origin/master..HEAD", args)
        self.assertNotIn("origin/main..HEAD", args)

    def test_default_spec_still_uses_main(self) -> None:
        # Sanity check: legacy single-repo deployments keep using `main`
        # because `_TEST_SPEC.base_branch` is `main`.
        fake_git, captured = self._capture_git("")
        with patch.object(branch_publication, "_git", fake_git):
            workflow._first_commit_subject(_TEST_SPEC, Path("/tmp/wt-not-real"))
        args, _cwd = captured[0]
        self.assertIn("origin/main..HEAD", args)

    def test_uses_per_spec_remote_name(self) -> None:
        # Multi-remote target clones (e.g. public `origin` + private fork
        # `private`) need the rev range to reference the configured remote.
        private_spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
            remote_name="private",
        )
        fake_git, captured = self._capture_git("feat: hi\n")
        with patch.object(branch_publication, "_git", fake_git):
            workflow._first_commit_subject(
                private_spec, Path("/tmp/wt-not-real")
            )
        args, _cwd = captured[0]
        self.assertIn("private/main..HEAD", args)
        self.assertNotIn("origin/main..HEAD", args)


class HasNewCommitsRemoteNameTest(unittest.TestCase):
    """`_has_new_commits` must compare against `spec.remote_name`, not the
    hardcoded `origin`. With REPOS configured to drive a non-default remote
    (e.g. `private`), the rev-list base reference has to honor that or the
    handler will read stale commits from the wrong upstream."""

    def test_rev_list_references_per_spec_remote(self) -> None:
        from unittest.mock import MagicMock

        captured: list[tuple] = []

        def fake_git(*args, cwd):
            captured.append((args, cwd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = "0\n"
            r.stderr = ""
            return r

        private_spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
            remote_name="private",
        )
        with patch.object(worktree_lifecycle, "_git", fake_git):
            workflow._has_new_commits(private_spec, Path("/tmp/wt-not-real"))
        args, _cwd = captured[0]
        self.assertIn("private/main..HEAD", args)
        self.assertNotIn("origin/main..HEAD", args)
