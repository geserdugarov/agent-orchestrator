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

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

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
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class SquashOnApprovalTest(unittest.TestCase, _PatchedWorkflowMixin):
    """After the reviewer agent emits VERDICT: APPROVED, the orchestrator
    squashes the dev's commits on the PR branch into one and force-pushes
    so the resulting PR is a single conventional-commit-shaped commit.
    Watermarks advance past the squash notice; the next in_review tick
    pings HITL without re-running the reviewer on the rewritten head.

    Failures (push rejected, lease violation, dirty tree) park
    awaiting_human and leave the original commits in place; SQUASH_ON_APPROVAL
    off preserves the legacy "leave the dev's commits as-is" behavior.
    """

    PR_NUMBER = 31
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-5"
    REVIEWED_SHA = "reviewedAA"
    SQUASHED_SHA = "squashedBB"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(5, label="validating", title="add a feature", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #31",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        # PR head SHA mirrors the post-squash remote head -- the force-push
        # inside the squash helper updates the remote, so by the time the
        # next gh.get_pr() is taken (inside _handle_validating's seeding
        # block, AND on the next in_review tick) the remote head matches
        # the new local SHA.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha=self.SQUASHED_SHA),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            5,
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_approval_squashes_and_lands_in_review_without_re_review(
        self,
    ) -> None:
        # End-to-end: validating approves, squash + force-push runs (mocked
        # to succeed), the squash PR comment is posted, the issue lands in
        # in_review, and the next in_review tick pings HITL WITHOUT
        # spawning the reviewer on the rewritten head.
        gh, issue, pr = self._setup()

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            mocks_v = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                # Squash: success, new local HEAD = SQUASHED_SHA, 3 commits
                # collapsed to 1.
                squash_result=(True, self.SQUASHED_SHA, 3, None),
            )

        # Squash helper was called exactly once on the approval path.
        self.assertEqual(mocks_v["_squash_and_force_push"].call_count, 1)
        # Reviewer ran once -- the only run_agent call on the approval path.
        self.assertEqual(mocks_v["run_agent"].call_count, 1)
        # Approval hands off through `documenting` (final docs pass);
        # `_handle_documenting`'s success exits advance unconditionally to
        # `in_review`. The squash / watermark state rides through the hop
        # untouched.
        self.assertIn((5, "documenting"), gh.label_history)
        data = gh.pinned_data(5)
        # The squash notice was posted to the PR conversation.
        squash_notice_posted = any(
            ":package: squashed 3 commits to 1" in body
            for _, body in gh.posted_pr_comments
        )
        self.assertTrue(
            squash_notice_posted,
            f"squash notice not posted; got: {gh.posted_pr_comments}",
        )
        # Watermark must include the squash comment so the next in_review
        # tick does not see it as fresh PR feedback once debounce expires.
        approval_and_squash_ids = [c.id for c in pr.issue_comments]
        self.assertTrue(approval_and_squash_ids)
        self.assertGreaterEqual(
            data.get("pr_last_comment_id"), max(approval_and_squash_ids),
            "pr_last_comment_id must advance past both the approval and "
            "the squash PR comments",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # Approved + mergeable; the ping MUST fire and must NOT re-run
        # the reviewer agent (its run_agent call would otherwise be
        # visible in mocks_r below).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for c in list(issue.comments) + list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago
        pr.approved = True
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks_r = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks_r["run_agent"].assert_not_called()
        # The orchestrator is manual-merge-only: the post-squash head
        # earns a HITL ping for the human to merge by hand. No
        # orchestrator-initiated merge call fires.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((5, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(5).get("ready_ping_sha"), self.SQUASHED_SHA,
        )

    def test_squash_failure_parks_awaiting_human_without_relabel(self) -> None:
        # Push rejected / lease violation / dirty tree all surface as
        # `success=False`. The orchestrator parks awaiting_human, leaves
        # the issue in `validating`, and does NOT seed watermarks (the
        # original commits remain on the branch and a human can decide
        # what to do).
        gh, issue, pr = self._setup()

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                squash_result=(
                    False, None, 0,
                    "force-push with lease rejected (concurrent update)",
                ),
            )

        self.assertEqual(mocks["_squash_and_force_push"].call_count, 1)
        # Park happened: awaiting_human flag set, HITL message posted to
        # the issue thread.
        data = gh.pinned_data(5)
        self.assertTrue(data.get("awaiting_human"))
        park_posted = any(
            "squash-on-approval failed" in body
            for _, body in gh.posted_comments
        )
        self.assertTrue(
            park_posted,
            f"HITL park message not posted; got: {gh.posted_comments}",
        )
        # No relabel to in_review or documenting -- the issue stays in
        # `validating` so the original commits remain on the branch.
        self.assertNotIn(
            (5, "in_review"), gh.label_history,
            "park must NOT relabel to in_review on squash failure",
        )
        self.assertNotIn(
            (5, "documenting"), gh.label_history,
            "park must NOT relabel to documenting (the final-docs hop) "
            "on squash failure",
        )

    def test_squash_off_preserves_legacy_behavior(self) -> None:
        # Kill switch: with SQUASH_ON_APPROVAL=off the squash helper must
        # NOT be called and no squash notice is posted.
        gh, issue, pr = self._setup()
        # Make pr.head.sha match REVIEWED_SHA -- legacy path: the local
        # HEAD the reviewer saw is what the remote PR points at, since no
        # force-push happened.
        pr.head = FakePRRef(sha=self.REVIEWED_SHA)

        with patch.object(config, "SQUASH_ON_APPROVAL", False):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
            )

        # Helper not called at all.
        mocks["_squash_and_force_push"].assert_not_called()
        # No squash notice posted.
        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # And the legacy approval flow flips to `documenting` (the
        # final-docs hop) regardless of SQUASH_ON_APPROVAL.
        self.assertIn((5, "documenting"), gh.label_history)

    def test_squash_with_only_one_commit_does_not_post_notice(self) -> None:
        # The helper returns `squashed_count=0` when there's only one
        # commit on top of base -- nothing to squash. The orchestrator
        # must skip the squash PR comment (the helper returns the same
        # SHA back).
        gh, issue, pr = self._setup()
        pr.head = FakePRRef(sha=self.REVIEWED_SHA)

        with patch.object(config, "SQUASH_ON_APPROVAL", True):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=(self.REVIEWED_SHA,),
                # Helper success no-op: nothing to squash.
                squash_result=(True, self.REVIEWED_SHA, 0, None),
            )

        for _, body in gh.posted_pr_comments:
            self.assertNotIn(":package: squashed", body)
        # Approval still flips to `documenting` (the final-docs hop)
        # even when there's only one commit (so no squash notice).
        self.assertIn((5, "documenting"), gh.label_history)


class SquashHelperRealGitTest(unittest.TestCase):
    """Integration test for `_squash_and_force_push` against a real git repo.

    The workflow-level squash tests above mock the helper itself, so they
    cannot catch failures in its rollback logic, in the squash-commit
    message construction, or in the lease-pinning. This class creates a
    bare remote + working clone with multiple commits on a topic branch,
    runs the helper directly, and asserts the on-disk state.
    """

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, env=env, check=True,
        )
        return r.stdout

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-squash-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        # Bare remote + working clone, base branch "main".
        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        # Identity for prep commits below; the orchestrator-owned squash
        # commit uses its own GIT_AUTHOR_*/GIT_COMMITTER_* env vars, so
        # this is just for the dev's pre-squash commits.
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        # Initial commit on main.
        (self.work / "README.md").write_text("hello\n")
        self._git("add", ".", cwd=self.work)
        self._git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        self._git("push", "origin", "main", cwd=self.work)

        # Topic branch with three dev commits.
        self.branch = "orchestrator/geserdugarov__agent-orchestrator/issue-9"
        self._git("checkout", "-b", self.branch, cwd=self.work)
        for i, msg in enumerate(["fix: typo", "add foo", "add bar"], start=1):
            (self.work / f"f{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )
        self._git("push", "origin", self.branch, cwd=self.work)
        self._git("fetch", "origin", cwd=self.work)

    def _make_issue(self, title: str = "test issue", number: int = 9):
        return make_issue(number, title=title)

    def _commits_on_branch(self) -> list[str]:
        """Subjects of all commits between origin/main and HEAD, oldest first."""
        out = self._git(
            "log", "--reverse", "--pretty=%s", "origin/main..HEAD",
            cwd=self.work,
        )
        return [s for s in out.splitlines() if s.strip()]

    def test_squash_collapses_three_commits_to_one(self) -> None:
        # First commit's subject ("fix: typo") is conventional-commit form,
        # so the squash subject reuses it. The squash message is
        # subject-only: the repo's Conventional-Commits-subject-only rule
        # forbids bodies on orchestrator-authored commits.
        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True):
            success, new_sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, f"expected success, got err={err!r}")
        self.assertIsNone(err)
        self.assertEqual(count, 3)
        self.assertTrue(new_sha)

        commits = self._commits_on_branch()
        self.assertEqual(
            len(commits), 1,
            f"expected one commit on top of base, got {commits!r}",
        )
        # Squash subject reuses the conventional-commit first subject.
        self.assertEqual(commits[0], "fix: typo")
        # Body is empty (subject-only commit): the repo's commit-style
        # rule forbids a body or trailer on orchestrator-authored
        # commits, so the squash MUST NOT carry the legacy
        # `Squashed commits: -...` listing.
        body = self._git(
            "log", "-1", "--pretty=%B", cwd=self.work,
        ).strip()
        self.assertEqual(body, "fix: typo")
        self.assertNotIn("Squashed commits:", body)

    def test_squash_uses_issue_title_when_no_conventional_first_subject(
        self,
    ) -> None:
        # Reset and rebuild the branch with non-conv-commit first subject.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        for i, msg in enumerate(["typo fix", "feat: add foo"], start=1):
            (self.work / f"g{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )

        issue = self._make_issue(title="rename frobnicator")
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)

        subject = self._git("log", "-1", "--pretty=%s", cwd=self.work).strip()
        self.assertEqual(subject, "feat: rename frobnicator")

    def test_squash_preserves_custom_repo_prefix_first_subject(self) -> None:
        # A repo-local first-commit prefix that is NOT a Conventional type
        # (e.g. a careers site's `career:`) must be reused verbatim as the
        # squash subject -- previously it would have been discarded for a
        # synthesized `feat: <issue title>`.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        for i, msg in enumerate(
            ["career: add a senior role", "fix wording"], start=1
        ):
            (self.work / f"c{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )

        issue = self._make_issue(title="hiring page")
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)
        subject = self._git("log", "-1", "--pretty=%s", cwd=self.work).strip()
        self.assertEqual(subject, "career: add a senior role")

    def test_squash_infers_repo_prefix_from_base_history(self) -> None:
        # No reusable first-commit subject, so the squash subject is
        # synthesized -- and it honors the repo-local `event:` prefix that
        # dominates recent base-branch history instead of defaulting to
        # `feat:`.
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        # Seed the base branch with a history dominated by `event:`.
        self._git("checkout", "main", cwd=self.work)
        for i, msg in enumerate(
            ["event: launch the site", "event: add a gala", "event: add a meetup"],
            start=1,
        ):
            (self.work / f"e{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )
        # Pushing updates the local `origin/main` tracking ref that
        # `_recent_base_subjects` reads.
        self._git("push", "origin", "main", cwd=self.work)
        # Rebuild the topic branch on the refreshed base with unprefixed
        # commits so the squash must fall back to inference.
        self._git("checkout", self.branch, cwd=self.work)
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        for i, msg in enumerate(["tweak the layout", "polish the copy"], start=1):
            (self.work / f"t{i}.txt").write_text(f"{i}\n")
            self._git("add", ".", cwd=self.work)
            self._git(
                "commit", "-m", msg, cwd=self.work, env_extra=author_env,
            )

        issue = self._make_issue(title="redesign the homepage")
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True):
            success, _, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, err)
        self.assertEqual(count, 2)
        subject = self._git("log", "-1", "--pretty=%s", cwd=self.work).strip()
        self.assertEqual(subject, "event: redesign the homepage")

    def test_squash_with_only_one_commit_is_a_no_op(self) -> None:
        # Reset to a single commit on top of base.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "only.txt").write_text("only\n")
        self._git("add", ".", cwd=self.work)
        self._git(
            "commit", "-m", "feat: only one", cwd=self.work,
            env_extra=author_env,
        )
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()

        issue = self._make_issue()
        push_mock = patch.object(branch_publication, "_push_branch", return_value=True)
        with patch.object(config, "BASE_BRANCH", "main"), push_mock as pm:
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success)
        self.assertEqual(count, 0)
        self.assertEqual(sha, original_head)
        # Single-commit branch must NOT trigger a push at all.
        pm.assert_not_called()
        # HEAD unchanged.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
        )

    def test_rollback_restores_branch_when_force_push_fails(self) -> None:
        # The whole point of saving original_head: a push failure after
        # the soft-reset + squash commit must not leave the branch
        # pointing at the squash commit. The original commits must still
        # be on the branch so the operator can decide what to do.
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()
        original_subjects = self._commits_on_branch()
        self.assertEqual(len(original_subjects), 3)

        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=False):
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertFalse(success)
        self.assertIsNone(sha)
        self.assertEqual(count, 0)
        self.assertIn("force-push", err or "")
        # HEAD restored.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
            "rollback must restore HEAD to the pre-squash SHA",
        )
        # All three original commits still on the branch.
        self.assertEqual(self._commits_on_branch(), original_subjects)
        # Working tree clean (rollback used --hard, but pre-reset tree
        # already matched HEAD's tree, so no file diffs should remain).
        status = self._git("status", "--porcelain", cwd=self.work)
        self.assertEqual(status.strip(), "")

    def test_squash_commit_uses_orchestrator_identity(self) -> None:
        # The squash commit must be authored under AGENT_GIT_NAME /
        # AGENT_GIT_EMAIL regardless of the dev's commit identity. This
        # keeps a single attribution for orchestrator-owned commits and
        # matches the agent-spawn `_agent_env` behavior.
        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True), \
             patch.object(config, "AGENT_GIT_NAME", "orch-bot"), \
             patch.object(
                 config, "AGENT_GIT_EMAIL", "orch-bot@example.com"
             ):
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertTrue(success, err)

        author = self._git(
            "log", "-1", "--pretty=%an <%ae>", cwd=self.work,
        ).strip()
        committer = self._git(
            "log", "-1", "--pretty=%cn <%ce>", cwd=self.work,
        ).strip()
        self.assertEqual(author, "orch-bot <orch-bot@example.com>")
        self.assertEqual(committer, "orch-bot <orch-bot@example.com>")

    def test_dirty_worktree_aborts_before_reset(self) -> None:
        # An uncommitted change in the worktree (the agent left work
        # behind) is a refuse-to-rewrite signal: the helper must abort
        # WITHOUT touching HEAD so the dirty state is visible to the
        # operator. Without the pre-reset dirty check the soft-reset
        # would happen and the rollback would clobber the dirty changes.
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()
        (self.work / "scratch.txt").write_text("uncommitted\n")

        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True) as pm:
            success, _, _, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertFalse(success)
        self.assertIn("uncommitted", (err or ""))
        # HEAD untouched, dirty file preserved, no push attempted.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
        )
        self.assertTrue((self.work / "scratch.txt").exists())
        pm.assert_not_called()

    def test_dirty_worktree_with_single_commit_still_fails(self) -> None:
        # The dirty-tree refusal is a precondition for the whole helper,
        # not just the rewrite path. A one-commit branch (squash would
        # be a no-op) with an uncommitted file must still fail so the
        # caller parks awaiting_human; otherwise the manual merge could
        # land the head with the operator's scratch invisible on the PR.
        self._git("reset", "--hard", "origin/main", cwd=self.work)
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "only.txt").write_text("only\n")
        self._git("add", ".", cwd=self.work)
        self._git(
            "commit", "-m", "feat: only one", cwd=self.work,
            env_extra=author_env,
        )
        original_head = self._git(
            "rev-parse", "HEAD", cwd=self.work,
        ).strip()
        (self.work / "scratch.txt").write_text("uncommitted\n")

        issue = self._make_issue()
        with patch.object(config, "BASE_BRANCH", "main"), \
             patch.object(branch_publication, "_push_branch", return_value=True) as pm:
            success, sha, count, err = workflow._squash_and_force_push(
                _TEST_SPEC, self.work, self.branch, issue,
            )
        self.assertFalse(success)
        self.assertIsNone(sha)
        self.assertEqual(count, 0)
        self.assertIn("uncommitted", (err or ""))
        # Single-commit + dirty path must NOT short-circuit to the
        # no-op success branch. HEAD untouched, dirty file preserved,
        # no push attempted.
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.work).strip(),
            original_head,
        )
        self.assertTrue((self.work / "scratch.txt").exists())
        pm.assert_not_called()
