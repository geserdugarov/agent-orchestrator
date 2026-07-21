# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestrator import branch_publication, config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

APPROVAL_ISSUE = 5
APPROVAL_PR = 31
APPROVAL_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-5"
REVIEWED_SHA = "reviewedAA"
SQUASHED_SHA = "squashedBB"
PICKUP_COMMENT_ID = 900
PR_OPEN_COMMENT_ID = 901
REVIEW_DEBOUNCE_SECONDS = 600
EXECUTABLE_MODE = 0o755
SQUASH_ON_APPROVAL = "SQUASH_ON_APPROVAL"
LABEL_DOCUMENTING = "documenting"
BASE_BRANCH_NAME = "main"
GIT_AUTHOR_NAME = "GIT_AUTHOR_NAME"
GIT_AUTHOR_EMAIL = "GIT_AUTHOR_EMAIL"
GIT_COMMITTER_NAME = "GIT_COMMITTER_NAME"
GIT_COMMITTER_EMAIL = "GIT_COMMITTER_EMAIL"
DEV_NAME = "Dev"
DEV_EMAIL = "dev@example.com"
GIT_ADD = "add"
GIT_COMMIT = "commit"
GIT_MESSAGE_FLAG = "-m"
REMOTE_NAME = "origin"
GIT_LOG = "log"
SUBJECT_FORMAT = "--pretty=%s"
BASE_BRANCH_SETTING = "BASE_BRANCH"
PUSH_BRANCH_HELPER = "_push_branch"
LAST_COMMIT = "-1"
GIT_RESET = "reset"
HARD_RESET = "--hard"
REMOTE_BASE_REF = "origin/main"
GIT_REV_PARSE = "rev-parse"
HEAD_REF = "HEAD"
SCRATCH_FILE = "scratch.txt"


class _SquashApprovalFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(
            APPROVAL_ISSUE,
            label="validating",
            title="add a feature",
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=":robot: orchestrator picking this up.",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #31",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        # PR head SHA mirrors the post-squash remote head -- the force-push
        # inside the squash helper updates the remote, so by the time the
        # next gh.get_pr() is taken (inside _handle_validating's seeding
        # block, AND on the next in_review tick) the remote head matches
        # the new local SHA.
        pr = FakePR(
            number=APPROVAL_PR,
            head_branch=APPROVAL_BRANCH,
            head=FakePRRef(sha=SQUASHED_SHA),
            mergeable=True,
            check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            APPROVAL_ISSUE,
            pr_number=APPROVAL_PR,
            branch=APPROVAL_BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PR_OPEN_COMMENT_ID],
            pickup_comment_id=PICKUP_COMMENT_ID,
        )
        return gh, issue, pr


class SquashOnApprovalTest(
    unittest.TestCase,
    _SquashApprovalFixtureMixin,
):
    """Squash approved branches and preserve the approval handoff."""

    def test_lands_in_review_without_re_review(
        self,
    ) -> None:
        # End-to-end: validating approves, squash + force-push runs (mocked
        # to succeed), the squash PR comment is posted, the issue lands in
        # in_review, and the next in_review tick pings HITL WITHOUT
        # spawning the reviewer on the rewritten head.
        gh, issue, pr = self._setup()

        with patch.object(config, SQUASH_ON_APPROVAL, True):
            mocks_v = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
                # Squash: success, new local HEAD = SQUASHED_SHA, 3 commits
                # collapsed to 1.
                squash_result=(True, SQUASHED_SHA, 3, None),
            )

        # Squash helper was called exactly once on the approval path.
        self.assertEqual(mocks_v["_squash_and_force_push"].call_count, 1)
        # Reviewer ran once -- the only run_agent call on the approval path.
        self.assertEqual(mocks_v["run_agent"].call_count, 1)
        # Approval hands off through `documenting` (final docs pass);
        # `_handle_documenting`'s success exits advance unconditionally to
        # `in_review`. The squash / watermark state rides through the hop
        # untouched.
        self.assertIn((APPROVAL_ISSUE, LABEL_DOCUMENTING), gh.label_history)
        state = gh.pinned_data(APPROVAL_ISSUE)
        # The squash notice was posted to the PR conversation.
        squash_notice_posted = any(":package: squashed 3 commits to 1" in body for _, body in gh.posted_pr_comments)
        self.assertTrue(
            squash_notice_posted,
            f"squash notice not posted; got: {gh.posted_pr_comments}",
        )
        # Watermark must include the squash comment so the next in_review
        # tick does not see it as fresh PR feedback once debounce expires.
        approval_and_squash_ids = [comment.id for comment in pr.issue_comments]
        self.assertTrue(approval_and_squash_ids)
        self.assertGreaterEqual(
            state.get("pr_last_comment_id"),
            max(approval_and_squash_ids),
            "pr_last_comment_id must advance past both the approval and the squash PR comments",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # Approved + mergeable; the ping MUST fire and must NOT re-run
        # the reviewer agent (its run_agent call would otherwise be
        # visible in mocks_r below).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for comment in list(issue.comments) + list(pr.issue_comments):
            if comment.created_at is None:
                comment.created_at = long_ago
        pr.approved = True
        if not any(label.name == "in_review" for label in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", REVIEW_DEBOUNCE_SECONDS):
            mocks_r = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks_r["run_agent"].assert_not_called()
        # The orchestrator is manual-merge-only: the post-squash head
        # earns a HITL ping for the human to merge by hand. No
        # orchestrator-initiated merge call fires.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((APPROVAL_ISSUE, "done"), gh.label_history)
        ping_comments = [body for _, body in gh.posted_comments if "ready for review/merge" in body]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(APPROVAL_ISSUE).get("ready_ping_sha"),
            SQUASHED_SHA,
        )

    def test_failure_parks_without_relabel(self) -> None:
        # Push rejected / lease violation / dirty tree all surface as
        # `success=False`. The orchestrator parks awaiting_human, leaves
        # the issue in `validating`, and does NOT seed watermarks (the
        # original commits remain on the branch and a human can decide
        # what to do).
        gh, issue, pr = self._setup()

        with patch.object(config, SQUASH_ON_APPROVAL, True):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
                squash_result=(
                    False,
                    None,
                    0,
                    "force-push with lease rejected (concurrent update)",
                ),
            )

        self.assertEqual(mocks["_squash_and_force_push"].call_count, 1)
        # Park happened: awaiting_human flag set, HITL message posted to
        # the issue thread.
        state = gh.pinned_data(APPROVAL_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        park_posted = any("squash-on-approval failed" in body for _, body in gh.posted_comments)
        self.assertTrue(
            park_posted,
            f"HITL park message not posted; got: {gh.posted_comments}",
        )
        # No relabel to in_review or documenting -- the issue stays in
        # `validating` so the original commits remain on the branch.
        self.assertNotIn(
            (APPROVAL_ISSUE, "in_review"),
            gh.label_history,
            "park must NOT relabel to in_review on squash failure",
        )
        self.assertNotIn(
            (APPROVAL_ISSUE, LABEL_DOCUMENTING),
            gh.label_history,
            "park must NOT relabel to documenting (the final-docs hop) on squash failure",
        )

    def test_squash_off_preserves_legacy_behavior(self) -> None:
        # Kill switch: with SQUASH_ON_APPROVAL=off the squash helper must
        # NOT be called and no squash notice is posted.
        gh, issue, pr = self._setup()
        # Make pr.head.sha match REVIEWED_SHA -- legacy path: the local
        # HEAD the reviewer saw is what the remote PR points at, since no
        # force-push happened.
        pr.head = FakePRRef(sha=REVIEWED_SHA)

        with patch.object(config, SQUASH_ON_APPROVAL, False):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
            )

        # Helper not called at all.
        mocks["_squash_and_force_push"].assert_not_called()
        # No squash notice posted.
        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # And the legacy approval flow flips to `documenting` (the
        # final-docs hop) regardless of SQUASH_ON_APPROVAL.
        self.assertIn((APPROVAL_ISSUE, LABEL_DOCUMENTING), gh.label_history)

    def test_single_commit_posts_no_notice(self) -> None:
        # The helper returns `squashed_count=0` when there's only one
        # commit on top of base -- nothing to squash. The orchestrator
        # must skip the squash PR comment (the helper returns the same
        # SHA back).
        gh, issue, pr = self._setup()
        pr.head = FakePRRef(sha=REVIEWED_SHA)

        with patch.object(config, SQUASH_ON_APPROVAL, True):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEWED_SHA,),
                # Helper success no-op: nothing to squash.
                squash_result=(True, REVIEWED_SHA, 0, None),
            )

        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # Approval still flips to `documenting` (the final-docs hop)
        # even when there's only one commit (so no squash notice).
        self.assertIn((APPROVAL_ISSUE, LABEL_DOCUMENTING), gh.label_history)


def _git(*args: str, cwd: Path, env_extra: dict | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if env_extra:
        env.update(env_extra)
    completed_process = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return completed_process.stdout


class _SquashGitFixtureMixin:
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-squash-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        # Bare remote + working clone, base branch "main".
        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", BASE_BRANCH_NAME, str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True,
            capture_output=True,
        )
        # Identity for prep commits below; the orchestrator-owned squash
        # commit uses its own GIT_AUTHOR_*/GIT_COMMITTER_* env vars, so
        # this is just for the dev's pre-squash commits.
        author_env = {
            GIT_AUTHOR_NAME: DEV_NAME,
            GIT_AUTHOR_EMAIL: DEV_EMAIL,
            GIT_COMMITTER_NAME: DEV_NAME,
            GIT_COMMITTER_EMAIL: DEV_EMAIL,
        }
        # Initial commit on main.
        (self.work / "README.md").write_text("hello\n")
        _git(GIT_ADD, ".", cwd=self.work)
        _git(GIT_COMMIT, GIT_MESSAGE_FLAG, "initial", cwd=self.work, env_extra=author_env)
        _git("push", REMOTE_NAME, BASE_BRANCH_NAME, cwd=self.work)

        # Topic branch with three dev commits.
        self.branch = "orchestrator/geserdugarov__agent-orchestrator/issue-9"
        _git("checkout", "-b", self.branch, cwd=self.work)
        for commit_index, msg in enumerate(
            ["fix: typo", "add foo", "add bar"],
            start=1,
        ):
            (self.work / f"f{commit_index}.txt").write_text(
                f"{commit_index}\n",
            )
            _git(GIT_ADD, ".", cwd=self.work)
            _git(
                GIT_COMMIT,
                GIT_MESSAGE_FLAG,
                msg,
                cwd=self.work,
                env_extra=author_env,
            )
        _git("push", REMOTE_NAME, self.branch, cwd=self.work)
        _git("fetch", REMOTE_NAME, cwd=self.work)

    def _make_issue(self, title: str = "test issue", number: int = 9):
        return make_issue(number, title=title)

    def _commits_on_branch(self) -> list[str]:
        """Subjects of all commits between origin/main and HEAD, oldest first."""
        out = _git(
            GIT_LOG,
            "--reverse",
            SUBJECT_FORMAT,
            "origin/main..HEAD",
            cwd=self.work,
        )
        return [line for line in out.splitlines() if line.strip()]


class SquashHelperRealGitTest(
    _SquashGitFixtureMixin,
    unittest.TestCase,
):
    """Build conventional squash commits against a real repository."""

    def test_squash_collapses_three_commits_to_one(self) -> None:
        # First commit's subject ("fix: typo") is conventional-commit form,
        # so the squash subject reuses it. The squash message is
        # subject-only: the repo's Conventional-Commits-subject-only rule
        # forbids bodies on orchestrator-authored commits.
        issue = self._make_issue()
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True),
        ):
            success, new_sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertTrue(success, f"expected success, got err={err!r}")
        self.assertIsNone(err)
        self.assertEqual(count, 3)
        self.assertTrue(new_sha)

        commits = self._commits_on_branch()
        self.assertEqual(
            len(commits),
            1,
            f"expected one commit on top of base, got {commits!r}",
        )
        # Squash subject reuses the conventional-commit first subject.
        self.assertEqual(commits[0], "fix: typo")
        # Body is empty (subject-only commit): the repo's commit-style
        # rule forbids a body or trailer on orchestrator-authored
        # commits, so the squash MUST NOT carry the legacy
        # `Squashed commits: -...` listing.
        body = _git(
            GIT_LOG,
            LAST_COMMIT,
            "--pretty=%B",
            cwd=self.work,
        ).strip()
        self.assertEqual(body, "fix: typo")
        self.assertNotIn("Squashed commits:", body)

    def test_issue_title_used_without_conventional(
        self,
    ) -> None:
        # Reset and rebuild the branch with non-conv-commit first subject.
        _git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        author_env = {
            GIT_AUTHOR_NAME: DEV_NAME,
            GIT_AUTHOR_EMAIL: DEV_EMAIL,
            GIT_COMMITTER_NAME: DEV_NAME,
            GIT_COMMITTER_EMAIL: DEV_EMAIL,
        }
        for commit_index, msg in enumerate(
            ["typo fix", "feat: add foo"],
            start=1,
        ):
            (self.work / f"g{commit_index}.txt").write_text(
                f"{commit_index}\n",
            )
            _git(GIT_ADD, ".", cwd=self.work)
            _git(
                GIT_COMMIT,
                GIT_MESSAGE_FLAG,
                msg,
                cwd=self.work,
                env_extra=author_env,
            )

        issue = self._make_issue(title="rename frobnicator")
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True),
        ):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)

        subject = _git(GIT_LOG, LAST_COMMIT, SUBJECT_FORMAT, cwd=self.work).strip()
        self.assertEqual(subject, "feat: rename frobnicator")

    def test_keeps_custom_prefix_first_subject(self) -> None:
        # A repo-local first-commit prefix that is NOT a Conventional type
        # (e.g. a careers site's `career:`) must be reused verbatim as the
        # squash subject -- previously it would have been discarded for a
        # synthesized `feat: <issue title>`.
        _git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        author_env = {
            GIT_AUTHOR_NAME: DEV_NAME,
            GIT_AUTHOR_EMAIL: DEV_EMAIL,
            GIT_COMMITTER_NAME: DEV_NAME,
            GIT_COMMITTER_EMAIL: DEV_EMAIL,
        }
        for commit_index, msg in enumerate(["career: add a senior role", "fix wording"], start=1):
            (self.work / f"c{commit_index}.txt").write_text(
                f"{commit_index}\n",
            )
            _git(GIT_ADD, ".", cwd=self.work)
            _git(
                GIT_COMMIT,
                GIT_MESSAGE_FLAG,
                msg,
                cwd=self.work,
                env_extra=author_env,
            )

        issue = self._make_issue(title="hiring page")
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True),
        ):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)
        subject = _git(GIT_LOG, LAST_COMMIT, SUBJECT_FORMAT, cwd=self.work).strip()
        self.assertEqual(subject, "career: add a senior role")

    def test_infers_prefix_from_base_history(self) -> None:
        # No reusable first-commit subject, so the squash subject is
        # synthesized -- and it honors the repo-local `event:` prefix that
        # dominates recent base-branch history instead of defaulting to
        # `feat:`.
        author_env = {
            GIT_AUTHOR_NAME: DEV_NAME,
            GIT_AUTHOR_EMAIL: DEV_EMAIL,
            GIT_COMMITTER_NAME: DEV_NAME,
            GIT_COMMITTER_EMAIL: DEV_EMAIL,
        }
        # Seed the base branch with a history dominated by `event:`.
        _git("checkout", BASE_BRANCH_NAME, cwd=self.work)
        for commit_index, msg in enumerate(
            ["event: launch the site", "event: add a gala", "event: add a meetup"],
            start=1,
        ):
            (self.work / f"e{commit_index}.txt").write_text(
                f"{commit_index}\n",
            )
            _git(GIT_ADD, ".", cwd=self.work)
            _git(
                GIT_COMMIT,
                GIT_MESSAGE_FLAG,
                msg,
                cwd=self.work,
                env_extra=author_env,
            )
        # Pushing updates the local `origin/main` tracking ref that
        # `_recent_base_subjects` reads.
        _git("push", REMOTE_NAME, BASE_BRANCH_NAME, cwd=self.work)
        # Rebuild the topic branch on the refreshed base with unprefixed
        # commits so the squash must fall back to inference.
        _git("checkout", self.branch, cwd=self.work)
        _git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        for commit_index, msg in enumerate(
            ["tweak the layout", "polish the copy"],
            start=1,
        ):
            (self.work / f"t{commit_index}.txt").write_text(
                f"{commit_index}\n",
            )
            _git(GIT_ADD, ".", cwd=self.work)
            _git(
                GIT_COMMIT,
                GIT_MESSAGE_FLAG,
                msg,
                cwd=self.work,
                env_extra=author_env,
            )

        issue = self._make_issue(title="redesign the homepage")
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True),
        ):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)
        subject = _git(GIT_LOG, LAST_COMMIT, SUBJECT_FORMAT, cwd=self.work).strip()
        self.assertEqual(subject, "event: redesign the homepage")


class SquashHelperRecoveryRealGitTest(
    _SquashGitFixtureMixin,
    unittest.TestCase,
):
    """Preserve branches and worktrees across no-op and failure paths."""

    def test_squash_with_only_one_commit_is_a_no_op(self) -> None:
        # Reset to a single commit on top of base.
        _git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        author_env = {
            GIT_AUTHOR_NAME: DEV_NAME,
            GIT_AUTHOR_EMAIL: DEV_EMAIL,
            GIT_COMMITTER_NAME: DEV_NAME,
            GIT_COMMITTER_EMAIL: DEV_EMAIL,
        }
        (self.work / "only.txt").write_text("only\n")
        _git(GIT_ADD, ".", cwd=self.work)
        _git(
            GIT_COMMIT,
            GIT_MESSAGE_FLAG,
            "feat: only one",
            cwd=self.work,
            env_extra=author_env,
        )
        original_head = _git(
            GIT_REV_PARSE,
            HEAD_REF,
            cwd=self.work,
        ).strip()

        issue = self._make_issue()
        push_mock = patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True)
        with patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME), push_mock as pm:
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertTrue(success)
        self.assertEqual(count, 0)
        self.assertEqual(sha, original_head)
        # Single-commit branch must NOT trigger a push at all.
        pm.assert_not_called()
        # HEAD unchanged.
        self.assertEqual(
            _git(GIT_REV_PARSE, HEAD_REF, cwd=self.work).strip(),
            original_head,
        )

    def test_push_failure_rollback_restores_branch(self) -> None:
        # The whole point of saving original_head: a push failure after
        # the soft-reset + squash commit must not leave the branch
        # pointing at the squash commit. The original commits must still
        # be on the branch so the operator can decide what to do.
        original_head = _git(
            GIT_REV_PARSE,
            HEAD_REF,
            cwd=self.work,
        ).strip()
        original_subjects = self._commits_on_branch()
        self.assertEqual(len(original_subjects), 3)

        issue = self._make_issue()
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=False),
        ):
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertFalse(success)
        self.assertIsNone(sha)
        self.assertEqual(count, 0)
        self.assertIn("force-push", err or "")
        # HEAD restored.
        self.assertEqual(
            _git(GIT_REV_PARSE, HEAD_REF, cwd=self.work).strip(),
            original_head,
            "rollback must restore HEAD to the pre-squash SHA",
        )
        # All three original commits still on the branch.
        self.assertEqual(self._commits_on_branch(), original_subjects)
        # Working tree clean (rollback used --hard, but pre-reset tree
        # already matched HEAD's tree, so no file diffs should remain).
        status = _git("status", "--porcelain", cwd=self.work)
        self.assertEqual(status.strip(), "")

    def test_never_executes_planted_fsmonitor(self) -> None:
        # Every index-refreshing git command in the squash helper -- the
        # pre-rewrite dirty check, the soft reset, the squash commit, and the
        # post-push rollback `reset --hard` -- runs inside a worktree whose
        # `.git/config` the agent can write. A planted `core.fsmonitor` helper
        # would run during any of them with the orchestrator's process
        # environment (ambient secrets) attached, so each must go through the
        # hardened git path that disables fsmonitor. This drives the whole
        # helper to the rollback branch (push mocked to fail) and asserts the
        # planted hook fired NOWHERE inside it -- while first proving the hook
        # is genuinely usable, so the negative assertion is not vacuous.
        marker = self.tmpdir / "fsmonitor_invocations.txt"
        hook = self.tmpdir / "fsmonitor_hook.sh"
        # Hook + marker live outside the worktree so they don't show up as
        # untracked files (which would trip the dirty check). The hook records
        # the invoking git command from the parent process's cmdline so a
        # failure names the offending command. The `/`+NUL response is
        # fsmonitor v1 for "assume everything changed" -- a scan hint only, so
        # a genuinely clean tree still reads clean.
        hook.write_text(
            "#!/bin/sh\n"
            "tr '\\0' ' ' < /proc/$PPID/cmdline >> '" + str(marker) + "'\n"
            "printf '\\n' >> '" + str(marker) + "'\n"
            "printf '/\\000'\n"
        )
        hook.chmod(EXECUTABLE_MODE)
        _git("config", "core.fsmonitor", str(hook), cwd=self.work)

        # Prove the planted hook is honored by this worktree config: a plain,
        # unhardened index refresh fires it. Without this the empty-marker
        # assertion below could pass simply because the hook was never wired.
        _git("status", "--porcelain", cwd=self.work)
        self.assertTrue(
            marker.exists() and marker.read_text().strip(),
            "planted fsmonitor never fired even for a plain git status; the test cannot detect a regression",
        )
        marker.unlink()

        original_head = _git(
            GIT_REV_PARSE,
            HEAD_REF,
            cwd=self.work,
        ).strip()
        original_subjects = self._commits_on_branch()
        self.assertEqual(len(original_subjects), 3)

        issue = self._make_issue()
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=False),
        ):
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )

        fired = marker.read_text() if marker.exists() else ""
        # The security property: no git command inside the squash helper
        # executed the planted fsmonitor. A plain `_git` dirty check / reset
        # would appear here with the orchestrator environment attached.
        self.assertEqual(
            fired,
            "",
            f"a git command inside the squash helper executed the planted fsmonitor: {fired!r}",
        )
        # Push failed, so the rollback ran and restored the original commits.
        self.assertFalse(success)
        self.assertIn("force-push", err or "")
        self.assertEqual(
            _git(GIT_REV_PARSE, HEAD_REF, cwd=self.work).strip(),
            original_head,
            "rollback must restore HEAD to the pre-squash SHA",
        )
        self.assertEqual(self._commits_on_branch(), original_subjects)

    def test_squash_commit_uses_orchestrator_identity(self) -> None:
        # The squash commit must be authored under AGENT_GIT_NAME /
        # AGENT_GIT_EMAIL regardless of the dev's commit identity. This
        # keeps a single attribution for orchestrator-owned commits and
        # matches the agent-spawn `_agent_env` behavior.
        issue = self._make_issue()
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True),
            patch.object(config, "AGENT_GIT_NAME", "orch-bot"),
            patch.object(config, "AGENT_GIT_EMAIL", "orch-bot@example.com"),
        ):
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertTrue(success, err)

        author = _git(
            GIT_LOG,
            LAST_COMMIT,
            "--pretty=%an <%ae>",
            cwd=self.work,
        ).strip()
        committer = _git(
            GIT_LOG,
            LAST_COMMIT,
            "--pretty=%cn <%ce>",
            cwd=self.work,
        ).strip()
        self.assertEqual(author, "orch-bot <orch-bot@example.com>")
        self.assertEqual(committer, "orch-bot <orch-bot@example.com>")

    def test_dirty_worktree_aborts_before_reset(self) -> None:
        # An uncommitted change in the worktree (the agent left work
        # behind) is a refuse-to-rewrite signal: the helper must abort
        # WITHOUT touching HEAD so the dirty state is visible to the
        # operator. Without the pre-reset dirty check the soft-reset
        # would happen and the rollback would clobber the dirty changes.
        original_head = _git(
            GIT_REV_PARSE,
            HEAD_REF,
            cwd=self.work,
        ).strip()
        (self.work / SCRATCH_FILE).write_text("uncommitted\n")

        issue = self._make_issue()
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True) as pm,
        ):
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertFalse(success)
        self.assertIn("uncommitted", (err or ""))
        # HEAD untouched, dirty file preserved, no push attempted.
        self.assertEqual(
            _git(GIT_REV_PARSE, HEAD_REF, cwd=self.work).strip(),
            original_head,
        )
        self.assertTrue((self.work / SCRATCH_FILE).exists())
        pm.assert_not_called()

    def test_dirty_single_commit_still_fails(self) -> None:
        # The dirty-tree refusal is a precondition for the whole helper,
        # not just the rewrite path. A one-commit branch (squash would
        # be a no-op) with an uncommitted file must still fail so the
        # caller parks awaiting_human; otherwise the manual merge could
        # land the head with the operator's scratch invisible on the PR.
        _git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        author_env = {
            GIT_AUTHOR_NAME: DEV_NAME,
            GIT_AUTHOR_EMAIL: DEV_EMAIL,
            GIT_COMMITTER_NAME: DEV_NAME,
            GIT_COMMITTER_EMAIL: DEV_EMAIL,
        }
        (self.work / "only.txt").write_text("only\n")
        _git(GIT_ADD, ".", cwd=self.work)
        _git(
            GIT_COMMIT,
            GIT_MESSAGE_FLAG,
            "feat: only one",
            cwd=self.work,
            env_extra=author_env,
        )
        original_head = _git(
            GIT_REV_PARSE,
            HEAD_REF,
            cwd=self.work,
        ).strip()
        (self.work / SCRATCH_FILE).write_text("uncommitted\n")

        issue = self._make_issue()
        with (
            patch.object(config, BASE_BRANCH_SETTING, BASE_BRANCH_NAME),
            patch.object(branch_publication, PUSH_BRANCH_HELPER, return_value=True) as pm,
        ):
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue,
            )
        self.assertFalse(success)
        self.assertIsNone(sha)
        self.assertEqual(count, 0)
        self.assertIn("uncommitted", (err or ""))
        # Single-commit + dirty path must NOT short-circuit to the
        # no-op success branch. HEAD untouched, dirty file preserved,
        # no push attempted.
        self.assertEqual(
            _git(GIT_REV_PARSE, HEAD_REF, cwd=self.work).strip(),
            original_head,
        )
        self.assertTrue((self.work / SCRATCH_FILE).exists())
        pm.assert_not_called()
