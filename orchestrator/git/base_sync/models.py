# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Frozen inputs and decisions threaded through one auto-rebase attempt.

The contexts live together because they are one another's continuations: the
legacy request derives a context, that context is what every publish and park
helper receives, and the recovery context is the same attempt re-read from
pinned state after a crash. Keeping them in one owner is what stops the fields
a resumed attempt carries from drifting away from the fields the interrupted
one recorded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from github.Issue import Issue

from orchestrator import config
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.state import WorkflowLabel


@dataclass(frozen=True)
class _PendingRewrite:
    """What one interrupted attempt recorded about the replay it made.

    Three facts, read as one, and written in the two moments they can each
    first be known. The pull request and the stage are the TERMS of the
    attempt, and they go down beside the anchor before git is allowed to touch
    the branch: they are what the permit's publication checks are asked
    AGAINST, and taken from the issue as it reads now they would compare today
    with today, so a relabel or a repoint made while the process was down
    would pass as the dead tick's own terms. The head is what the rebase
    produced, so it cannot exist until git hands it back -- and it is what
    says the checkout in front of a recovery is that attempt's own work.

    That split is what makes the window between them recoverable. A comment
    carrying the terms and no head is an attempt that was IN FLIGHT: the
    branch may be standing on the replay it made and nothing on the record can
    name that commit, so the head has to be vouched for by something else --
    the transfer evidence, whose permit proves what the checkout contributes
    against the pair a human ruled on. Which is why the terms are still worth
    having there: the permit is asked over the dead tick's own publication
    rather than over whatever the issue says on the tick that finds it.

    Absent, DECLARED, and DAMAGED are three answers rather than one. Absent is
    a comment carrying no member at all -- an attempt from before this record
    existed, or one whose anchor is all that was ever pinned -- and the
    recovery falls back to the readings it can still take for itself. Declared
    is the in-flight window above. Damaged is a comment that claims the record
    and cannot show it -- a member taken out, a head that is not a whole git
    object id, a pull request that is not an identity, a stage no publication
    is entered from -- and reading that as either of the other two would let
    exactly the state nobody can vouch for take a road reserved for one that
    can.
    """

    sha: str = ""
    pr_number: int = 0
    stage: WorkflowLabel | None = None
    # Whether the pinned comment claims this group and cannot show it. Set by
    # the reader rather than derived here, because only the reader can see the
    # difference between a member that is absent and one carrying something it
    # could not make sense of -- and the write that ends an attempt blanks
    # these fields rather than removing them, so a group of nulls is the
    # record nobody wrote and a member beside a null is the one something took
    # apart.
    damaged: bool = False

    @property
    def is_declared(self) -> bool:
        """Whether the terms this attempt was made under read back whole."""
        if self.damaged or self.stage is None:
            return False
        return self.pr_number > 0

    @property
    def is_recorded(self) -> bool:
        """Whether all three facts came back in the shape they claim."""
        return self.is_declared and bool(self.sha)

    @property
    def left_a_replay(self) -> bool:
        """Whether this record says the attempt got past `git rebase`.

        The terms alone do not: they go down before git runs, so an attempt
        that pinned them and stopped there left the branch exactly where the
        pull request has it. What says otherwise is a head -- or a claim to
        one this reader could not make sense of, which is a record something
        edited after a rebase rather than one nobody ever wrote.
        """
        return bool(self.sha) or self.damaged

    def names(self, local_head: str) -> bool:
        """Whether this record vouches for the commit a checkout stands on."""
        return self.is_recorded and bool(local_head) and self.sha == local_head

    def answers_for(
        self, pr_number: int, stage: WorkflowLabel | None,
    ) -> bool:
        """Whether the attempt was made for the publication being finished.

        What a recovery has to ask before it finishes anything, because
        finishing is not silent: the notice goes to the pull request this tick
        holds, the audit event is filed under the stage this tick reads, and
        the anchor that would bring the tick back is dropped. A repoint or a
        relabel made while the process was down would have all three
        attributed to a publication the interrupted attempt was never made
        for -- and the record is the only thing that can say so.

        Answered on the terms alone, with no exception for the stage the
        route's own finish moves to. That relabel IS a state a recovery has to
        recognize -- it is the last thing a finish does before the write that
        clears this record -- but the label on its own cannot say whether it
        was this route's step or somebody else's, and an issue moved to
        `validating` after a crash the push never survived looks identical.
        What tells them apart is the effect the finish had already had, which
        the caller reads off the remote and the receipt rather than off a
        label.
        """
        return (
            self.is_recorded
            and self.pr_number == pr_number
            and self.stage == stage
        )


@dataclass(frozen=True)
class _AutoRebaseContext:
    """Stable inputs for one refresh-time PR rebase attempt."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    behind: int
    label: WorkflowLabel | None
    pending_pre_rebase_sha: str | None


@dataclass(frozen=True)
class _AutoRebaseRequest:
    """Legacy refresh arguments before derived synchronization fields."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    behind: int

    def to_context(self, pending_field: str) -> _AutoRebaseContext:
        """Derive label and recovery state at the compatibility boundary."""
        return _AutoRebaseContext(
            gh=self.gh,
            spec=self.spec,
            issue=self.issue,
            state=self.state,
            worktree=self.worktree,
            pr_number=self.pr_number,
            behind=self.behind,
            label=self.gh.workflow_label(self.issue),
            pending_pre_rebase_sha=self.state.get(pending_field),
        )


@dataclass(frozen=True)
class _AutoRebaseRecoveryContext:
    """Stable inputs for finalizing one interrupted auto-rebase."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    label: str
    pending_pre_rebase_sha: str
    # What the interrupted attempt recorded about its own replay, where it got
    # as far as recording anything: the head it produced, and the publication
    # it produced it for. The anchor beside it names the lease and can prove
    # neither, so this is what says the checkout in front of this recovery is
    # that attempt's work and what its permit's terms are re-asked against.
    pending_rewrite: _PendingRewrite = _PendingRewrite()
    behind: int = 0
    unparking_consumed_max: int | None = None


@dataclass(frozen=True)
class _AutoRebaseRecoverySnapshot:
    """Local and remote branch state observed during crash recovery."""

    branch: str
    local_head: str
    remote_head: str = ""
    ahead: int = 0
    behind: int = 0

    @property
    def head(self) -> str:
        """The commit this checkout stands on, as a name anything may hold.

        The same reading as `local_head` with the one absence spelled out: a
        checkout that could not name its own head answers the empty string,
        which every comparison behind this reads as "not that commit" rather
        than as None.
        """
        return self.local_head or ""


@dataclass(frozen=True)
class _AutoRebaseDecision:
    """Whether the coordinator should continue its normal rebase flow."""

    should_continue: bool
    consumed_comment_id: int | None = None


@dataclass(frozen=True)
class _ConflictRouteContext:
    """Stable inputs for routing an auto-rebase conflict to its handler."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    pr_number: int
    label: str
    behind: int
    conflicted_files: list[str]
    pr_head_sha: str | None
