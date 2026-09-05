# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One adjudicated branch a real base advance rebases, seeded on a real repo.

The refresh only rewrites a branch that already has a pull request, so the
world a transfer is decided in here is the one the PR-aware sync runs in: an
open pull request standing on the head this refresh reads, an exemption naming
the commit that head is, and the canonical digest of what that commit really
contributes over the base the adjudication was measured from.

Both base advances a case can ask for are real commits on the real remote. The
inherited one touches a path the branch never has, so the replay leaves the
prospective contribution byte-identical and the transfer is the equivalence it
claims. The SHARED one edits the other end of a file the branch also edits,
which rebases without a conflict and still changes the contribution: the
pre-image a reviewer would be handed is a different blob, which is exactly the
base advance the ordinary cumulative gate has to measure.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.git.base_sync import pre_pr as _pre_pr
from orchestrator.git.measurement import (
    commits as _measurement_commits,
    fingerprint as _fingerprint,
)
from orchestrator.git.measurement.models import FrozenCommit
from orchestrator.workflow.late_split import exemption as _exemption
from tests.git.base_sync.real_git_test_support import (
    ADD_COMMAND,
    BASE_BRANCH,
    ORIGIN_REMOTE,
    PR_BRANCH,
    PR_NUMBER,
    PUSH_COMMAND,
    _RefreshBaseRealGitFixture,
)
from tests.git.base_sync.refresh_test_support import _patched
from tests.support.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow.fixtures import LABEL_IN_REVIEW, STATE_OPEN

ISSUE = 7

# The file both the branch and a SHARED base advance write to, long enough
# that the two edits are far apart and the replay is clean.
SHARED_FILE = "shared.py"
SHARED_LINES = 20

# What the two ends of `SHARED_FILE` are rewritten to, so a case reads which
# side moved rather than which line number did.
BRANCH_EDIT = "branch tail\n"
BASE_EDIT = "base head\n"

# The path a forged base ref carries and the real remote does not, so a case
# reads the extra work by name rather than by counting commits.
FORGED_FILE = "forged.txt"

# The round the reviewer is on when the refresh takes the branch, so the reset
# the published tail makes is a reset of something.
REVIEW_ROUND = 3


class _RemoteBaseFreeze:
    """The base branch as the REMOTE really has it, for a fixture with no token.

    The production freeze asks the remote over an authenticated `ls-remote`
    and then makes sure this host holds what it named. These fixtures have no
    token to reach one with, so the bare repository behind the clone answers
    instead -- which is the same authority read locally: it IS the remote, and
    nothing a worktree writes can move its refs.
    """

    def __init__(self, remote: Path) -> None:
        self._remote = remote

    def __call__(self, _spec, _worktree) -> FrozenCommit:
        """Freeze whatever the remote's own base branch points at."""
        named = subprocess.run(
            [
                "git", "--git-dir", str(self._remote),
                "rev-parse", f"refs/heads/{BASE_BRANCH}",
            ],
            capture_output=True, text=True, check=True,
        )
        return FrozenCommit(sha=named.stdout.strip())


class _RepointsTheBaseRef:
    """A worktree moving the local base ref after this tick's fetch.

    The refresh fetches `<remote>/<base>` once and then rebases onto the ref
    that fetch updated, so the window between the two is the one another
    worktree sharing the object store can write in -- and what the branch is
    replayed onto is whatever the ref says at the moment `git rebase` runs.
    Wrapped around the rebase rather than seeded beforehand for exactly that
    reason: a ref forged before the tick is one the tick's own fetch puts back.
    """

    def __init__(self, fixture, forged: str) -> None:
        self._fixture = fixture
        self._forged = forged
        self._rebase = _pre_pr._rebase_base_into_worktree

    def __call__(self, spec, worktree):
        """Repoint the base ref, then run the rebase the refresh asked for."""
        self._fixture._git(
            "update-ref", f"refs/remotes/{ORIGIN_REMOTE}/{BASE_BRANCH}",
            self._forged, cwd=worktree,
        )
        return self._rebase(spec, worktree)


def _shared_body(*, first: str = "", last: str = "") -> str:
    """The shared file with either end replaced, and the middle untouched."""
    body = [f"line {number}\n" for number in range(SHARED_LINES)]
    if first:
        body[0] = first
    if last:
        body[-1] = last
    return "".join(body)


class AdjudicatedRebaseRealGitFixture(_RefreshBaseRealGitFixture):
    """A PR-having branch whose head is the commit an adjudication accepted."""

    def setUp(self) -> None:
        super().setUp()
        # The transfer reads the base from the remote rather than off a local
        # ref, so the fixture has to answer as the remote does -- the shared
        # gate double names a SHA no repository here holds.
        _patched(
            self, _measurement_commits, "_freeze_base_commit",
            _RemoteBaseFreeze(self._remote),
        )

    def _adjudicate(self) -> None:
        """Record the verdict a settled `single` left, and open its remote.

        The branch is published first, because the head the pull request is
        standing on is the head the force-push is leased against and the two
        are one fact on a branch this workflow keeps in step with its remote.
        """
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, PR_BRANCH, cwd=self._wt)
        self._open_pull_request()
        accepted = self._wt_head()
        state = self._gh.read_pinned_state(self._gh._issues[ISSUE])
        _exemption.record_exemption(state, accepted)
        _exemption.record_semantic_identity(
            state,
            base_sha=self._merge_base(),
            candidate_sha=accepted,
            fingerprint=self._contribution(),
        )
        self._gh.write_pinned_state(self._gh._issues[ISSUE], state)

    def _open_pull_request(self, label: str = LABEL_IN_REVIEW) -> None:
        """Put this issue on `label` with its pull request on the branch head."""
        self._gh = FakeGitHubClient()
        self._gh.add_issue(make_issue(ISSUE, label=label))
        self._gh.seed_state(
            ISSUE,
            pr_number=PR_NUMBER,
            branch=PR_BRANCH,
            review_round=REVIEW_ROUND,
        )
        self._gh.add_pr(FakePR(
            number=PR_NUMBER,
            head_branch=PR_BRANCH,
            merged=False,
            state=STATE_OPEN,
            head=FakePRRef(sha=self._wt_head()),
        ))

    def _commits_on_the_shared_file(self) -> None:
        """Give the base a shared file and the branch an edit at its far end.

        Run before the verdict is recorded, so what the adjudication accepts
        is a contribution a later base advance can change without conflicting
        with it.
        """
        self._commit_to_base(SHARED_FILE, _shared_body())
        self._git("fetch", ORIGIN_REMOTE, BASE_BRANCH, cwd=self._wt)
        # A rebase replays commits, so it needs a committer of its own: the
        # production one runs under the hardened envelope that injects one,
        # and a host with no git identity configured has none to inherit.
        self._git(
            "rebase", f"{ORIGIN_REMOTE}/{BASE_BRANCH}",
            cwd=self._wt, env_extra=self._author_env,
        )
        (self._wt / SHARED_FILE).write_text(_shared_body(last=BRANCH_EDIT))
        self._git(ADD_COMMAND, ".", cwd=self._wt)
        self._git(
            "commit", "-m", "feat: edit the shared tail",
            cwd=self._wt, env_extra=self._author_env,
        )

    def _commit_to_base(self, filename: str, body: str) -> None:
        """Write one commit onto the remote's base branch."""
        self._git("checkout", BASE_BRANCH, cwd=self._work)
        (self._work / filename).write_text(body)
        self._git(ADD_COMMAND, ".", cwd=self._work)
        self._git(
            "commit", "-m", f"base advance: {filename}",
            cwd=self._work, env_extra=self._author_env,
        )
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, BASE_BRANCH, cwd=self._work)

    def _merge_base(self) -> str:
        """The fork point this branch's contribution is read over."""
        return self._git(
            "merge-base", f"{ORIGIN_REMOTE}/{BASE_BRANCH}", "HEAD",
            cwd=self._wt,
        ).strip()

    def _contribution(self) -> str:
        """What the checkout really contributes over that fork point."""
        fingerprinted = _fingerprint._fingerprint_contribution(
            self._wt, self._merge_base(), self._wt_head(),
        )
        self.assertTrue(fingerprinted.is_fingerprinted)
        return fingerprinted.digest


def forged_base(fixture) -> str:
    """A commit over the real base that the remote does not carry.

    Made on the clone's own base branch and then rewound off it, so what is
    left is an object this host holds and the remote has never seen -- which
    is what a ref repointed under the tick names.
    """
    fixture._git("checkout", BASE_BRANCH, cwd=fixture._work)
    (fixture._work / FORGED_FILE).write_text("forged\n")
    fixture._git(ADD_COMMAND, ".", cwd=fixture._work)
    fixture._git(
        "commit", "-m", "forged base",
        cwd=fixture._work, env_extra=fixture._author_env,
    )
    forged = fixture._git("rev-parse", "HEAD", cwd=fixture._work).strip()
    fixture._git(
        "reset", "--hard", f"{ORIGIN_REMOTE}/{BASE_BRANCH}",
        cwd=fixture._work,
    )
    return forged


def events_of(fixture, family: str) -> list[dict]:
    """Every audit record of one family this tick left."""
    return [
        record for record in fixture._gh.recorded_events
        if record.get("event") == family
    ]
