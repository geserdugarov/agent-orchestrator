# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One oversized candidate walked from the size gate to a settled `single`.

The adjudicated fixture beside this one WRITES the verdict a rebase then
carries; this one earns it. It puts a change past the ceiling on the branch,
sends it through the real size gate, and settles the real adjudicator's
answer -- so what the base advance after it replays is a verdict this workflow
actually reached, over a pair it actually froze, on an issue the route
actually walked.

Three things are stood in for and none of them decides anything: the agent's
reply, the authenticated push, and the remote-side base freeze these fixtures
have no token to take. The push double moves the pull request as well, because
the repository is real and the pull request is a double -- left disagreeing,
every round past the first would be entered on a head the remote no longer
has.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

from orchestrator import config
from orchestrator.agents import runner as _agent_runner
from orchestrator.git import branch_transport as _branch_transport
from orchestrator.git.base_sync import (
    persistence as _persistence,
    pre_pr as _pre_pr,
    publication as _base_publication,
    startup as _base_startup,
)
from orchestrator.git.measurement import additions as _additions, commits as _measurement_commits
from orchestrator.workflow.stages.decomposition import (
    late_coordinator as _coordinator,
    late_reply as _late_reply,
)
from orchestrator.workflow.stages.implementing import (
    late_push as _late_push,
    late_records as _late_records,
    late_rotation as _rotation,
)
from orchestrator.workflow.stages.validating import handler as _validating
from orchestrator.workflow.state import WorkflowLabel
from tests.git.base_sync.exemption_git_support import (
    ISSUE,
    AdjudicatedRebaseRealGitFixture,
)
from tests.git.base_sync.real_git_test_support import (
    ADD_COMMAND,
    BASE_BRANCH,
    ORIGIN_REMOTE,
    PR_BRANCH,
    PR_NUMBER,
    PUSH_COMMAND,
    WORKTREES_DIR_NAME,
    _LocalBranchPusher,
)
from tests.git.base_sync.refresh_test_support import _patched
from tests.support.fakes import FakePRRef
from tests.workflow.fixtures import (
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    _agent,
)

# The ceiling the journey's candidate is oversized against, and the file that
# puts it there. Both are small enough to keep the real diff cheap and large
# enough that the real counter really crosses the ceiling on the real objects.
# The seams a push and the recovery's own branch fetch go out through, named
# once because several legs of this journey hold them and the alias is what a
# mock lands on.
PUSH_BRANCH = "_push_branch"
AUTHED_FETCH = "_authed_fetch"

# What a process that never came back looks like from inside the tick.
DIED = "the process died before the tick returned"


def _stops_the_tick() -> MagicMock:
    """A seam that ends the tick the moment it is reached."""
    return MagicMock(side_effect=RuntimeError(DIED))


def _local_branch_fetch(_spec, refspec: str, *, cwd):
    """Stand in for the authenticated fetch of the pull request's branch."""
    return subprocess.run(
        ["git", "fetch", "--quiet", ORIGIN_REMOTE, refspec],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )

JOURNEY_CEILING = 20
JOURNEY_FILE = "oversized.py"
JOURNEY_LINES = 200

# The file a second base advance lands on. `_advance_base` writes one fixed
# name with fixed content, so a journey that needs the base to move TWICE
# needs a commit of its own for the second move.
SECOND_ADVANCE_FILE = "later.txt"

# The verdict the adjudicator reaches on that candidate, in the fence the
# reply owner really parses -- taken from that owner rather than retyped, so
# a manifest this build could not read would fail here rather than pass.
SINGLE_MANIFEST = (
    f"```{_late_reply._LATE_BLOCK}\n"
    + '{"decision": "single", "rationale": "one coherent change",'
    + ' "category": "generated_artifacts"}\n```'
)

# The line counter as it is before the shared base-sync doubles replace it. A
# journey about an OVERSIZED candidate has to cross the ceiling on the objects
# themselves, so the real reading is put back for the fixture below.
_REAL_ADDITION_COUNT = _additions._count_added_lines


class _PublishesToThePullRequest(_LocalBranchPusher):
    """A push that moves the pull request this fixture's client answers with.

    The repository is real and the pull request is a double, so a push that
    landed would otherwise leave the two disagreeing about where the branch
    is -- and every gate past it is entered on a head the remote no longer
    has. What a real remote does to `pr.head` is done here, so a journey of
    several rounds reads one branch rather than two.
    """

    def __init__(self, github) -> None:
        super().__init__()
        self._github = github

    def __call__(self, spec, worktree, branch, **options) -> bool:
        """Push, and stand the pull request on whatever the push published."""
        landed = super().__call__(spec, worktree, branch, **options)
        pull_request = self._github.pulls[PR_NUMBER]
        if landed and self.revision:
            pull_request.head = FakePRRef(sha=self.revision)
        return landed


class _DiesAfterTheRelabel:
    """A client whose relabel lands and whose process does not come back.

    The last window a finish has: the notice, the audit event, and the route
    have all gone out, and the write that clears the record of the attempt has
    not. Staged where it really is rather than by seeding a comment nothing
    wrote.
    """

    def __init__(self, relabel) -> None:
        self._relabel = relabel

    def __call__(self, issue, label) -> None:
        """Apply the label the finish chose, then stop the tick."""
        self._relabel(issue, label)
        raise RuntimeError(DIED)


class _LandsThenDies:
    """A push that reaches the remote and whose answer never comes back.

    The window between the request landing and the write that receipts it,
    which no seam above the transport can stage: the branch really moves, and
    the tick that moved it never learns that it did.
    """

    def __init__(self, github) -> None:
        self._pusher = _PublishesToThePullRequest(github)
        self._held = patch.object(_branch_transport, PUSH_BRANCH, self)

    def __call__(self, *called, **options) -> bool:
        """Publish the branch, then stop the tick that asked for it."""
        self._pusher(*called, **options)
        raise RuntimeError(DIED)

    def __enter__(self):
        """Hold the push seam for the tick that is about to be lost."""
        return self._held.__enter__()

    def __exit__(self, *details) -> None:
        """Release it once that tick has ended."""
        self._held.__exit__(*details)


class OversizedJourneyRealGitFixture(AdjudicatedRebaseRealGitFixture):
    """One oversized candidate, adjudicated for real and then rebased for real.

    Every step here is the production one over a real repository: the size
    gate counts the real diff and routes the real generation, the adjudicator
    settles a real `single` and records the exemption and the identity from
    the pair it froze, and the refresh rebases and force-publishes through the
    same gate. What is stood in for is what a fixture cannot have -- the
    agent's reply, the authenticated push, and the remote-side base freeze.
    """

    def setUp(self) -> None:
        super().setUp()
        # The crash recovery fetches the pull request's own branch before it
        # will compare anything, and that hop is the one this fixture has no
        # token for. Answered against the bare repository behind the clone,
        # which IS the remote.
        _patched(self, _branch_transport, AUTHED_FETCH, _local_branch_fetch)
        _patched(self, _additions, "_count_added_lines", _REAL_ADDITION_COUNT)
        _patched(self, config, "MAX_ADDED_LINES", JOURNEY_CEILING)
        # The adjudicator fingerprints the accepted pair in the checkout the
        # configured root names, and this journey's is the real one.
        _patched(
            self, config, "WORKTREES_DIR", self._tmpdir / WORKTREES_DIR_NAME,
        )

    def _commits_an_oversized_candidate(self) -> str:
        """Put a change past the ceiling on the branch and open its review."""
        (self._wt / JOURNEY_FILE).write_text(
            "".join(f"value_{line} = {line}\n" for line in range(JOURNEY_LINES)),
        )
        self._git(ADD_COMMAND, ".", cwd=self._wt)
        self._git(
            "commit", "-m", "feat: add the oversized change",
            cwd=self._wt, env_extra=self._author_env,
        )
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, PR_BRANCH, cwd=self._wt)
        self._open_pull_request(label=LABEL_VALIDATING)
        return self._wt_head()

    def _publishes_the_candidate(self, candidate: str):
        """Take the publication a `workflow:validating` round makes.

        The gate call itself rather than the reviewer around it, because what
        opens this journey is a PUSH: the stage's own publication seams -- the
        dev-fix bounce and the validating recovery -- each reach the gate with
        exactly these terms, and it is the gate that holds the candidate and
        hands the issue to the adjudication. The reviewer is a different tick
        and is driven as itself below.
        """
        issue = self._gh._issues[ISSUE]
        with patch.object(
            _branch_transport, PUSH_BRANCH,
            _PublishesToThePullRequest(self._gh),
        ):
            return _late_push._publishes(
                _late_records._gate(
                    self._gh, self._spec, issue,
                    self._gh.read_pinned_state(issue), self._wt,
                ),
                PR_BRANCH,
                _late_records._Entered(
                    stage=WorkflowLabel.VALIDATING,
                    head=candidate,
                    candidate=candidate,
                ),
            )

    def _accepted_as_single(self):
        """Run the real adjudicator over the frozen pair and settle its verdict.

        The settlement publishes the accepted commit itself -- it is the last
        tick holding the head the verdict was measured over -- so the push it
        makes is a real one against the fixture's own remote.
        """
        issue = self._gh._issues[ISSUE]
        spawn = MagicMock(return_value=_agent(last_message=SINGLE_MANIFEST))
        with patch.object(_agent_runner, "run_agent", spawn), patch.object(
            _branch_transport, PUSH_BRANCH,
            _PublishesToThePullRequest(self._gh),
        ):
            return _coordinator._adjudicate_late_generation(
                self._gh, self._spec, issue,
                self._gh.read_pinned_state(issue),
            )

    def _advances_the_base_again(self) -> None:
        """Move the real base branch a second time, on its own commit."""
        self._git("checkout", BASE_BRANCH, cwd=self._work)
        (self._work / SECOND_ADVANCE_FILE).write_text("later base side\n")
        self._git(ADD_COMMAND, ".", cwd=self._work)
        self._git(
            "commit", "-m", "chore: advance the base again",
            cwd=self._work, env_extra=self._author_env,
        )
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, BASE_BRANCH, cwd=self._work)

    def _refreshes(self) -> _LocalBranchPusher:
        """Run one refresh over the advanced base and report the push it made."""
        pusher = _PublishesToThePullRequest(self._gh)
        with patch.object(_branch_transport, PUSH_BRANCH, pusher):
            self._refresh()
        return pusher

    def _crashes(self, seam) -> None:
        """Run one refresh whose tick a durable boundary never returns from.

        `seam` is a context manager standing in for the process dying at one
        of the moments this journey is about, and every one of them is a real
        moment: the refresh really rebases, really enters the gate, and really
        pushes up to whichever of them the case is about.

        Nothing is asserted about the raise. The refresh already treats one
        worktree's failure as that worktree's -- it logs and moves on -- so
        what the tick leaves behind is the same durable state a process death
        would, which is the whole of what the next one has to work from.

        The ordinary push double goes on first and the seam second, so a case
        about the transport replaces it and every other case still reaches a
        real remote.
        """
        pushing = patch.object(
            _branch_transport, PUSH_BRANCH,
            _PublishesToThePullRequest(self._gh),
        )
        with pushing, seam:
            self._refresh()

    def _dies_before_the_grant(self):
        """The base reading the evidence is assembled over never comes back."""
        return patch.object(
            _measurement_commits, "_freeze_base_commit",
            _stops_the_tick(),
        )

    def _dies_before_the_push(self):
        """The request that would put the replay on the remote never returns."""
        return patch.object(
            _branch_transport, PUSH_BRANCH,
            _stops_the_tick(),
        )

    def _dies_before_the_rebase(self):
        """The anchor and the terms go down and `git rebase` never runs.

        The first window an attempt has, and the one that looks exactly like
        the window after it from the comment alone: what is pinned is the
        anchor and the terms, and the checkout is still standing where the
        pull request has it.
        """
        return patch.object(
            _pre_pr, "_rebase_base_into_worktree",
            _stops_the_tick(),
        )

    def _dies_before_the_record(self):
        """`git rebase` replays the branch and nothing writes what it made.

        The narrowest window this journey has, and the only one no id can
        close. The branch carries a replay that diverges from the head the
        pull request still has, the comment carries the anchor and the terms
        the attempt was entered under, and no field anywhere names the commit
        in the checkout -- so what has to vouch for it is what it contributes.
        """
        return patch.object(
            _base_startup, "_record_the_rewrite",
            _stops_the_tick(),
        )

    def _dies_after_the_rewrite(self):
        """The rebase lands and is recorded; nothing after it runs.

        One statement past the window above, and the one the record after
        `git rebase` exists for: the divergence in the checkout is the same,
        and the id the attempt wrote as git handed it back is what says it is
        that attempt's own work.
        """
        return patch.object(
            _base_publication, "_publish_auto_rebase",
            _stops_the_tick(),
        )

    def _dies_before_the_report(self):
        """The receipt lands; the record it owes the sinks never goes out."""
        return patch.object(
            _rotation, "_reports_the_transfer",
            _stops_the_tick(),
        )

    def _dies_before_the_route(self):
        """The push and the receipt land; the notice never goes out."""
        return patch.object(
            _base_publication, "_post_auto_rebase_notice",
            _stops_the_tick(),
        )

    def _dies_before_the_checkpoint(self):
        """The notice and the event go out; nothing records that they did."""
        return patch.object(
            _persistence, "_announced",
            _stops_the_tick(),
        )

    def _dies_at_the_relabel(self):
        """The finish records that it announced itself and never routes."""
        return patch.object(
            self._gh, "set_workflow_label",
            _stops_the_tick(),
        )

    def _dies_after_the_relabel(self):
        """The reviewer is routed; the write that clears the record is not."""
        return patch.object(
            self._gh, "set_workflow_label",
            _DiesAfterTheRelabel(self._gh.set_workflow_label),
        )

    def _durable(self):
        """The pinned comment as a process starting now would read it."""
        return self._gh.read_pinned_state(self._gh._issues[ISSUE])

    def _reviews(self, verdict: str = REVIEW_APPROVED_MESSAGE):
        """Run one real `workflow:validating` tick and report its spawn.

        The handler itself, with only the reviewer agent stood in for: the
        terminals, the drift read, the round cap, the worktree reuse, the
        prompt, the verdict parse, and everything an approval earns are the
        production ones, over the checkout the base refresh just rewrote.
        """
        spawn = MagicMock(return_value=_agent(last_message=verdict))
        with patch.object(_agent_runner, "run_agent", spawn), patch.object(
            _branch_transport, PUSH_BRANCH,
            _PublishesToThePullRequest(self._gh),
        ):
            _validating._handle_validating(
                self._gh, self._spec, self._gh._issues[ISSUE],
            )
        return spawn

    def _issue_comments(self) -> list[str]:
        """Every comment this journey posted on the issue thread."""
        return [
            body for number, body in self._gh.posted_comments
            if number == ISSUE
        ]
