# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow, worktrees

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _iso_hours_ago,
)


class HandleImplementingFreshRunTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, label="implementing"):
        gh = FakeGitHubClient()
        issue = make_issue(1, label=label)
        gh.add_issue(issue)
        # No prior pinned state; simulate just-after-pickup.
        return gh, issue

    def test_commits_clean_tree_opens_pr_and_flips_label(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First call: not a recovered worktree -> codex runs.
            # Second call: codex produced commits -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(len(gh.opened_prs), 1)
        opened = gh.opened_prs[0]
        self.assertTrue(any(
            f":sparkles: PR opened: #{opened.number}" in body
            for _, body in gh.posted_comments
        ))
        # After PR open, hand off to the documenting stage; the docs
        # pass runs against the same PR worktree and advances to
        # validating only once docs are pushed / explicit no-change.
        self.assertIn((1, "documenting"), gh.label_history)
        self.assertNotIn((1, "validating"), gh.label_history)
        data = gh.pinned_data(1)
        self.assertEqual(data["pr_number"], opened.number)
        self.assertEqual(data["branch"], "orchestrator/issue-1")
        # First fresh dev spawn writes the new keys; the legacy field is
        # deliberately not migrated.
        self.assertEqual(data["dev_agent"], config.DEV_AGENT)
        self.assertEqual(data["dev_session_id"], "sess-1")
        self.assertNotIn("codex_session_id", data)
        self.assertEqual(data["review_round"], 0)

    def test_commits_with_dirty_tree_parks_without_pushing(self) -> None:
        gh, issue = self._seeded()
        dirty = [f"file_{i}.py" for i in range(15)]
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="commit done but more work pending"),
            has_new_commits=[False, True],
            dirty_files=dirty,
            push_branch=True,
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("file_0.py", last_comment)
        self.assertIn("file_9.py", last_comment)
        self.assertNotIn("file_10.py", last_comment)
        self.assertIn("… (5 more)", last_comment)

    def test_no_commits_with_message_parks_as_question(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="What database should I use?"),
            has_new_commits=False,
        )

        self.assertEqual(gh.opened_prs, [])
        data = gh.pinned_data(1)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("> What database should I use?", last_comment)
        self.assertIn("agent needs your input", last_comment)
        # A real question with content is not a silent failure.
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("silent_park_count", 0), 0)

    def test_no_commits_no_message_parks_as_silent_failure(self) -> None:
        # Empty `last_message` AND no commits is the poisoned-resume shape
        # documented in #24: a session killed mid-stream (e.g. by a Claude
        # rate limit) consistently returns empty results on every resume.
        # The park must surface as a silent failure (distinct
        # `park_reason`, distinct HITL message) instead of impersonating a
        # real "agent has a content question" park.
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message=""),
            has_new_commits=False,
        )

        data = gh.pinned_data(1)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_silent")
        self.assertEqual(data.get("silent_park_count"), 1)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent produced no output", last_comment)
        self.assertIn("session-resume failure", last_comment)
        self.assertNotIn("agent needs your input", last_comment)
        # No quoted empty-message body either.
        self.assertNotIn("> (agent did not produce a final message)", last_comment)

    def test_silent_failure_park_includes_stderr_diagnostics(self) -> None:
        # Same shape as the silent-failure park, but the agent left
        # something on stderr (e.g. a Cloudflare blob, an auth error).
        # The park comment must surface that tail and the exit code so
        # the operator can triage without reading ~/.codex/log/.
        gh, issue = self._seeded()
        with self.assertLogs("orchestrator.workflow", level="WARNING") as logs:
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    last_message="",
                    stderr="401 Unauthorized: token expired",
                    exit_code=1,
                ),
                has_new_commits=False,
            )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent produced no output", last_comment)
        self.assertIn("_Agent stderr (last 1KB):_", last_comment)
        self.assertIn("401 Unauthorized", last_comment)
        self.assertIn("_Agent exit code:_ 1", last_comment)
        self.assertTrue(any(
            "agent produced no output" in r.getMessage()
            and "exit_code=1" in r.getMessage()
            for r in logs.records
        ))

    def test_codex_timeout_parks_with_timeout_message(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
            has_new_commits=False,
        )

        mocks["_push_branch"].assert_not_called()
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent timed out", last_comment)
        self.assertEqual(gh.opened_prs, [])

    def test_push_failure_parks_without_opening_pr(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=False,
        )

        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)


class HandleImplementingAwaitingHumanTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_no_new_comments_returns_without_writing_state(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        gh.add_issue(issue)
        # Pre-seed `user_content_hash` so the durability-fix branch in
        # `_detect_user_content_change` doesn't trigger an extra
        # baseline-seeding write; this test specifically verifies the
        # awaiting-human no-reply path produces zero state churn.
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            user_content_hash=workflow._compute_user_content_hash(
                issue, set()
            ),
        )
        before = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.write_state_calls, before)
        # Pinned data unchanged.
        self.assertTrue(gh.pinned_data(2).get("awaiting_human"))
        self.assertEqual(gh.pinned_data(2).get("codex_session_id"), "sess-old")

    def test_new_comments_resume_with_session_and_clear_awaiting(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            branch="orchestrator/issue-2",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", last_message="ok"),
            # awaiting_human path skips the recovered-worktree probe; only
            # the post-codex commit check runs.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        # Legacy `codex_session_id` locks the resume to the codex backend
        # regardless of the current DEV_AGENT default.
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "sess-old")
        followup_arg = call.args[1]
        self.assertIn("please use sqlite", followup_arg)
        # Ran through to PR open.
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertFalse(gh.pinned_data(2).get("awaiting_human"))


class HandleImplementingRecoveredWorktreeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_recovered_worktree_skips_codex_and_pushes(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(3, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(3, codex_session_id="sess-prev")

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # Prior session id retained.
        self.assertEqual(gh.pinned_data(3).get("codex_session_id"), "sess-prev")


class OnCommitsPRReuseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_existing_open_pr_is_reused(self) -> None:
        from tests.fakes import FakePR

        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        existing = FakePR(number=42, head_branch="orchestrator/issue-4")
        gh.existing_open_pr["orchestrator/issue-4"] = existing

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
        self.assertIn((4, "documenting"), gh.label_history)
        self.assertEqual(gh.pinned_data(4).get("pr_number"), 42)


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
        with patch.object(worktrees, "_git", fake_git):
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
        with patch.object(worktrees, "_git", fake_git):
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
        with patch.object(worktrees, "_git", fake_git):
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
        with patch.object(worktrees, "_git", fake_git):
            workflow._has_new_commits(private_spec, Path("/tmp/wt-not-real"))
        args, _cwd = captured[0]
        self.assertIn("private/main..HEAD", args)
        self.assertNotIn("origin/main..HEAD", args)


class HandleImplementingRetryCapTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Bound the implementing loop with MAX_RETRIES_PER_DAY in pinned state.

    Resumes on human reply and recovered-worktree pushes are explicitly NOT
    counted; only fresh codex spawns consume the budget.
    """

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(8, label="implementing")
        gh.add_issue(issue)
        if state:
            gh.seed_state(8, **state)
        return gh, issue

    def test_fourth_fresh_attempt_in_window_is_parked_before_codex(self) -> None:
        # Run three fresh attempts that each park as a question, then assert
        # the fourth tick parks before run_agent is called. Pin the cap at 3
        # so the test is hermetic against a `MAX_RETRIES_PER_DAY` env
        # override that would otherwise let the fourth tick spawn through.
        gh, issue = self._seeded()

        with patch.object(config, "MAX_RETRIES_PER_DAY", 3):
            # First three ticks: codex returns no commits + a question, parking on
            # awaiting_human. Each tick consumes one retry from the budget.
            for tick in range(3):
                self._run(
                    lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                    run_agent=_agent(last_message=f"q{tick}"),
                    has_new_commits=False,
                )
                # Clear the awaiting-human flag manually so the next tick takes
                # the fresh-spawn branch again (simulating that the human answered
                # but the agent still failed to commit). We do NOT update
                # last_action_comment_id, but we also drop awaiting_human so the
                # else branch runs.
                data = gh._pinned[8].data
                data["awaiting_human"] = False

            self.assertEqual(gh.pinned_data(8).get("retry_count"), 3)
            self.assertIsNotNone(gh.pinned_data(8).get("retry_window_start"))

            # Fourth tick: must park before codex spawns.
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="should not run"),
                has_new_commits=False,
            )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(8).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("hit retry cap (3/day)", last_comment)
        self.assertIn("Window opened at", last_comment)

    def test_successful_on_commits_clears_retry_counter(self) -> None:
        # Pre-seed near-cap state, then run a successful tick (commits + clean
        # tree + push succeeds). The PR-open path must clear the budget.
        gh, issue = self._seeded(
            retry_count=2,
            retry_window_start=_iso_hours_ago(1),
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        data = gh.pinned_data(8)
        self.assertEqual(data.get("retry_count"), 0)
        # window_start cleared back to falsy.
        self.assertFalse(data.get("retry_window_start"))
        self.assertEqual(len(gh.opened_prs), 1)

    def test_window_older_than_24h_resets_counter(self) -> None:
        # Cap exhausted but the window is 25h old: next fresh attempt opens a
        # new window with count=1 and codex actually spawns.
        gh, issue = self._seeded(
            retry_count=3,
            retry_window_start=_iso_hours_ago(25),
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="ask again"),
            has_new_commits=False,
        )

        mocks["run_agent"].assert_called_once()
        data = gh.pinned_data(8)
        # Reset to 0 by the window-expired branch, then incremented to 1.
        self.assertEqual(data.get("retry_count"), 1)
        # Park message must NOT be the cap message.
        last_comment = gh.posted_comments[-1][1]
        self.assertNotIn("hit retry cap", last_comment)

    def test_awaiting_human_resume_does_not_increment_counter(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(9, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            9,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            retry_count=2,
            retry_window_start=_iso_hours_ago(1),
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        # Resume happened (codex was called once with the followup comment).
        mocks["run_agent"].assert_called_once()
        # retry_count NOT incremented by the resume itself. The successful
        # _on_commits then clears it to 0.
        data = gh.pinned_data(9)
        self.assertEqual(data.get("retry_count"), 0)


class ConfigurableBackendTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The dev/review backends are picked from config, with the dev backend
    locked to whatever wrote `dev_session_id` (or legacy `codex_session_id`)
    so a config flip mid-flight does not break a resumable session.
    """

    def test_fresh_implementing_spawn_uses_dev_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label="implementing")
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-fresh", last_message="done"),
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "claude")
        data = gh.pinned_data(20)
        self.assertEqual(data["dev_agent"], "claude")
        self.assertEqual(data["dev_session_id"], "sess-fresh")
        self.assertNotIn("codex_session_id", data)

    def test_reviewer_spawn_uses_review_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(21, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pr_number=21,
            branch="orchestrator/issue-21",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
        )

        with patch.object(config, "REVIEW_AGENT", "codex"):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message="LGTM\n\nVERDICT: APPROVED",
                ),
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        data = gh.pinned_data(21)
        self.assertEqual(data["review_agent"], "codex")
        self.assertEqual(data["last_review_session_id"], "rev-sess")

    def test_dev_fix_uses_recorded_dev_backend_not_current_config(self) -> None:
        # Issue locked to codex via pinned state; even if config flips to
        # claude, the validating dev-fix call must stay on codex.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pr_number=22,
            branch="orchestrator/issue-22",
            dev_agent="codex",
            dev_session_id="dev-sess",
            review_round=0,
        )
        review = _agent(
            session_id="rev-sess",
            last_message="1. Tighten\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        with patch.object(config, "DEV_AGENT", "claude"), \
             patch.object(config, "REVIEW_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "aaa", "bbb"],
            )

        # Reviewer takes config; dev-fix takes pinned state.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(mocks["run_agent"].call_args_list[0].args[0], "claude")
        self.assertEqual(mocks["run_agent"].call_args_list[1].args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args_list[1].kwargs.get("resume_session_id"),
            "dev-sess",
        )

    def test_legacy_codex_session_id_resumes_with_codex(self) -> None:
        # Pinned state predates the rollout: only `codex_session_id`. Resume
        # on human reply must stick with codex even when DEV_AGENT=claude.
        gh = FakeGitHubClient()
        issue = make_issue(23, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            23,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-legacy",
            branch="orchestrator/issue-23",
        )

        with patch.object(config, "DEV_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-legacy", last_message="ok"),
                has_new_commits=[True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "sess-legacy",
        )
        # No proactive migration: legacy key stays put, no new keys written
        # by a resume (only fresh spawns write `dev_agent`/`dev_session_id`).
        data = gh.pinned_data(23)
        self.assertEqual(data.get("codex_session_id"), "sess-legacy")
        self.assertNotIn("dev_agent", data)
        self.assertNotIn("dev_session_id", data)


class SilentSessionResumeFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_resume_dev_with_text` drops a poisoned `dev_session_id` after
    `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent` parks
    and starts a fresh spawn instead. Without this fallback every human
    "retry" comment burns another fresh-spawn retry slot on the same dead
    session (the Claude rate-limit kill shape documented in #24).
    """

    def _seeded_issue(self, *, silent_park_count: int):
        gh = FakeGitHubClient()
        issue = make_issue(950, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            950,
            dev_agent="claude",
            dev_session_id="poisoned-sess",
            silent_park_count=silent_park_count,
        )
        return gh, issue

    def test_below_threshold_keeps_existing_session_id(self) -> None:
        # One prior silent park is treated as a transient blip, not a
        # poisoned session: the resume still passes the original session
        # id and the streak counter stays put for the next park to bump.
        gh, issue = self._seeded_issue(silent_park_count=1)
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["resume_session_id"] = resume_session_id
            return _agent(session_id="ignored", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            captured["resume_session_id"], "poisoned-sess",
            "below threshold the original session id must still be resumed",
        )
        # Session id and streak are not touched on the below-threshold path.
        self.assertEqual(state.get("dev_session_id"), "poisoned-sess")
        self.assertEqual(state.get("silent_park_count"), 1)

    def test_at_threshold_drops_session_and_persists_fresh_one(self) -> None:
        # `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive silent parks ==
        # session is poisoned. The resume must call `run_agent` with
        # `resume_session_id=None`, persist the new session id from the
        # result, and reset the silent-park streak so the new session
        # starts with a clean budget.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["agent"] = agent
            captured["resume_session_id"] = resume_session_id
            return _agent(session_id="fresh-sess", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            captured["resume_session_id"],
            "fresh spawn must drop the poisoned dev_session_id",
        )
        self.assertEqual(captured["agent"], "claude")
        # New session id must be persisted so the next resume picks it up
        # instead of looking up an empty `dev_session_id` and re-spawning.
        self.assertEqual(state.get("dev_session_id"), "fresh-sess")
        # Streak resets so a future blip doesn't drop the new session
        # immediately.
        self.assertEqual(state.get("silent_park_count"), 0)

    def test_fresh_spawn_with_empty_session_id_still_clears_pinned(self) -> None:
        # If the fresh spawn comes back without a `session_id` (agent
        # backend hiccup, missing file, etc.), the poisoned id must STILL
        # be removed from pinned state. Otherwise `_read_dev_session` on
        # the next tick returns the dead session and the resume loop
        # re-poisons itself.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(
                 workflow, "run_agent",
                 lambda *a, **kw: _agent(session_id="", last_message=""),
             ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            state.get("dev_session_id"),
            "poisoned session id must be cleared even when the fresh "
            "spawn returns no session_id",
        )

    def test_fresh_spawn_clears_legacy_codex_session_id_too(self) -> None:
        # An issue still on the legacy `codex_session_id` schema must
        # also have that field cleared on fresh-spawn -- otherwise the
        # next tick's `_read_dev_session` falls through the new keys
        # (because `dev_session_id` is None) and resurrects the poisoned
        # legacy id.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh = FakeGitHubClient()
        issue = make_issue(951, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            951,
            # Legacy schema: only `codex_session_id` is set, no `dev_agent`.
            codex_session_id="poisoned-legacy",
            silent_park_count=threshold,
        )
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["agent"] = agent
            captured["resume_session_id"] = resume_session_id
            return _agent(session_id="fresh-legacy", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (legacy).
        self.assertEqual(captured["agent"], "codex")
        # Resume happened with no session id -- the poisoned legacy id
        # was dropped.
        self.assertIsNone(captured["resume_session_id"])
        # Pinned state migrated to the new keys with the fresh session
        # id, and the legacy field is cleared.
        self.assertEqual(state.get("dev_agent"), "codex")
        self.assertEqual(state.get("dev_session_id"), "fresh-legacy")
        self.assertIsNone(state.get("codex_session_id"))


class StaleSessionImmediateRetryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """When Claude's `--resume <sid>` lands on a transcript that no longer
    exists, the CLI prints `No conversation found with session ID` on stderr
    and exits with empty stdout. Without an immediate retry, the resume
    would park `agent_silent` and the `_SILENT_PARKS_BEFORE_FRESH_SESSION`
    threshold path would wait for a second silent park before recovering.
    `_resume_dev_with_text` short-circuits that by detecting the marker and
    retrying once with a cleared session id in the same worktree.
    """

    STALE_STDERR = "Error: No conversation found with session ID: poisoned-sess\n"

    def _seeded_issue(self, *, dev_agent: str = "claude"):
        gh = FakeGitHubClient()
        issue = make_issue(960, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(
            960,
            dev_agent=dev_agent,
            dev_session_id="poisoned-sess",
            silent_park_count=0,
        )
        return gh, issue

    def test_marker_detector_matches_known_phrasings(self) -> None:
        # The detector is keyed off lowercase substrings so phrasing tweaks
        # across Claude CLI releases still trip the recovery path.
        for stderr in (
            "Error: No conversation found with session ID: abc-123",
            "no conversation found with id abc",
            "No conversation with session ID xyz",
            "Conversation not found.",
            # Mixed casing still matches.
            "NO CONVERSATION FOUND WITH SESSION ID foo",
        ):
            with self.subTest(stderr=stderr):
                result = _agent(session_id="", last_message="", stderr=stderr)
                self.assertTrue(
                    workflow._is_stale_session_failure("claude", result),
                    f"{stderr!r} should be classified stale-session",
                )

    def test_marker_detector_ignores_unrelated_stderr(self) -> None:
        result = _agent(
            session_id="", last_message="",
            stderr="Error: rate limited, please retry shortly",
        )
        self.assertFalse(
            workflow._is_stale_session_failure("claude", result)
        )

    def test_marker_detector_only_triggers_for_claude(self) -> None:
        # Codex has no analogous stable marker today; the detector must
        # not misfire on a codex resume whose stderr happens to share text.
        result = _agent(
            session_id="", last_message="",
            stderr="No conversation found with session ID: xyz",
        )
        self.assertFalse(
            workflow._is_stale_session_failure("codex", result)
        )

    def test_claude_stale_session_retries_once_with_fresh_spawn(self) -> None:
        # Two calls expected: the first one resumes the poisoned session and
        # comes back with the marker; the second is a fresh spawn (no resume
        # session id) in the same worktree, with the new session id
        # persisted on success.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="",
                    stderr=self.STALE_STDERR,
                )
            return _agent(session_id="fresh-sess", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, ["poisoned-sess", None],
            "expected one resume with the poisoned id then one fresh spawn",
        )
        self.assertEqual(
            state.get("dev_session_id"), "fresh-sess",
            "fresh spawn's session id must be persisted",
        )
        self.assertEqual(state.get("dev_agent"), "claude")
        self.assertIsNone(state.get("codex_session_id"))
        # Silent-park streak resets so a future blip does not immediately
        # re-drop the new session.
        self.assertEqual(state.get("silent_park_count"), 0)

    def test_stale_session_retry_clears_pinned_even_if_retry_empty(self) -> None:
        # If the fresh-spawn retry returns no session id (CLI hiccup), the
        # poisoned id must still be cleared from pinned state -- otherwise
        # the next tick's `_read_dev_session` resurrects it.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="",
                    stderr=self.STALE_STDERR,
                )
            return _agent(session_id="", last_message="")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            state.get("dev_session_id"),
            "poisoned session id must be cleared even when the retry "
            "returns no session id",
        )

    def test_stale_session_retry_does_not_loop_when_retry_also_stale(self) -> None:
        # If the fresh spawn ALSO trips a stale-session marker something
        # deeper is broken (e.g. a misconfigured CLI). Surface that result
        # to the caller instead of looping infinitely.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(
                session_id="", last_message="", stderr=self.STALE_STDERR,
            )

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            _, result = workflow._resume_dev_with_text(
                gh, _TEST_SPEC, issue, state, "go",
            )

        self.assertEqual(
            calls, ["poisoned-sess", None],
            "retry must be bounded to a single fresh spawn",
        )
        # Result reflects the still-failing retry; caller's downstream
        # `_on_question` will handle the agent_silent park.
        self.assertEqual(result.stderr, self.STALE_STDERR)

    def test_codex_stale_stderr_does_not_trigger_immediate_retry(self) -> None:
        # Codex falls back to the silent-park-count path. A first resume
        # whose stderr happens to contain the marker must NOT retry
        # immediately for the codex backend.
        gh, issue = self._seeded_issue(dev_agent="codex")
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(
                session_id="", last_message="", stderr=self.STALE_STDERR,
            )

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, ["poisoned-sess"],
            "codex backend must NOT trigger the claude-only immediate retry",
        )
        # Poisoned id remains; the existing silent-park-count path is what
        # will eventually drop it.
        self.assertEqual(state.get("dev_session_id"), "poisoned-sess")


class HandleImplementingResumeOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_resumes_dev_session_not_re_decompose(self) -> None:
        # The spec rules out re-decomposing mid-implementation. Once a dev
        # session exists, the handler must instead notify the human and
        # resume the locked dev session with the new body so it can decide
        # whether more work is needed.
        gh = FakeGitHubClient()
        issue = make_issue(60, label="implementing", body="new requirements")
        gh.add_issue(issue)
        gh.seed_state(
            60,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            last_action_comment_id=500,
            branch="orchestrator/issue-60",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="addressed it"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            # Two SHAs so the drift branch's "did THIS resume commit?"
            # head-SHA delta check sees a real change (the original
            # `_has_new_commits` check would have falsely accepted
            # pre-existing unpushed commits on a recovered worktree).
            head_shas=["before-resume", "after-resume"],
        )

        # Dev session resumed; the prompt mentions the updated body.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("new requirements", prompt)
        self.assertIn("Updated issue", prompt)
        # The label flipped via _on_commits -> documenting because the
        # resume produced a commit; the issue is NOT routed to
        # decomposing, and validating only comes after the docs pass.
        self.assertNotIn((60, "decomposing"), gh.label_history)
        self.assertIn((60, "documenting"), gh.label_history)
        data = gh.pinned_data(60)
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))

    def test_no_dev_session_falls_through_to_fresh_spawn(self) -> None:
        # Pre-spawn implementing (ready -> implementing on the same tick,
        # but the dev hasn't run yet): a hash change should just persist
        # the new value and let the fresh-spawn path pick up the new body
        # via `_build_implement_prompt`. There is no "stale dev session"
        # to notify about.
        gh = FakeGitHubClient()
        issue = make_issue(61, label="implementing", body="brand new body")
        gh.add_issue(issue)
        gh.seed_state(
            61,
            user_content_hash="stale-hash",
            pickup_comment_id=900,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            # Three `_has_new_commits` calls: (1) the drift-no-session
            # "are there recovered commits to park on?" check
            # (False -- fall through), (2) the regular fresh-spawn-
            # branch's "recovered worktree?" check (False), (3) the
            # post-agent "did the spawn commit?" check (True).
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        # Fresh spawn ran; the implement prompt was built (not the
        # "issue body changed" resume prompt).
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        # No "issue body changed" notice was posted (we fell through to
        # the normal fresh-spawn path).
        self.assertFalse(any(
            "issue body changed" in body
            for _, body in gh.posted_comments
        ))
        # But the new hash is persisted.
        data = gh.pinned_data(61)
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")


class ImplementingDriftHeadShaDeltaTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: the implementing drift branch must compare HEAD
    SHA before/after the resume, not `_has_new_commits` (which only
    compares against `origin/<base>`). A worktree carrying pre-existing
    unpushed commits from a previous tick would otherwise mask an empty
    or failed resume and walk into `_on_commits` -> push -> open PR
    against commits that never had a chance to address the edited
    requirements."""

    def test_recovered_unpushed_commits_do_not_mask_empty_resume(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            850, label="implementing", body="new requirements",
        )
        gh.add_issue(issue)
        gh.seed_state(
            850,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            last_action_comment_id=100,
            branch="orchestrator/issue-850",
        )

        # The drift resume returns no new commit (`last_message=""` so
        # not an ack either -- this is a silent-failure shape). HEAD is
        # the same before and after, simulating a recovered worktree
        # carrying pre-existing unpushed commits from a prior tick: the
        # old SHA-agnostic `_has_new_commits` check would have returned
        # True (commits ahead of origin/base) and pushed a PR.
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message=""
            ),
            # has_new_commits would return True for the recovered
            # worktree; the drift branch must NOT consult it.
            has_new_commits=True,
            push_branch=True,
            head_shas=["recovered-sha", "recovered-sha"],
        )

        # The handler must NOT have opened a PR or flipped to
        # validating: the empty resume gave the dev no chance to
        # address the edited requirements.
        self.assertEqual(gh.opened_prs, [])
        self.assertNotIn((850, "validating"), gh.label_history)
        # Should fall to the silent-failure park via `_on_question`.
        data = gh.pinned_data(850)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_silent")


class ImplementingDriftNoDevSessionRecoveredCommitsTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: when implementing drift fires with NO recorded
    dev session AND the worktree carries recovered unpushed commits, the
    handler must refuse to push those commits and open a PR -- no agent
    has seen the edited issue body. Park awaiting human and let the
    operator decide whether to discard the recovered work or accept it."""

    def test_drift_with_recovered_commits_and_no_session_parks(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            860, label="implementing", body="updated requirements",
        )
        gh.add_issue(issue)
        # No `dev_session_id` recorded: legacy/recovered state. Pre-seed
        # `user_content_hash` so the drift detection fires (vs. silently
        # initializing the baseline on first encounter).
        gh.seed_state(
            860,
            user_content_hash="stale-hash",
            branch="orchestrator/issue-860",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            # Recovered worktree has unpushed commits ahead of base.
            has_new_commits=True,
            push_branch=True,
        )

        # Crucial: must NOT push or open a PR against commits the dev
        # never authored against the edited body.
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertNotIn((860, "validating"), gh.label_history)
        # Parked so the operator can adjudicate.
        data = gh.pinned_data(860)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("never saw the edited requirements", last_comment)
        # New hash baseline persisted so subsequent ticks don't keep
        # re-firing the drift park on the same edit.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")

    def test_drift_no_session_no_recovered_commits_falls_through(
        self,
    ) -> None:
        # The fall-through path is still correct when there are NO
        # recovered commits: a fresh spawn picks up the new body via
        # `_build_implement_prompt`.
        gh = FakeGitHubClient()
        issue = make_issue(861, label="implementing", body="new body")
        gh.add_issue(issue)
        gh.seed_state(
            861,
            user_content_hash="stale-hash",
            pickup_comment_id=900,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            # Three `_has_new_commits` calls: (1) drift-no-session park
            # check returns False -> fall through; (2) recovered-worktree
            # check in the regular path returns False; (3) post-agent
            # check returns True -> push + open PR.
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        # Fresh implement prompt ran (not the drift resume prompt).
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        # PR opened from the fresh spawn.
        self.assertEqual(len(gh.opened_prs), 1)


class ImplementingDriftAwaitingHumanNoDevSessionTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point: implementing drift with no recorded `dev_session_id`
    can still be `awaiting_human=True` (manual relabel, drift on a
    freshly-picked-up issue parked before its first spawn, etc.).
    Without the fix:
      * body-edit-only: falls through to `_resume_developer_on_human_reply`,
        finds no new comments, returns -- and the new hash is never
        written, so the drift loops every tick.
      * with new comment: fresh-spawns via `_resume_dev_with_text` with
        ONLY the new-comment text as the prompt, never quoting the
        updated body that triggered the drift.
    Fix: clear the park flags so the fresh-spawn path below fires with
    the full implement prompt (which quotes `issue.body` and the
    conversation via `_recent_comments_text`)."""

    def test_body_edit_only_clears_park_and_fresh_spawns(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1200, label="implementing", body="updated requirements",
        )
        # No prior dev session, but parked. Pre-seed `user_content_hash`
        # to a stale value so the drift detection fires (auto-seeding on
        # first encounter would hide the bug).
        gh.seed_state(
            1200,
            user_content_hash="stale-hash",
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            # Three `_has_new_commits` calls: (1) the drift-no-session
            # park-on-recovered-commits check returns False; (2) the
            # else-branch recovered-worktree check returns False;
            # (3) the post-agent commit detection returns True.
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        data = gh.pinned_data(1200)
        # The new hash is durably persisted -- the drift does NOT loop.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Park flags cleared so the fresh-spawn branch fired.
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        # The fresh implement prompt was used (NOT the resume-with-just-
        # comments prompt), so the dev sees the updated body.
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        self.assertIn("updated requirements", prompt)
        # PR opened from the fresh spawn.
        self.assertEqual(len(gh.opened_prs), 1)

    def test_body_edit_with_new_comment_uses_full_implement_prompt(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            1210, label="implementing", body="updated body",
        )
        # New human comment that triggers comment-driven resume in the
        # legacy code path -- the bug there fresh-spawns with ONLY the
        # comment text, missing the body context.
        human = FakeComment(
            id=500, body="here's more detail",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        gh.seed_state(
            1210,
            user_content_hash="stale-hash",
            awaiting_human=True,
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess", last_message="implemented"
            ),
            has_new_commits=[False, False, True],
            push_branch=True,
        )

        # Fresh implement prompt with the updated body AND the new
        # comment quoted via `_recent_comments_text`.
        prompt = mocks["run_agent"].call_args[0][1]
        self.assertIn("You are the implementer", prompt)
        self.assertIn("updated body", prompt)
        self.assertIn("here's more detail", prompt)
        # Comment marked consumed so the validating->in_review handoff
        # later won't classify it as fresh PR feedback.
        data = gh.pinned_data(1210)
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 500,
        )


class FullSpecPersistenceTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Issue #67: the pinned `dev_agent`/`decomposer_agent`/`review_agent`
    fields must store the full configured agent command (backend + CLI
    args), not just the parsed backend. This protects in-flight issues
    from a mid-flight env flip rewriting which CLI args run on subsequent
    resumes, and keeps legacy bare-backend pinned values and
    `codex_session_id` working unchanged.
    """

    _CODEX_SPEC = 'codex -m gpt-5.5 -c \'model_reasoning_effort="xhigh"\''
    _CODEX_ARGS = (
        "-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"',
    )
    _CLAUDE_SPEC = "claude --model claude-opus-4-7"
    _CLAUDE_ARGS = ("--model", "claude-opus-4-7")

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _patch_dev_config(spec: str, backend: str, args: tuple[str, ...]):
        return [
            patch.object(config, "DEV_AGENT_SPEC", spec),
            patch.object(config, "DEV_AGENT", backend),
            patch.object(config, "DEV_AGENT_ARGS", args),
        ]

    @staticmethod
    def _patch_review_config(spec: str, backend: str, args: tuple[str, ...]):
        return [
            patch.object(config, "REVIEW_AGENT_SPEC", spec),
            patch.object(config, "REVIEW_AGENT", backend),
            patch.object(config, "REVIEW_AGENT_ARGS", args),
        ]

    @staticmethod
    def _patch_decompose_config(spec: str, backend: str, args: tuple[str, ...]):
        return [
            patch.object(config, "DECOMPOSE_AGENT_SPEC", spec),
            patch.object(config, "DECOMPOSE_AGENT", backend),
            patch.object(config, "DECOMPOSE_AGENT_ARGS", args),
        ]

    def _enter(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # --- dev role --------------------------------------------------------

    def test_fresh_dev_spawn_stores_full_spec_with_args(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67001, label="implementing")
        gh.add_issue(issue)

        self._enter(self._patch_dev_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-67001", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs["extra_args"],
            self._CODEX_ARGS,
        )
        data = gh.pinned_data(67001)
        # Full spec verbatim, NOT just the parsed backend.
        self.assertEqual(data["dev_agent"], self._CODEX_SPEC)
        self.assertEqual(data["dev_session_id"], "sess-67001")

    def test_dev_resume_uses_stored_spec_after_env_flip(self) -> None:
        # Pinned state recorded codex+args. Even after a config flip to
        # plain claude, the resume MUST keep the recorded backend AND
        # the recorded args -- the new backend's CLI would reject codex
        # flags, and silently dropping them on a resume changes what
        # the agent actually sees mid-flight.
        gh = FakeGitHubClient()
        issue = make_issue(67002, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            67002,
            pr_number=67002,
            branch="orchestrator/issue-67002",
            dev_agent=self._CODEX_SPEC,
            dev_session_id="dev-67002",
            review_round=0,
        )
        # Config now points to plain claude (no args).
        self._enter(self._patch_dev_config(self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS))
        # Reviewer too -- we just want the dev-fix call to use the stored spec.
        self._enter(self._patch_review_config("claude", "claude", ()))

        review = _agent(
            session_id="rev-67002",
            last_message="1. Tighten\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-67002", last_message="fixed")

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "aaa", "bbb"],
        )

        # Two calls: reviewer (claude per config) then dev-fix (codex per
        # pinned state).
        self.assertEqual(mocks["run_agent"].call_count, 2)
        dev_call = mocks["run_agent"].call_args_list[1]
        self.assertEqual(dev_call.args[0], "codex")
        self.assertEqual(dev_call.kwargs.get("resume_session_id"), "dev-67002")
        # Args came from the stored spec, NOT the current config.
        self.assertEqual(dev_call.kwargs.get("extra_args"), self._CODEX_ARGS)

    def test_legacy_bare_backend_pinned_value_still_works(self) -> None:
        # An issue pinned with the pre-#67 bare-backend value (`"codex"`)
        # must still resume on codex with no args -- that is what those
        # deployments had at the time the session was spawned.
        gh = FakeGitHubClient()
        issue = make_issue(67003, label="implementing")
        issue.comments.append(
            FakeComment(id=2100, body="please retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            67003,
            awaiting_human=True,
            last_action_comment_id=2000,
            dev_agent="codex",  # legacy bare-backend pinned form.
            dev_session_id="dev-legacy-spec",
            branch="orchestrator/issue-67003",
        )

        # Flip current config to a spec with args -- which the resume must IGNORE.
        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-legacy-spec", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        # No args -- the legacy pinned form had none.
        self.assertEqual(call.kwargs.get("extra_args"), ())
        # No proactive migration -- a resume does NOT rewrite `dev_agent`.
        self.assertEqual(gh.pinned_data(67003).get("dev_agent"), "codex")

    def test_legacy_codex_session_id_resumes_with_codex_no_args(self) -> None:
        # The pre-rollout schema only had `codex_session_id`. Resume MUST
        # use codex regardless of any current config flip, and MUST pass
        # no args (the spec at the time was bare codex).
        gh = FakeGitHubClient()
        issue = make_issue(67004, label="implementing")
        issue.comments.append(
            FakeComment(id=2200, body="retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            67004,
            awaiting_human=True,
            last_action_comment_id=2100,
            codex_session_id="sess-legacy-67004",
            branch="orchestrator/issue-67004",
        )

        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-legacy-67004", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "sess-legacy-67004")
        self.assertEqual(call.kwargs.get("extra_args"), ())

    def test_poisoned_session_drop_preserves_full_spec(self) -> None:
        # After hitting the silent-park threshold, the resume drops the
        # poisoned session id and starts a fresh spawn. The stored
        # full spec MUST be preserved so the fresh spawn uses the same
        # backend+args (a poisoned session is a transcript problem,
        # not a backend-selection problem).
        gh = FakeGitHubClient()
        issue = make_issue(67005, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            67005,
            dev_agent=self._CODEX_SPEC,
            dev_session_id="poisoned-67005",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["agent"] = agent
            captured["resume_session_id"] = resume_session_id
            captured["extra_args"] = extra_args
            return _agent(session_id="fresh-67005", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(captured["agent"], "codex")
        self.assertIsNone(captured["resume_session_id"])
        # Critical: the args from the stored spec survived the drop.
        self.assertEqual(captured["extra_args"], self._CODEX_ARGS)
        # Stored spec is untouched -- not overwritten with the bare backend.
        self.assertEqual(state.get("dev_agent"), self._CODEX_SPEC)
        self.assertEqual(state.get("dev_session_id"), "fresh-67005")

    def test_poisoned_legacy_codex_session_pins_to_codex_before_clearing(self) -> None:
        # Legacy schema (only `codex_session_id`): a poisoned-session drop
        # must pin `dev_agent="codex"` before clearing the legacy field,
        # so a subsequent env flip to claude cannot retroactively switch
        # the backend.
        gh = FakeGitHubClient()
        issue = make_issue(67006, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            67006,
            codex_session_id="poisoned-legacy-67006",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["agent"] = agent
            captured["extra_args"] = extra_args
            return _agent(session_id="fresh-legacy-67006", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (the legacy implicit spec).
        self.assertEqual(captured["agent"], "codex")
        self.assertEqual(captured["extra_args"], ())
        # Migrated to the new key with the legacy backend pinned, legacy
        # field cleared.
        self.assertEqual(state.get("dev_agent"), "codex")
        self.assertEqual(state.get("dev_session_id"), "fresh-legacy-67006")
        self.assertIsNone(state.get("codex_session_id"))

    # --- reviewer role ---------------------------------------------------

    def test_fresh_reviewer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67010, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            67010,
            pr_number=67010,
            branch="orchestrator/issue-67010",
            dev_agent="claude",
            dev_session_id="dev-67010",
            review_round=0,
        )

        self._enter(self._patch_review_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67010",
                last_message="LGTM\n\nVERDICT: APPROVED",
            ),
        )

        # Reviewer ran with backend+args from current config.
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        # And the FULL spec is what gets persisted -- not just the backend.
        data = gh.pinned_data(67010)
        self.assertEqual(data["review_agent"], self._CODEX_SPEC)
        self.assertEqual(data["last_review_session_id"], "rev-67010")

    def test_reviewer_pr_comments_use_configured_backend_name(self) -> None:
        # Issue #67: reviewer trace/comments must not hardcode `codex` --
        # when the operator configures claude as the reviewer, the PR
        # comments must say so. We test both approval and CHANGES_REQUESTED
        # paths since both posted hardcoded text before the fix.
        gh = FakeGitHubClient()
        issue = make_issue(67011, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            67011,
            pr_number=67011,
            branch="orchestrator/issue-67011",
            dev_agent="codex",
            dev_session_id="dev-67011",
            review_round=0,
        )

        self._enter(self._patch_review_config("claude", "claude", ()))

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67011",
                last_message="LGTM\n\nVERDICT: APPROVED",
            ),
        )

        approval_comments = [
            body for (_, body) in gh.posted_pr_comments
            if "review approved" in body
        ]
        self.assertEqual(len(approval_comments), 1, approval_comments)
        self.assertIn("claude review approved", approval_comments[0])
        self.assertNotIn("codex review approved", approval_comments[0])

    def test_changes_requested_pr_comment_uses_configured_backend(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67012, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            67012,
            pr_number=67012,
            branch="orchestrator/issue-67012",
            dev_agent="codex",
            dev_session_id="dev-67012",
            review_round=0,
        )

        self._enter(self._patch_review_config("claude", "claude", ()))

        review = _agent(
            session_id="rev-67012",
            last_message="1. tighten\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-67012", last_message="fixed")

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "aaa", "bbb"],
        )

        bodies = [body for (_, body) in gh.posted_pr_comments]
        review_bodies = [b for b in bodies if "review (round" in b]
        self.assertEqual(len(review_bodies), 1, review_bodies)
        self.assertIn("claude review (round", review_bodies[0])
        self.assertNotIn("codex review (round", review_bodies[0])

    def test_review_prompt_describes_dev_backend_not_codex(self) -> None:
        # The reviewer prompt's intro line described the implementer as
        # "a separate codex session" before the fix, which is wrong when
        # claude is the dev backend. Build the prompt directly and
        # assert it reflects the dev backend.
        prompt = workflow._build_review_prompt(
            _TEST_SPEC,
            make_issue(67013),
            comments_text="",
            dev_backend="claude",
        )
        self.assertIn("A separate claude session", prompt)
        self.assertNotIn("A separate codex session", prompt)

    # --- decomposer role -------------------------------------------------

    def test_fresh_decomposer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67020, label="decomposing")
        gh.add_issue(issue)

        self._enter(self._patch_decompose_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        # Manifest "single" -- simplest successful decompose path.
        manifest = (
            "OK\n\n"
            "```orchestrator-manifest\n"
            '{"decision": "single", "rationale": "fits one context"}\n'
            "```\n"
        )
        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-67020", last_message=manifest),
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        data = gh.pinned_data(67020)
        # Full spec, not the bare backend.
        self.assertEqual(data["decomposer_agent"], self._CODEX_SPEC)
        self.assertEqual(data["decomposer_session_id"], "dec-67020")

    def test_decomposer_resume_uses_stored_spec_after_env_flip(self) -> None:
        # Pinned with the full codex spec. After DECOMPOSE_AGENT flips to
        # claude, the awaiting-human resume must still resume on codex
        # with the codex args, not retarget the next call to claude.
        gh = FakeGitHubClient()
        issue = make_issue(67021, label="decomposing", comments=[
            FakeComment(id=3000, body="park", user=FakeUser("orchestrator")),
            FakeComment(id=3010, body="please split", user=FakeUser("alice")),
        ])
        gh.add_issue(issue)
        gh.seed_state(
            67021,
            awaiting_human=True,
            last_action_comment_id=3000,
            decomposer_agent=self._CODEX_SPEC,
            decomposer_session_id="dec-67021",
        )

        self._enter(self._patch_decompose_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-67021",
                last_message=(
                    "OK\n\n"
                    "```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "ok"}\n'
                    "```\n"
                ),
            ),
        )

        # Resume call used the stored backend AND the stored args.
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dec-67021")
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        # Stored spec untouched.
        self.assertEqual(
            gh.pinned_data(67021).get("decomposer_agent"), self._CODEX_SPEC,
        )

    def test_legacy_bare_decomposer_backend_still_works(self) -> None:
        # `decomposer_agent="codex"` (no args) is the legacy pinned form;
        # it must continue to round-trip cleanly to `("codex", "codex", ())`.
        spec, backend, args, sid = workflow._read_decomposer_session(
            workflow.PinnedState(
                data={"decomposer_agent": "codex", "decomposer_session_id": "sid-x"},
            )
        )
        self.assertEqual(spec, "codex")
        self.assertEqual(backend, "codex")
        self.assertEqual(args, ())
        self.assertEqual(sid, "sid-x")

    # --- read-helper round-trip ------------------------------------------

    def test_read_dev_session_round_trips_full_spec(self) -> None:
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={
                    "dev_agent": self._CODEX_SPEC,
                    "dev_session_id": "sid-y",
                },
            )
        )
        self.assertEqual(spec, self._CODEX_SPEC)
        self.assertEqual(backend, "codex")
        self.assertEqual(args, self._CODEX_ARGS)
        self.assertEqual(sid, "sid-y")

    def test_read_dev_session_legacy_codex_session_id_path(self) -> None:
        # Even with a custom DEV_AGENT_SPEC in config, a legacy
        # codex_session_id-only state must yield codex with no args.
        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={"codex_session_id": "legacy-sid"},
            )
        )
        self.assertEqual(spec, "codex")
        self.assertEqual(backend, "codex")
        self.assertEqual(args, ())
        self.assertEqual(sid, "legacy-sid")

    def test_read_dev_session_unseeded_falls_back_to_current_config(self) -> None:
        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))
        spec, backend, args, sid = workflow._read_dev_session(workflow.PinnedState())
        self.assertEqual(spec, self._CLAUDE_SPEC)
        self.assertEqual(backend, "claude")
        self.assertEqual(args, self._CLAUDE_ARGS)
        self.assertIsNone(sid)

    # --- no-session-id regression (reviewer-flagged) ----------------------

    def test_dev_spec_pinned_even_when_spawn_returns_no_session_id(self) -> None:
        # A fresh dev spawn that produces commits but no session id (a
        # codex `-o` file the agent left empty, an unparseable claude
        # JSONL line, etc.) MUST still pin `dev_agent` to the full
        # configured spec. Without this, a subsequent `DEV_AGENT` flip
        # would silently retarget the next validating dev-fix resume
        # at a backend that never ran on this issue.
        gh = FakeGitHubClient()
        issue = make_issue(67030, label="implementing")
        gh.add_issue(issue)

        self._enter(self._patch_dev_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        # Empty session_id: backend hiccup, but the worktree got commits.
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        data = gh.pinned_data(67030)
        self.assertEqual(
            data.get("dev_agent"), self._CODEX_SPEC,
            "dev_agent must be pinned to the full spec even when the "
            "spawn returns no session_id",
        )
        # session_id was empty, so the legacy field stays absent.
        self.assertNotIn("dev_session_id", data)

    def test_dev_no_session_id_then_config_flip_resumes_recorded_spec(self) -> None:
        # Reviewer-requested scenario: spawn returns no session id but
        # commits/parks land. Operator then flips `DEV_AGENT` between
        # ticks. The next resume MUST stick with the spec that was
        # actually running, not retarget at the new config.
        gh = FakeGitHubClient()
        issue = make_issue(67031, label="implementing")
        gh.add_issue(issue)

        # First tick: codex spawn, no session id, agent question -> park
        # awaiting human.
        self._enter(self._patch_dev_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="need input"),
            has_new_commits=False,
        )
        data = gh.pinned_data(67031)
        self.assertEqual(data.get("dev_agent"), self._CODEX_SPEC)
        self.assertTrue(data.get("awaiting_human"))

        # Second tick: operator flipped `DEV_AGENT` to claude AND
        # provided new args; a human reply lands on the issue. The
        # resume MUST stick with codex+args from pinned state, NOT
        # retarget at the current claude config.
        issue.comments.append(
            FakeComment(id=4000, body="ok proceed", user=FakeUser("alice"))
        )

        # Switch config to claude (different backend + different args).
        # `_enter` schedules cleanup; start a fresh override block.
        for p in self._patch_dev_config(self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS):
            p.start()
            self.addCleanup(p.stop)

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-67031", last_message="done"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
            # The user-content-drift branch (which fires here because the
            # human's "ok proceed" comment changes the issue hash from
            # tick 1) snapshots HEAD before and after the resume to decide
            # whether THIS resume committed, so we need two SHA values
            # (different ones, so the post-resume "did the agent commit"
            # check goes through `_on_commits`). The drift path still
            # calls `_resume_dev_with_text` with the recorded codex spec,
            # so the call-arg assertions below still hold.
            head_shas=["before-sha", "after-sha"],
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(
            call.args[0], "codex",
            "resume must stick with the spec the first tick recorded, "
            "NOT the new DEV_AGENT after the flip",
        )
        self.assertEqual(
            call.kwargs.get("extra_args"), self._CODEX_ARGS,
            "stored codex args must survive across the config flip",
        )

    def test_decomposer_spec_pinned_even_when_spawn_returns_no_session_id(self) -> None:
        # Same reviewer concern, decomposer side: a fresh decomposer
        # that emits a manifest without surfacing a session id (or
        # parks awaiting human after a question) must still pin
        # `decomposer_agent` to the full spec.
        gh = FakeGitHubClient()
        issue = make_issue(67032, label="decomposing")
        gh.add_issue(issue)

        self._enter(self._patch_decompose_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        # No session_id, question-only output -> awaiting human park.
        # The spec must still land in pinned state so a later config
        # flip cannot retarget the awaiting-human resume.
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="please clarify"),
        )

        data = gh.pinned_data(67032)
        self.assertEqual(
            data.get("decomposer_agent"), self._CODEX_SPEC,
            "decomposer_agent must be pinned to the full spec even "
            "when the spawn returns no session_id",
        )
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn("decomposer_session_id", data)

    def test_decomposer_no_session_then_flip_resumes_recorded_spec(self) -> None:
        # Same config-flip scenario, decomposer side: the awaiting-
        # human resume must stick with the recorded spec.
        gh = FakeGitHubClient()
        issue = make_issue(67033, label="decomposing")
        gh.add_issue(issue)

        # First tick: codex decomposer, no session id, parks on a
        # clarification request.
        self._enter(self._patch_decompose_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="", last_message="please clarify scope",
            ),
        )
        data = gh.pinned_data(67033)
        self.assertEqual(data.get("decomposer_agent"), self._CODEX_SPEC)
        self.assertTrue(data.get("awaiting_human"))

        # Human replies; operator flips `DECOMPOSE_AGENT` to claude
        # between ticks. The resume must stick with codex+args.
        issue.comments.append(
            FakeComment(id=4100, body="single is fine", user=FakeUser("alice"))
        )
        for p in self._patch_decompose_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ):
            p.start()
            self.addCleanup(p.stop)

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-67033",
                last_message=(
                    "OK\n\n"
                    "```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "fits one context"}\n'
                    "```\n"
                ),
            ),
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(
            call.args[0], "codex",
            "decomposer resume must stick with the spec the first tick "
            "recorded, NOT the new DECOMPOSE_AGENT after the flip",
        )
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
