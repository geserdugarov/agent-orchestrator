# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config
from orchestrator.git.base_sync import refresh as _base_refresh
from tests.git.base_sync.sync_test_support import (
    _diverged,
    _git_result,
    _patch_base_sync,
)
from tests.support.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeIssue,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)

# --- Shared base-sync fixture literals -----------------------------------
# One worktree per issue drives every scenario here: issue #7 with an open
# PR #42 on the canonical head branch of the `acme/widget` target repo.
ISSUE = 7
PR_NUMBER = 42
SLUG = "acme/widget"
BASE_BRANCH = "main"
PR_BRANCH = "orchestrator/acme__widget/issue-7"

# Multi-remote spec exercised by the per-spec authed-fetch regression.
PRIVATE_SLUG = "acme/widget-private"
PRIVATE_BASE_BRANCH = "cache-main"
PRIVATE_REMOTE = "private"

# Worktree HEAD SHAs threaded through the rebase / push / recovery flows.
BEFORE_SHA = "be40e5ba" * 5
# The head the refreshed pull request is standing on. It IS the pre-rebase
# head this refresh reads and leases its force-push against, because the two
# are one fact: an auto rebase publishes over the branch it reconciled with.
CONFLICT_PR_HEAD_SHA = BEFORE_SHA

# A pull request somebody else pushed to while this refresh was in flight.
MOVED_PR_HEAD_SHA = "cafef00d" * 5

# The world the size gate reads before a refresh may push: a provably clean
# tree, a candidate this host holds, a base the remote named, and a diff well
# under any ceiling. A refresh test is about the rebase rather than the size
# question, so it gets the ordinary answers and a test about the gate itself
# seeds what it is about.
GATE_CANDIDATE_SHA = "ca11ab1e" * 5
GATE_BASE_SHA = "ba5e0000" * 5

# The head a rebase leaves the checkout on, and the head a crash recovery finds
# already there. Both ARE the commit the gate proves that checkout to, because
# in production the two are one read of one worktree: the refresh names the
# commit it means to publish and the gate refuses a checkout standing anywhere
# else. Spelled differently a fixture would model the race rather than the
# tick -- pushing one commit while the notice, the event, and the finalize name
# another.
AFTER_SHA = GATE_CANDIDATE_SHA
REBASED_SHA = GATE_CANDIDATE_SHA

# What the checkout stands on once something has moved it out from under the
# reading its caller took, which is the race the bound candidate refuses.
MOVED_CHECKOUT_SHA = "m0vedc0m" * 5

# Workflow labels the refresh routes between.
LABEL_IN_REVIEW = "in_review"
LABEL_VALIDATING = "workflow:validating"
LABEL_RESOLVING_CONFLICT = "workflow:resolving_conflict"
LABEL_DOCUMENTING = "workflow:documenting"
LABEL_IMPLEMENTING = "workflow:implementing"

# The pull-request state a refresh may act on.
STATE_OPEN = "open"

# Audit event names emitted by the base-sync flow.
EVENT_BASE_REBASED = "base_rebased"
EVENT_CONFLICT_ROUND = "conflict_round"

# Awaiting-human park reasons the auto-rebase flow writes.
PARK_PUSH_FAILED = "auto_base_rebase_push_failed"
PARK_DIRTY = "auto_base_rebase_dirty"
PARK_FAILED = "auto_base_rebase_failed"

# Pinned-state field keys read back from `gh.pinned_data(...)`.
KEY_AWAITING_HUMAN = "awaiting_human"
KEY_PARK_REASON = "park_reason"
KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
KEY_PENDING_REWRITE_SHA = "pending_auto_base_rebase_rewrite_sha"
KEY_PENDING_REWRITE_PR = "pending_auto_base_rebase_rewrite_pr"
KEY_PENDING_REWRITE_STAGE = "pending_auto_base_rebase_rewrite_stage"
KEY_REVIEW_ROUND = "review_round"
KEY_CONFLICT_ROUND = "conflict_round"
KEY_LAST_ACTION_COMMENT_ID = "last_action_comment_id"
KEY_PR_LAST_COMMENT_ID = "pr_last_comment_id"

# Git output, command, and event fields shared by the scenario assertions.
THREE_BEHIND_STDOUT = "3\n"
TWO_BEHIND_STDOUT = "2\n"
UP_TO_DATE_STDOUT = "0\n"
REBASE_COMMAND = "rebase"
ABORT_FLAG = "--abort"
RESET_COMMAND = "reset"
HARD_RESET_FLAG = "--hard"
FORCE_WITH_LEASE_KWARG = "force_with_lease"
EVENT_FIELD = "event"
SHA_FIELD = "sha"
METHOD_FIELD = "method"

# Stable identities and values used across park and recovery scenarios.
HUMAN_LOGIN = "human"
PARK_WATERMARK_COMMENT_ID = 99
RETRY_COMMENT_ID = 200
OUTSIDER_COMMENT_ID = 201
UNREAD_COMMENT_ID = 500
GIT_FAILURE_EXIT_CODE = 128
MISSING_ISSUE_NUMBER = 9999
NEW_REBASED_SHA = "9ea5eba0" * 5


def _pending_attempt(rewrite: str, *, stage: str = LABEL_IN_REVIEW) -> dict:
    """The whole record one interrupted auto-rebase attempt leaves behind.

    Both halves rather than the anchor alone, because that is what a real
    attempt writes: the head its push is leased against, pinned before git
    ran, and the replay it produced with the publication it produced it for,
    written once git had made one. A recovery holding only the first cannot
    show the checkout in front of it is that attempt's own work, which is a
    state of its own rather than the ordinary interrupted rebase.
    """
    return {
        KEY_PENDING_PUSH_SHA: BEFORE_SHA,
        KEY_PENDING_REWRITE_SHA: rewrite,
        KEY_PENDING_REWRITE_PR: PR_NUMBER,
        KEY_PENDING_REWRITE_STAGE: stage,
    }


def _patched(test_case, owner, name: str, replacement) -> None:
    """Install one replacement on its owner for the rest of the test."""
    patcher = patch.object(owner, name, replacement)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class _RemoteHeadGit:
    def __init__(
        self,
        remote_head: str = "",
        *,
        returncode: int = 0,
    ) -> None:
        self._remote_head = remote_head
        self._returncode = returncode

    def __call__(self, *args, **_kwargs):
        if (
            len(args) >= 2
            and args[0] == "rev-parse"
            and isinstance(args[1], str)
            and args[1].startswith("refs/remotes/")
        ):
            return _git_result(
                returncode=self._returncode,
                stdout=f"{self._remote_head}\n" if self._remote_head else "",
            )
        return _git_result()


class _AwaitingHumanRecorder:
    def __init__(self) -> None:
        self.observed: list[bool] = []

    def __call__(self, gh, _spec, issue) -> None:
        state = gh.pinned_data(issue.number)
        self.observed.append(bool(state.get(KEY_AWAITING_HUMAN)))


class _RebaseAnchorRecorder:
    def __init__(self, gh: FakeGitHubClient) -> None:
        self.observed: list[object] = []
        self._gh = gh

    def __call__(self, _spec, _worktree):
        self.observed.append(
            self._gh.pinned_data(ISSUE).get(KEY_PENDING_PUSH_SHA),
        )
        return True, []


class _SyncWorktreeWithBaseFixture:
    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug=SLUG,
            target_root=Path("/tmp/refresh-target"),
            base_branch=BASE_BRANCH,
        )
        self.wt = Path("/tmp/refresh-wt")
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(ISSUE, label=LABEL_IMPLEMENTING))
        from tests.git.base_sync.gate_reads_support import _gate_reads

        _gate_reads(self)

    def _seed_pr_issue(
        self,
        *,
        label: str = LABEL_IN_REVIEW,
        extra_labels=(),
        with_pr: bool = True,
        **state,
    ) -> FakeIssue:
        """Seed issue #7 at `label` with pinned PR #42 on the canonical
        branch. `extra_labels` are appended to the issue (backlog / paused
        markers); `state` fields merge into the pinned state. Returns the
        issue so callers can seed comments on its thread.

        The pull request itself is registered with it, because a number in
        pinned state with nothing behind it is a state the size gate refuses
        before any refresh may push. `with_pr=False` is for the tests whose
        premise IS that `gh.get_pr` cannot answer."""
        issue = make_issue(ISSUE, label=label)
        for name in extra_labels:
            issue.labels.append(FakeLabel(name))
        self.gh.add_issue(issue)
        self.gh.seed_state(
            ISSUE,
            pr_number=PR_NUMBER,
            branch=PR_BRANCH,
            **state,
        )
        if with_pr and PR_NUMBER not in self.gh.pulls:
            self._add_pr(head=FakePRRef(sha=CONFLICT_PR_HEAD_SHA))
        return issue

    def _add_pr(
        self,
        *,
        pr_number: int = PR_NUMBER,
        head_branch: str = PR_BRANCH,
        merged: bool = False,
        state: str = "open",
        head: FakePRRef | None = None,
    ) -> FakePR:
        # Standing on the head this refresh reads and leases against unless a
        # case says otherwise: the size gate compares the two and refuses a
        # pull request that moved out from under the reading.
        pr = FakePR(
            number=pr_number,
            head_branch=head_branch,
            merged=merged,
            state=state,
            head=head or FakePRRef(sha=CONFLICT_PR_HEAD_SHA),
        )
        self.gh.add_pr(pr)
        return pr

    def _add_comment(
        self,
        comment_id: int,
        body: str,
        user: str,
    ) -> None:
        issue = self.gh._issues[ISSUE]
        issue.comments.append(
            FakeComment(
                id=comment_id,
                body=body,
                user=FakeUser(user),
            )
        )


class _CrashRecoveryVerificationFixture(_SyncWorktreeWithBaseFixture):
    def _run_unverifiable_recovery(
        self,
        *,
        fetch_returncode: int = 0,
        rev_parse_returncode: int = 0,
        rev_parse_stdout: str = "remote-sha\n",
        ahead_behind: tuple = (1, 0),
        local_head: str = REBASED_SHA,
    ):
        """Helper for the four `_recover_pending_auto_base_rebase`
        cannot-verify regressions: seed a flag-pinned in_review issue,
        wire mocks per arguments, and run the refresh once.

        Returns a `(hardened_mock, push_mock, merge_mock)` triple so the
        caller can assert on the reset call and the no-push / no-rebase
        invariant.
        """
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha=BEFORE_SHA,
            review_round=3,
        )
        self._add_pr()
        mocks = {
            "dirty": MagicMock(return_value=[]),
            REBASE_COMMAND: MagicMock(),
            "head_sha": MagicMock(return_value=local_head),
            "ahead_behind": MagicMock(
                return_value=_diverged(*ahead_behind),
            ),
            "fetch": MagicMock(
                return_value=_git_result(returncode=fetch_returncode),
            ),
            "push": MagicMock(),
            "git": MagicMock(
                return_value=_git_result(stdout=UP_TO_DATE_STDOUT),
            ),
            "hardened": MagicMock(
                side_effect=_RemoteHeadGit(
                    rev_parse_stdout.rstrip("\n"),
                    returncode=rev_parse_returncode,
                )
            ),
        }
        with _patch_base_sync(**mocks):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        return mocks["hardened"], mocks["push"], mocks[REBASE_COMMAND]

    def _assert_recovery_unverified_reset_and_park(self, hardened_mock, push_mock, merge_mock) -> None:
        """Common assertions for the four cannot-verify recovery exits.

        Every such exit must (a) reset local HEAD to the pre-rebase
        anchor (so the worktree matches the last-known remote PR head
        and the same-tick handler dispatch cannot read a SHA the PR
        may not carry), (b) park awaiting human with
        `auto_base_rebase_push_failed` (so the dispatcher's
        `awaiting_human` short-circuit fires on every PR-stage
        handler this tick), and (c) clear the anchor (the reset put
        HEAD back at it, so a follow-up tick would hit case 1
        anyway).
        """
        # Reset to the pre-rebase SHA was issued.
        reset_calls = [
            recorded_call
            for recorded_call in hardened_mock.call_args_list
            if recorded_call.args[:3] == (RESET_COMMAND, HARD_RESET_FLAG, BEFORE_SHA)
        ]
        self.assertEqual(len(reset_calls), 1, hardened_mock.call_args_list)
        # No push, no merge, no relabel -- recovery aborted before
        # finalize.
        push_mock.assert_not_called()
        merge_mock.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        state = self.gh.pinned_data(ISSUE)
        # Anchor cleared (reset put HEAD back at it).
        self.assertIsNone(state.get(KEY_PENDING_PUSH_SHA))
        # Same-tick handler dispatch will short-circuit on this park.
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertEqual(
            state.get(KEY_PARK_REASON),
            PARK_PUSH_FAILED,
        )
        # No `base_rebased` event -- we did NOT route to validating.
        rebased = [event for event in self.gh.recorded_events if event.get(EVENT_FIELD) == EVENT_BASE_REBASED]
        self.assertEqual(rebased, [])
