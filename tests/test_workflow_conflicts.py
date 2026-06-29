# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.github import BASE_SYNC_HOLD_LABEL
from orchestrator.stages import conflicts

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
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleResolvingConflictUsesAuthedFetchTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The conflict-resolution fetch must run inside the agent-writable
    worktree under the same security envelope as `_push_branch`: askpass-
    based auth, detached global/system config, blocked hooks/fsmonitor/
    credential helpers. `_handle_resolving_conflict` MUST route the
    fetch through `_authed_fetch` (not plain `_git`) so a planted url
    rewrite / credential helper / hooksPath cannot exfiltrate the token.
    """

    def test_fetch_call_targets_authed_fetch_with_explicit_refspec(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        issue = make_issue(450, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=850, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-450",
            head=FakePRRef(sha="cafe1234"),
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            450, pr_number=850, branch="orchestrator/geserdugarov__agent-orchestrator/issue-450",
            dev_agent="claude", dev_session_id="dev-sess",
            conflict_round=0,
        )

        merge_mock = MagicMock(return_value=(True, []))

        # The mixin's `_run` itself patches `_authed_fetch` to a default
        # success mock, so we read the call back from the returned
        # mocks dict rather than installing our own outer patch (which
        # `_run`'s inner `with` would override).
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                head_shas=["sha", "sha"],
            )

        authed_fetch_mock = mocks["_authed_fetch"]
        # Two fetches per fresh resolving_conflict round: first for the
        # PR branch (so the SHA-alignment / unpushed-recovery check sees
        # current `origin/<branch>`), then for the base branch (so the
        # upcoming `git rebase` sees current `origin/<base>`).
        self.assertEqual(authed_fetch_mock.call_count, 2)
        refspecs = [call.args[1] for call in authed_fetch_mock.call_args_list]
        cwds = [call.kwargs["cwd"] for call in authed_fetch_mock.call_args_list]
        # All fetches run inside the WORKTREE (agent-writable), where
        # the hardening actually matters -- not `target_root`.
        for cwd in cwds:
            self.assertEqual(cwd, _FAKE_WT)
        # All refspecs use the explicit `+refs/heads/X:refs/remotes/origin/X`
        # form so single-branch clones still create the remote-tracking ref.
        for refspec in refspecs:
            self.assertTrue(
                refspec.startswith("+"),
                f"refspec {refspec!r} should start with '+' for force-update",
            )
        # Verify both refs are fetched: the PR branch and the base branch.
        joined = " ".join(refspecs)
        self.assertIn(
            f"refs/remotes/origin/{_TEST_SPEC.base_branch}", joined,
            "expected base-branch fetch refspec",
        )
        self.assertIn(
            "refs/remotes/origin/orchestrator/geserdugarov__agent-orchestrator/issue-450", joined,
            "expected PR-branch fetch refspec",
        )


class HandleResolvingConflictTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Drive `_handle_resolving_conflict` through the rebase / push / cap /
    PR-state branches with `_git`, `_rebase_base_into_worktree`, and the
    push helper mocked so no shell-out happens.
    """

    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-200"
    PR_NUMBER = 800

    def _seed(
        self,
        *,
        merge_succeeded: bool = True,
        conflicted_files=(),
        head_shas=("before", "after"),
        push_branch: bool = True,
        run_agent_result=None,
        pr_state: str = "open",
        pr_merged: bool = False,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(200, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=False, check_state="success",
            merged=pr_merged, state=pr_state,
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=2,
            conflict_round=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(200, **state)
        return gh, issue, pr

    def _run_with_merge(
        self,
        gh,
        issue,
        *,
        merge_succeeded: bool,
        conflicted_files=(),
        head_shas=("before", "after"),
        push_branch: bool = True,
        run_agent_result=None,
        fetch_returncode: int = 0,
        dirty_files=(),
        rebase_in_progress: bool = False,
    ):
        from unittest.mock import MagicMock

        agent = run_agent_result or _agent(
            session_id="dev-sess", last_message="resolved",
        )
        merge_mock = MagicMock(
            return_value=(merge_succeeded, list(conflicted_files))
        )
        fetch_result = MagicMock(returncode=fetch_returncode, stdout="", stderr="")
        # `_git_hardened` is what the fetch in `_handle_resolving_conflict`
        # actually calls; `_git` covers the diff helper inside the merge
        # wrapper. Both must be mocked or the real subprocess.run() fires
        # on `_FAKE_WT`.
        git_mock = MagicMock(return_value=fetch_result)
        git_hardened_mock = MagicMock(return_value=fetch_result)
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_hardened_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=agent,
                push_branch=push_branch,
                head_shas=head_shas,
                dirty_files=dirty_files,
                rebase_in_progress=rebase_in_progress,
            )
        return mocks, merge_mock, git_mock

    def test_clean_rebase_pushes_and_flips_to_validating(self) -> None:
        # A clean base rebase that actually moved HEAD pushes the
        # rebased branch and hands straight back to `validating`. Docs
        # do not run here -- the single docs pass runs after reviewer
        # approval before `in_review` via the final-docs handoff.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent must NOT be spawned -- a clean base rebase does not need
        # the dev to do anything.
        mocks["run_agent"].assert_not_called()
        merge_mock.assert_called_once()
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease="beforehead",
        )
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)

    def test_hold_base_sync_label_pauses_resolving_conflict(self) -> None:
        gh, issue, pr = self._seed()
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(200)
        self.assertEqual(data.get("conflict_round"), 0)
        self.assertFalse(data.get("awaiting_human"))

    def test_clean_rebase_already_up_to_date_skips_push_and_ticks_round(
        self,
    ) -> None:
        # When the base hasn't moved (e.g. unmergeability is purely due to
        # branch protection), the rebase is a no-op and there is nothing to
        # push. The handler must still increment `conflict_round` so the
        # cap eventually fires -- otherwise the in_review <-> resolving
        # cycle would loop forever. The label hands back to `validating`
        # so the next reviewer round / in_review tick can re-evaluate;
        # every other resolving_conflict exit also targets `validating`
        # now, so there's no `documenting` detour to skip relative to
        # the pushed paths.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
        )
        mocks["run_agent"].assert_not_called()
        # Nothing to push when base hasn't moved relative to the branch.
        mocks["_push_branch"].assert_not_called()
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)

    def test_no_op_rebase_loops_until_cap_fires(self) -> None:
        # A PR stuck unmergeable purely due to branch protection would
        # bounce between in_review and resolving_conflict with the rebase
        # always a no-op. The cap must fire after MAX_CONFLICT_ROUNDS
        # such no-op rounds.
        gh, issue, pr = self._seed(extra_state={"conflict_round": 2})
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks, merge_mock, git_mock = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,
                head_shas=["samehead", "samehead"],
                push_branch=True,
            )
        # One more no-op round consumed: 2 -> 3.
        self.assertEqual(gh.pinned_data(200).get("conflict_round"), 3)
        # On the next tick we'd be at the cap; simulate by re-running:
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks2, merge_mock2, _ = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,
                head_shas=["samehead", "samehead"],
                push_branch=True,
            )
        merge_mock2.assert_not_called()
        self.assertTrue(gh.pinned_data(200).get("awaiting_human"))

    def test_conflict_resolved_by_agent_pushes_and_flips_to_validating(
        self,
    ) -> None:
        # Agent-resolved conflict push pushes the resolved branch and
        # hands straight back to `validating`. Docs do not run here --
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py", "b.py"],
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent IS spawned with the conflict-resolution prompt.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("a.py", prompt)
        self.assertIn("b.py", prompt)
        self.assertIn("rebase", prompt.lower())
        self.assertIn("git rebase --skip", prompt)
        self.assertIn("git commit --allow-empty", prompt)
        self.assertIn("git rebase --abort", prompt)
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease="beforehead",
        )
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)

    def test_cap_exhausted_parks_awaiting_human(self) -> None:
        # `MAX_CONFLICT_ROUNDS` defaults to 3; once the counter reaches it,
        # the handler must park instead of attempting another round.
        gh, issue, pr = self._seed(extra_state={"conflict_round": 3})
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks, merge_mock, git_mock = self._run_with_merge(
                gh, issue, merge_succeeded=True,
            )
        # Neither merge nor agent runs on the cap branch.
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # Label stays on `resolving_conflict` -- no flip.
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "done"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("MAX_CONFLICT_ROUNDS", last_comment)

    def test_pr_already_merged_externally_finalizes_to_done(self) -> None:
        # Mirror the in_review terminal: a human merged the PR (perhaps
        # after manually resolving conflicts) while we were resolving.
        gh, issue, pr = self._seed(pr_merged=True, pr_state="closed")
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        # No merge / agent / push attempt -- terminal short-circuit.
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((200, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(200))
        self.assertTrue(issue.closed)

    def test_pr_closed_unmerged_finalizes_to_rejected(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed")
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertIn((200, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(200))
        # PR is gone -- the orchestrator-owned branch and worktree must
        # come down on the rejected terminal too, mirroring the merged
        # path. Failure to clean up here is exactly the bug this test
        # guards against.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 200,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-200",
        )

    def test_manually_closed_with_open_pr_marks_rejected_without_cleanup(
        self,
    ) -> None:
        # Mirror the in_review counterpart: closing the issue while the
        # PR is still open is a human stop signal. The handler flips the
        # label to `rejected` but deliberately leaves the branch /
        # worktree alone (operator may still want to salvage the PR).
        gh, issue, pr = self._seed(pr_state="open")
        issue.closed = True
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertIn((200, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(200))
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Documented caveat: a subsequent PR close is not observed by
        # the orchestrator -- the closed-issue sweep only covers
        # `in_review` / `resolving_conflict`, and `rejected` is terminal
        # in the dispatcher. Operator must clean up by hand.
        pr.state = "closed"
        pollable_numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertNotIn(
            200, pollable_numbers,
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_agent_timeout_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", timed_out=True,
            ),
        )
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # Label stays on resolving_conflict -- the dispatcher will keep
        # routing here until the operator clears the park.
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("timed out", last_comment)

    def test_agent_left_dirty_worktree_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(False, ["a.py"]))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        # Note: the mixin's `_run` patches `_worktree_dirty_files` itself,
        # so wire dirty_files through the kwarg rather than a separate
        # outer patch (which `_run`'s patch would override).
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(
                    session_id="dev-sess", last_message="halfway there",
                ),
                push_branch=True,
                head_shas=["beforehead", "after"],
                dirty_files=["a.py"],
            )

        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_agent_left_rebase_in_progress_parks_without_push(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            push_branch=True,
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="I resolved one stop but another remains",
            ),
            rebase_in_progress=True,
        )

        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rebase is still in progress", last_comment)
        self.assertIn("I resolved one stop", last_comment)

    def test_push_failure_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "merged"],
            push_branch=False,
        )
        # Agent ran successfully and committed, but the push failed.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # No label flip -- still resolving_conflict.
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_awaiting_human_no_new_comments_is_quiet(self) -> None:
        # Once parked, ticks without a new human reply must not retry --
        # otherwise the cap is meaningless and a poisoned rebase would
        # burn tokens. The parked state stays put.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                # Watermark above any comment so `comments_after` is empty.
                "last_action_comment_id": 999_999,
            },
        )
        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_awaiting_human_with_new_comment_resumes_dev(self) -> None:
        # `_on_question` / `_on_dirty_worktree` parks tell the human
        # "reply with guidance and the orchestrator will resume the
        # session". Honor that contract: a fresh comment past the
        # watermark must resume the dev on the in-progress rebase
        # worktree, NOT keep the issue stuck until a manual relabel.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        # Fresh comment above the watermark.
        issue.comments.append(
            FakeComment(
                id=2000, body="try harder; conflict in foo.py is structural",
                user=FakeUser("alice"),
            )
        )

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,  # unused on resume path
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )

        # Resume runs the agent with the human's text; rebase is NOT
        # re-attempted (the worktree is mid-rebase already).
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("try harder", prompt)
        # The bare human-reply followup must carry the foreground-only
        # execution-model note -- a resumed dev that backgrounds a slow
        # test run and ends its turn "to check later" strands the issue
        # (the job dies with the session).
        self.assertIn("NEVER start a background job", prompt)
        merge_mock.assert_not_called()
        # Successful resume pushes the branch and hands straight back
        # to `validating`. Docs do not run here -- the single docs pass
        # runs after reviewer approval before `in_review` via the
        # final-docs handoff.
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease=None,
        )
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 2)
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        # Watermark advanced past the consumed comment.
        self.assertEqual(data.get("last_action_comment_id"), 2000)

    def _seed_with_baseline_hash(self, gh, issue, **extra):
        # Re-seed pinned state with a matching `user_content_hash` (plus any
        # extra fields) so the drift detector's first-encounter persistence
        # doesn't itself write -- these interrupted tests assert ZERO writes.
        data = gh.pinned_data(200)
        data.update(extra)
        data["user_content_hash"] = workflow._compute_user_content_hash(
            issue, set(),
        )
        gh.seed_state(200, **data)

    def test_conflict_resolution_interrupted_leaves_state_untouched(self) -> None:
        # A dev run spawned to resolve the rebase conflict, but the shutdown
        # sweep killed it mid-flight. The partial result must be ignored:
        # `_post_conflict_resolution_result` returns WITHOUT writing pinned
        # state, so durable state stays retryable -- no park, no flip, no
        # round increment, no push off the partial tree.
        gh, issue, pr = self._seed()
        self._seed_with_baseline_hash(gh, issue)
        before_writes = gh.write_state_calls

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", interrupted=True,
            ),
        )

        # The conflict-resolution dev run spawned, then was seen interrupted.
        mocks["run_agent"].assert_called_once()
        self.assertEqual(gh.write_state_calls, before_writes)
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertFalse(data.get("awaiting_human"))
        # `conflict_round` not bumped and no flip back to validating.
        self.assertEqual(data.get("conflict_round"), 0)
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertFalse(any(
            "timed out" in body
            or "rebase is still in progress" in body
            or "agent needs your input" in body
            or "git push failed" in body
            for _, body in gh.posted_comments
        ))

    def test_awaiting_human_resume_interrupted_does_not_consume_reply(
        self,
    ) -> None:
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        # Fresh comment above the watermark drives the resume.
        issue.comments.append(
            FakeComment(
                id=2000, body="try the three-way merge",
                user=FakeUser("alice"),
            )
        )
        # Seed the hash AFTER the comment so drift stays quiet and the
        # awaiting-human branch (not the drift path) owns the resume.
        self._seed_with_baseline_hash(
            gh, issue,
            awaiting_human=True, conflict_round=1, last_action_comment_id=1000,
        )
        before_writes = gh.write_state_calls

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,  # unused on the resume path
            head_shas=["beforehead", "merged"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", interrupted=True,
            ),
        )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()
        self.assertEqual(gh.write_state_calls, before_writes)
        data = gh.pinned_data(200)
        # Park not consumed, reply watermark not advanced -- the next process
        # re-resumes on the same comment.
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("last_action_comment_id"), 1000)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_awaiting_human_resume_recovers_from_stale_claude_session(self) -> None:
        # Regression: a `resolving_conflict` issue parked awaiting human
        # whose pinned `dev_session_id` references a Claude transcript that
        # no longer exists. The first `--resume <sid>` call comes back with
        # `No conversation found with session ID` on stderr and empty
        # stdout. Without immediate detection the resume would surface as
        # an `agent_silent` park, the silent-park counter would tick to 1
        # (still below the threshold), and the human would have to comment
        # twice more before recovery. With the fix, `_resume_dev_with_text`
        # transparently retries with a fresh spawn in the same worktree;
        # the rebase commit produced by the retry pushes and the issue
        # flips back to validating in a single tick.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
                "dev_session_id": "poisoned-sess",
            },
        )
        issue.comments.append(
            FakeComment(
                id=2000, body="please retry the conflict resolution",
                user=FakeUser("alice"),
            )
        )

        stale_stderr = "Error: No conversation found with session ID: poisoned-sess"

        calls: list = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="", stderr=stale_stderr,
                )
            return _agent(session_id="fresh-sess", last_message="resolved")

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=fake_run,
                push_branch=True,
                head_shas=["beforehead", "merged"],
            )

        # Two run_agent calls: the poisoned resume + the fresh-spawn retry.
        self.assertEqual(
            calls, ["poisoned-sess", None],
            "stale-session resume must be transparently retried as fresh",
        )
        # Successful retry pushes the branch and hands straight back to
        # `validating` WITHOUT parking agent_silent; the single docs
        # pass is deferred to the post-approval hop.
        mocks["_push_branch"].assert_called_once()
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertFalse(
            data.get("awaiting_human"),
            "awaiting_human must be cleared on a recovered resume",
        )
        self.assertNotEqual(data.get("park_reason"), "agent_silent")
        self.assertEqual(data.get("conflict_round"), 2)
        self.assertEqual(data.get("dev_session_id"), "fresh-sess")

    def test_awaiting_human_resume_with_question_parks_again(self) -> None:
        # Resumed agent that produces no new commit (asks another
        # question) must re-park rather than push or flip the label.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        issue.comments.append(
            FakeComment(
                id=2000, body="try harder",
                user=FakeUser("alice"),
            )
        )

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            # Same SHA before and after -- agent did nothing.
            head_shas=["samehead", "samehead"],
            push_branch=True,
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="I still need clarification on bar.py",
            ),
        )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        # Re-parked: counter unchanged, no label flip.
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertTrue(data.get("awaiting_human"))

    def test_no_pr_number_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(201, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(201)

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(201).get("awaiting_human"))

    def test_unpushed_local_commits_pushed_on_recovery(self) -> None:
        # Crash recovery: a previous tick committed a conflict resolution
        # but crashed before `_push_branch` returned (or before the post-
        # push state write landed). The next tick must push the local
        # commit and complete the round, NOT treat it as "no work needed"
        # and flip to validating with the resolution unpushed.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        # After the recovered push the handler probes whether the
        # worktree is still behind base via `git rev-list --count
        # HEAD..origin/<base>`. The crash-recovery scenario this test
        # exercises has HEAD already on base, so the probe returns 0
        # and the handler takes the fast path to validating without a
        # follow-up rebase.
        git_on_base = MagicMock(
            return_value=MagicMock(returncode=0, stdout="0\n", stderr=""),
        )

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock), \
             patch.object(workflow, "_git", git_on_base):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                # HEAD ahead of `origin/<branch>` by one commit (the
                # unpushed resolution); not behind.
                branch_ahead_behind=(1, 0),
            )
        # Recovered work pushed; rebase NOT attempted (we already have a
        # resolution waiting to ship).
        mocks["_push_branch"].assert_called_once()
        merge_mock.assert_not_called()
        # No agent spawn -- the recovery is a pure push, the dev already
        # produced the commit on the previous tick.
        mocks["run_agent"].assert_not_called()
        # Round completed: counter incremented, label flipped, marker
        # stamped exactly as on the happy-path resolve. The recovered
        # push hands straight back to `validating`; the single docs
        # pass is deferred to the post-approval hop.
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)

    def test_stale_worktree_parks_awaiting_human(self) -> None:
        # Worktree behind `origin/<branch>` (someone pushed to the PR
        # branch out-of-band). Force-pushing the local state would
        # clobber the real PR head; refuse and park.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(0, 2),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("stale or diverged", last_comment)

    def test_diverged_worktree_parks_awaiting_human(self) -> None:
        # Both ahead and behind: histories diverged. Cannot safely push
        # without rewriting remote history that may have value.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 1),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_unpushed_recovery_push_failure_parks(self) -> None:
        # Recovery push fails (e.g. force-with-lease lease miss because
        # the remote actually moved). Park rather than silently flipping
        # to validating with an unsynced local SHA.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=False,
                branch_ahead_behind=(1, 0),
            )
        mocks["_push_branch"].assert_called_once()
        merge_mock.assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_dirty_recovered_commits_parks_without_push(self) -> None:
        # Crash recovery with leftover dirty files: a previous tick
        # committed a resolution but ALSO left uncommitted edits, then
        # crashed before the dirty check ran. Pushing now would publish
        # a SHA that silently omits the leftover edits, and the reviewer
        # at validating would later run on a tree that does not match
        # the PR. Park instead.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 0),
                dirty_files=["leftover.py"],
            )
        # No push, no merge attempt, no label flip.
        mocks["_push_branch"].assert_not_called()
        merge_mock.assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted", last_comment)

    def test_dirty_clean_rebase_with_new_commit_parks_without_push(self) -> None:
        # Clean rebase produced a new HEAD but the
        # worktree carries pre-existing dirty files. Pushing the merge
        # rebased branch without those edits would publish an incomplete branch.
        gh, issue, pr = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))

    def test_dirty_clean_rebase_no_op_parks_without_flip(self) -> None:
        # Clean no-op rebase (HEAD didn't change because base hadn't
        # moved) but the worktree carries dirty files. The reviewer
        # at validating reads the worktree directly, so flipping with a
        # dirty tree would let the agent vote on something that does NOT
        # match the PR head. Park instead.
        gh, issue, pr = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))

    def test_recovered_push_with_stale_base_falls_through_to_rebase(self) -> None:
        # The `fixing` drift router
        # (`_reconcile_parked_fixing`) reroutes here
        # when a stuck `push_failed` / `agent_timeout` park has
        # UNPUSHED FIX COMMITS on a base that has since advanced. The
        # recovered-push fast path would publish the fix to the PR
        # branch and flip straight to `validating` -- but the branch
        # is still behind base. Probe behind-base after the push and
        # fall through to the rebase path so the same tick integrates
        # base and consumes exactly ONE `conflict_round` for the
        # combined push+rebase reconciliation. Without this, the PR
        # would be republished still-behind-base and the round counter
        # would burn a slot toward `MAX_CONFLICT_ROUNDS` without ever
        # attempting the base rebase the reroute was meant to perform.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        # Clean rebase that actually moved HEAD (recovered push +
        # rebase pushes a different SHA than the recovered SHA).
        merge_mock = MagicMock(return_value=(True, []))
        # Probe says still 2 commits behind base after the recovered
        # push, forcing the fall-through.
        git_behind_base = MagicMock(
            return_value=MagicMock(returncode=0, stdout="2\n", stderr=""),
        )

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock), \
             patch.object(workflow, "_git", git_behind_base):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                # Recovered push first (force-with-lease=None on a
                # straight-ahead push), then the rebased-head push
                # (force-with-lease=before_sha). The handler also reads
                # HEAD for the round-emit on success, so feed enough
                # SHAs through `_head_sha` for both the rebase-path's
                # before/after compare and the audit emit.
                branch_ahead_behind=(1, 0),
                head_shas=["before", "after", "after"],
            )

        # Both the recovered push AND the rebased-head push fired this
        # tick; the merge attempt ran in between.
        self.assertEqual(mocks["_push_branch"].call_count, 2)
        merge_mock.assert_called_once()
        # No agent spawn -- the rebase was clean.
        mocks["run_agent"].assert_not_called()
        # Single conflict_round increment for the combined push+rebase
        # reconciliation, NOT one per push.
        data = gh.pinned_data(200)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertEqual(data.get("review_round"), 0)
        self.assertIn("last_conflict_resolved_at", data)
        # The combined round outcome is the rebase path's
        # `base_rebased_clean`, not the fast-path `recovered_push`.
        rounds = [
            e for e in gh.recorded_events
            if e.get("event") == "conflict_round"
            and e.get("action") == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].get("outcome"), "base_rebased_clean")
        # Hand back to validating after the rebase landed.
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)


class HandleResolvingConflictHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `resolving_conflict` is dispatched per tick too,
    so a body edit while the dev is resolving conflicts must surface to
    the dev. Mirrors the in_review pattern: post a PR notice and resume."""

    def test_drift_posts_pr_notice_and_resumes_dev(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            500, label="resolving_conflict", body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=5000, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-500")
        gh.add_pr(pr)
        gh.seed_state(
            500,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            conflict_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-500",
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="resolved with edit"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            # Three SHAs: drift before/after for the post-resume head
            # delta, plus the third for the `conflict_round` audit emit
            # that records the pushed worktree HEAD.
            head_shas=["before", "after", "after"],
        )

        # Pushed drift fix -> hand straight back to `validating`; the
        # single docs pass is deferred to the post-approval hop.
        self.assertIn((500, "validating"), gh.label_history)
        self.assertNotIn((500, "documenting"), gh.label_history)
        # Notice posted on the PR.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_drift_resume_interrupted_leaves_state_untouched(self) -> None:
        # The drift resume routes through the shared
        # `_post_user_content_change_result`, which has no interrupted check
        # of its own. The conflicts caller must short-circuit BEFORE it so a
        # shutdown-sweep-killed run cannot ACK / park off partial output and
        # then persist the consumed-comment / refreshed-hash changes.
        gh = FakeGitHubClient()
        issue = make_issue(
            501, label="resolving_conflict", body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=5001, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-501")
        gh.add_pr(pr)
        gh.seed_state(
            501,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            conflict_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-501",
            user_content_hash="stale-hash",
        )
        before_writes = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="", interrupted=True,
            ),
            has_new_commits=True,
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        # The drift resume spawned, then was seen interrupted.
        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        # No durable state churn: the refreshed `user_content_hash`,
        # consumed-comment, and session mutations are all discarded.
        self.assertEqual(gh.write_state_calls, before_writes)
        data = gh.pinned_data(501)
        self.assertEqual(data.get("user_content_hash"), "stale-hash")
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("conflict_round"), 0)
        # No flip back to validating and no HITL question / ack on the issue.
        self.assertNotIn((501, "validating"), gh.label_history)
        self.assertFalse(any(
            "agent needs your input" in body
            or "existing work" in body
            or "timed out" in body
            for _, body in gh.posted_comments
        ))


class ResolvingConflictPublishesAlreadyRebasedTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A worktree the dev already rebased onto base in an earlier run but
    never pushed reaches `_handle_resolving_conflict` diverged from the
    stale PR head (ahead AND behind it). Instead of the conservative
    `diverged_branch` park, the handler force-publishes -- but ONLY when
    the rebase is genuinely on base AND the stale PR head is one the
    orchestrator produced. Either guard failing keeps the park.
    """

    BRANCH = "orchestrator/issue-310"
    PR_NUMBER = 910
    PR_HEAD = "stalehead00"

    def _seed(self):
        gh = FakeGitHubClient()
        issue = make_issue(310, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha=self.PR_HEAD), state="open",
        )
        gh.add_pr(pr)
        gh.seed_state(
            310, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=2, conflict_round=0,
            # `_handle_documenting`'s success exits are the one place
            # production code records the orchestrator's pushed head, so
            # the force-publish guard recognises this state.
            docs_checked_sha=self.PR_HEAD,
        )
        return gh, issue, pr

    def _run_diverged(self, gh, issue, *, on_base, recognized):
        # The worktree is 4 ahead / 2 behind the remote PR head (a rebase
        # rewrote history). Patch the two safety probes directly so the
        # handler's publish-vs-park branch is exercised in isolation.
        # After a successful force-publish the handler probes
        # `rev-list HEAD..origin/<base>` to decide between the fast
        # path and a follow-up rebase; this scenario is "already on
        # base", so the probe returns 0 and the fast path fires.
        git_on_base = MagicMock(
            return_value=MagicMock(returncode=0, stdout="0\n", stderr=""),
        )
        with patch.object(
            conflicts, "_already_rebased_onto_base",
            MagicMock(return_value=on_base),
        ), patch.object(
            conflicts, "_pr_head_orchestrator_produced",
            MagicMock(return_value=recognized),
        ), patch.object(workflow, "_git", git_on_base):
            return self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(session_id="dev-sess"),
                branch_ahead_behind=(4, 2),
                push_branch=True,
                head_shas=("local", "local"),
            )

    def test_publishes_when_on_base_and_recognized(self) -> None:
        gh, issue, _ = self._seed()
        mocks = self._run_diverged(gh, issue, on_base=True, recognized=True)
        # Force-published over the stale PR head -> validating, no park.
        self.assertIn((310, "validating"), gh.label_history)
        data = gh.pinned_data(310)
        self.assertFalse(data.get("awaiting_human"))
        self.assertNotEqual(data.get("park_reason"), "diverged_branch")
        self.assertEqual(data.get("review_round"), 0)
        rounds = [
            e for e in gh.recorded_events
            if e.get("event") == "conflict_round"
            and e.get("action") == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].get("outcome"), "recovered_push")
        # The push must be leased to the EXACT PR head we validated as
        # orchestrator-produced. A bare `_push_branch(spec, wt, branch)`
        # would do a fresh `ls-remote` and lease against whatever SHA
        # is live at push time, silently clobbering any foreign push
        # that landed between `gh.get_pr()` and this push.
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC, _FAKE_WT, self.BRANCH,
            force_with_lease=self.PR_HEAD,
        )

    def _assert_diverged_park(self, gh) -> None:
        # `_park_awaiting_human` records the reason on the audit event;
        # the durable `park_reason` field stays None by its contract.
        self.assertNotIn((310, "validating"), gh.label_history)
        self.assertTrue(gh.pinned_data(310).get("awaiting_human"))
        parks = [
            e for e in gh.recorded_events
            if e.get("event") == "park_awaiting_human"
            and e.get("reason") == "diverged_branch"
        ]
        self.assertEqual(len(parks), 1)

    def test_parks_when_not_on_base(self) -> None:
        gh, issue, _ = self._seed()
        self._run_diverged(gh, issue, on_base=False, recognized=True)
        self._assert_diverged_park(gh)

    def test_parks_when_pr_head_unrecognized(self) -> None:
        gh, issue, _ = self._seed()
        self._run_diverged(gh, issue, on_base=True, recognized=False)
        self._assert_diverged_park(gh)


class ResolvingConflictPublishGuardUnitTest(unittest.TestCase):
    """Unit tests for the two safety probes behind the already-rebased
    force-publish decision."""

    def _pr(self, sha):
        return FakePR(number=1, head_branch="b", head=FakePRRef(sha=sha))

    def test_pr_head_orchestrator_produced_recognizes_docs_checked_sha(
        self,
    ) -> None:
        # `docs_checked_sha` is the only key production code persists for
        # an orchestrator-produced PR head (set by `_handle_documenting`'s
        # success exits). PR heads from earlier in the lifecycle (the
        # initial implementing push, an intermediate fixing push) are not
        # currently recorded, so the guard refuses those by design rather
        # than guessing.
        gh = FakeGitHubClient()
        issue = make_issue(1, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(1, docs_checked_sha="abc")
        st = gh.read_pinned_state(issue)
        self.assertTrue(
            conflicts._pr_head_orchestrator_produced(st, self._pr("abc")),
        )
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(st, self._pr("xyz")),
        )
        # An empty/missing head never matches.
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(st, self._pr("")),
        )
        # No `docs_checked_sha` recorded -- e.g. a pre-docs validating
        # PR head -- must NOT match an empty-string lookup either.
        gh2 = FakeGitHubClient()
        issue2 = make_issue(2, label="resolving_conflict")
        gh2.add_issue(issue2)
        gh2.seed_state(2, dev_agent="claude")
        st2 = gh2.read_pinned_state(issue2)
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(st2, self._pr("abc")),
        )

    def test_already_rebased_onto_base_reads_rev_list_count(self) -> None:
        fetch_ok = MagicMock(return_value=MagicMock(returncode=0))
        with patch.object(workflow, "_authed_fetch", fetch_ok), \
             patch.object(
                 workflow, "_git_hardened",
                 MagicMock(return_value=MagicMock(returncode=0, stdout="0\n")),
             ):
            self.assertTrue(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )
        with patch.object(workflow, "_authed_fetch", fetch_ok), \
             patch.object(
                 workflow, "_git_hardened",
                 MagicMock(return_value=MagicMock(returncode=0, stdout="3\n")),
             ):
            self.assertFalse(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )

    def test_already_rebased_onto_base_fails_closed_on_fetch_failure(
        self,
    ) -> None:
        # Without proving HEAD is on the CURRENT base tip, we cannot
        # let the force-publish path enable. A stale
        # `<remote>/<base>` ref would let `rev-list HEAD..<remote>/<base>`
        # report "no missing commits" purely because the local mirror is
        # behind the real base -- mis-classifying a behind-base worktree
        # as already-rebased and force-publishing it.
        fetch_fail = MagicMock(
            return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
        )
        rev_list_zero = MagicMock(
            return_value=MagicMock(returncode=0, stdout="0\n"),
        )
        with patch.object(workflow, "_authed_fetch", fetch_fail), \
             patch.object(workflow, "_git_hardened", rev_list_zero):
            self.assertFalse(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )
        # And the rev-list probe must be skipped entirely on fetch failure
        # -- there is no value reading a count off a stale ref.
        rev_list_zero.assert_not_called()


if __name__ == "__main__":
    unittest.main()
