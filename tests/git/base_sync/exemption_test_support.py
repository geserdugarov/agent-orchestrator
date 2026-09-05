# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The adjudicated issue one refresh-time rebase is decided over, as doubles.

The composed refresh takes the readings a transfer turns on and no other
base-sync case seeds: which commit each revision proves to, what the REMOTE
says the base branch is at, and what each of the two contributions
fingerprints to. They are answered here as one world -- the ordinary one, in
which the replay contributes exactly what the adjudication accepted -- so a
case about a refusal moves the single answer it is about and says nothing
else.

The revisions are keyed rather than sequenced because the permit asks about
two different things: the checkout's own head and the pre-rebase anchor its
force-push is leased against. A positional double would silently swap them the
moment that order changed.

The base is answered by the freeze rather than by peeling a local ref, because
that is the reading the production evidence takes: the ref a rebase names
lives in a store the agent writes to, so what a transfer is granted over has
to be the base the remote itself answers for.

The tick itself is here too, in the four moments a process can be stopped at
and the resumption that comes back to each. They are one fixture because they
are one sequence -- anchor, rebase, grant, push, receipt, route -- and a case
about any window in it says only where the process stopped and where the
remote ended up.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestrator.git.base_sync import (
    publication as _base_publication,
    startup as _base_startup,
)
from orchestrator.git.measurement import (
    commits as _measurement_commits,
    fingerprint as _measurement_fingerprint,
)
from orchestrator.git.measurement.models import (
    ContributionFingerprint,
    FrozenCommit,
    MeasurementFailure,
)
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    rewrites as _rewrites,
)
from tests.git.base_sync.refresh_scenarios import (
    PUSH_PATCH,
    _clean_rebase_scenario,
    _scenario,
)
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    BEFORE_SHA,
    ISSUE,
    PR_NUMBER,
    THREE_BEHIND_STDOUT,
    UP_TO_DATE_STDOUT,
    FakePRRef,
    _patched,
    _RemoteHeadGit,
    _SyncWorktreeWithBaseFixture,
)
from tests.git.base_sync.sync_test_support import _diverged, _git_result

EVENT_MEASUREMENT = "late_measurement"
EVENT_TRANSFER = "late_transfer"

# The keyword a gated push names the commit it publishes by, and the one it
# pins that push to.
REVISION = "revision"
LEASE = "force_with_lease"

# What a process that never came back looks like from inside the tick, and the
# two methods a recovery that finishes its route records itself under.
DIED = "the process died before the tick returned"
RECOVERY_PUSHED = "crash_recovery_pushed"
RECOVERY_RELABELLED = "crash_recovery_relabel_only"

SHA_LENGTH = 40
DIGEST_LENGTH = 64

# The base the adjudication measured the accepted commit over, and the base
# the rebase replayed it onto. Two commits, because that is what a base
# advance is -- and what makes the digest, rather than the pair, the thing the
# equality is read from.
ACCEPTED_BASE_SHA = "acce97ed" * 5
REPLAYED_BASE_SHA = "5eba5ed0" * 5

# A commit a human ruled on that is NOT the head the rebase found. The two are
# ordinarily one, and the record never says so: what a transfer is granted on
# is the equality of two contributions, which two distinct commits can carry.
ACCEPTED_SHA = "a55e55ed" * 5

# What the contribution a human ruled on fingerprints to, and what a base
# advance that changed it fingerprints to instead.
ACCEPTED_DIGEST = "d" * DIGEST_LENGTH
CHANGED_DIGEST = "c" * DIGEST_LENGTH

# The revision a checkout's own head is named by. Every other revision the
# permit asks about is a commit some record names by id.
HEAD_REVISION = "HEAD"


class Readings:
    """What each revision proves to, what the remote answers, and the digests.

    `base` is the freeze, and it is a whole `FrozenCommit` rather than a SHA
    because the ways it fails are the point: a remote that would not name the
    branch, and an object this host does not hold even after a fetch, each
    leave the evidence with no base to read a contribution over.
    """

    def __init__(self) -> None:
        self.proved = {
            HEAD_REVISION: AFTER_SHA,
            BEFORE_SHA: BEFORE_SHA,
        }
        self.base = FrozenCommit(sha=REPLAYED_BASE_SHA)
        # The commits this host turns out not to hold. A revision only ever
        # resolves to itself here, so a case about an object the store lost
        # says which one rather than removing an answer.
        self.unheld: set = set()
        self.digests = {
            (ACCEPTED_BASE_SHA, BEFORE_SHA): ACCEPTED_DIGEST,
            (REPLAYED_BASE_SHA, AFTER_SHA): ACCEPTED_DIGEST,
        }

    def prove(self, _worktree, revision: str) -> FrozenCommit:
        """The commit one revision names, or the object this host has not."""
        if revision in self.unheld:
            return FrozenCommit(failure=MeasurementFailure.CANDIDATE_ABSENT)
        return FrozenCommit(sha=self.proved.get(revision, revision))

    def freeze(self, _spec, _worktree) -> FrozenCommit:
        """The commit the remote says this spec's base branch is at."""
        return self.base

    def fingerprint(
        self, _worktree, base_sha: str, candidate_sha: str,
    ) -> ContributionFingerprint:
        """What one pair contributes, as the digest naming it."""
        return ContributionFingerprint(
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            digest=self.digests.get(
                (base_sha, candidate_sha), CHANGED_DIGEST,
            ),
        )


def readings(test_case) -> Readings:
    """Install the world an equivalent replay is granted in, and hand it back."""
    answers = Readings()
    _patched(
        test_case, _measurement_commits, "_prove_candidate_commit",
        answers.prove,
    )
    _patched(
        test_case, _measurement_commits, "_freeze_base_commit", answers.freeze,
    )
    _patched(
        test_case, _measurement_fingerprint, "_fingerprint_contribution",
        answers.fingerprint,
    )
    return answers


def adjudicated(
    test_case, *, identity: bool = True, accepted: str = BEFORE_SHA,
) -> None:
    """Record the verdict a settled `single` left, on the head it accepted.

    `identity=False` is the legacy shape: a comment written before the
    semantic record existed, so the exact commit is exempt and nothing on it
    says what that commit contributes.

    `accepted` is the commit a human ruled on, and it defaults to the head the
    rebase finds because that is the ordinary case rather than the rule. A
    case naming another commit is seeding the world where the two are distinct
    carriers of one contribution, and it seeds the digest for that pair too.
    """
    issue = test_case.gh._issues[ISSUE]
    state = test_case.gh.read_pinned_state(issue)
    _exemption.record_exemption(state, accepted)
    if identity:
        _exemption.record_semantic_identity(
            state,
            base_sha=ACCEPTED_BASE_SHA,
            candidate_sha=accepted,
            fingerprint=ACCEPTED_DIGEST,
        )
    test_case.gh.write_pinned_state(issue, state)


class _DiesAfterTheRelabel:
    """A client whose relabel lands and whose process does not come back.

    The relabel is the second-to-last thing a finish does, and the pinned
    write that clears the attempt is the last -- so this is the whole of the
    window between them, staged where it really is rather than by seeding a
    comment nothing wrote.
    """

    def __init__(self, relabel) -> None:
        self._relabel = relabel

    def __call__(self, issue, label) -> None:
        """Apply the label the finish chose, then stop the tick."""
        self._relabel(issue, label)
        raise RuntimeError(DIED)


class _CleanRebaseCase(_SyncWorktreeWithBaseFixture):
    """One behind-base issue in review whose head a human already ruled on."""

    def setUp(self) -> None:
        super().setUp()
        self.reading = readings(self)
        self._seed_pr_issue(review_round=3)

    def _rebases(self, **scenario_options):
        """Run one refresh over the seeded world and hand back its scenario."""
        scenario = _clean_rebase_scenario(
            THREE_BEHIND_STDOUT, **scenario_options,
        )
        scenario.run(self)
        return scenario

    def _durable(self):
        """The pinned comment as a process starting now would read it."""
        return self.gh.read_pinned_state(self.gh._issues[ISSUE])

    def _events_of(self, family: str) -> list[dict]:
        return [
            record for record in self.gh.recorded_events
            if record.get("event") == family
        ]

    def _assert_measured(self) -> None:
        """The ordinary cumulative gate read this replay and published it."""
        self.assertEqual(len(self._events_of(EVENT_MEASUREMENT)), 1)
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))

    def _crashes_before_the_grant(self) -> None:
        """Pin the anchor, rebase, and die on the way to the evidence.

        The base the replay sits over is one end of the contribution a permit
        is granted on and the last reading taken before the gate is entered,
        so a process stopped there leaves the rewrite on the branch with
        nothing on the comment naming it.
        """
        with patch.object(
            _measurement_commits, "_freeze_base_commit",
            MagicMock(side_effect=RuntimeError(DIED)),
        ):
            self._dies_mid_tick()

    def _crashes_before_the_record(self) -> None:
        """Rebase for real, and die on the statement that names what it made.

        The narrowest window one attempt has: `git rebase` has replayed the
        branch and the write that says which commit that produced never
        happened, so what the next tick comes back to is a divergent checkout
        and a comment carrying the anchor and the terms the attempt was
        entered under.
        """
        with patch.object(
            _base_startup, "_record_the_rewrite",
            MagicMock(side_effect=RuntimeError(DIED)),
        ):
            self._dies_mid_tick()

    def _crashes_before_the_push(self) -> None:
        """Rebase, grant the transfer, and die on the way to the remote."""
        crashing = _clean_rebase_scenario(THREE_BEHIND_STDOUT)
        crashing[PUSH_PATCH].side_effect = RuntimeError(DIED)
        with self.assertRaises(RuntimeError):
            crashing.run(self)

    def _crashes_before_the_route(self) -> None:
        """Publish and receipt the rewrite, and die before the notice.

        The far side of the same tick: the push landed, the receipt and the
        rotation are durable, and what never happened is the notice, the audit
        event, the cleared anchor, and the reviewer's route back.
        """
        with patch.object(
            _base_publication, "_post_auto_rebase_notice",
            MagicMock(side_effect=RuntimeError(DIED)),
        ):
            self._dies_mid_tick()

    def _resumes(
        self,
        *,
        remote_head: str = BEFORE_SHA,
        local_head: str = AFTER_SHA,
        diverged: tuple = (1, 0),
        dirty: tuple = (),
        push: bool = True,
        behind: bool = False,
    ):
        """The next tick, over the world one of those crashes left behind.

        `remote_head` is the whole of what says which crash it was: the anchor
        for a push that never went out, the replay for one that landed and
        lost its receipt, and anything else for a branch somebody moved. It is
        answered on the pull request as well as on the fetched ref, because
        the gate reads one and the recovery comparison reads the other.

        `local_head` is the other side of the same question, and a case moves
        it to say the branch was put BACK -- a reset that landed without its
        park write, or a hand at the checkout.

        `behind` says the base moved again while the process was down, which
        is the one reading that outlives the recovery: whatever it decides,
        the tick behind it still owes this branch the rebase the new base
        earned. The rebase seam answers for that one the way the crashed tick
        answered for its own.
        """
        self.gh.pulls[PR_NUMBER].head = FakePRRef(sha=remote_head)
        lagging = THREE_BEHIND_STDOUT if behind else UP_TO_DATE_STDOUT
        resumed = _scenario(
            dirty=MagicMock(return_value=list(dirty)),
            rebase=MagicMock(return_value=(True, [])),
            push=MagicMock(return_value=push),
            head_sha=MagicMock(return_value=local_head),
            git=MagicMock(return_value=_git_result(stdout=lagging)),
            hardened=MagicMock(side_effect=_RemoteHeadGit(remote_head)),
            fetch=MagicMock(return_value=_git_result()),
            ahead_behind=MagicMock(return_value=_diverged(*diverged)),
        )
        resumed.run(self)
        return resumed

    def _crashes_at_the_relabel(self) -> None:
        """Record that the finish announced itself, and never route.

        One statement earlier than the window below, and the one that leaves
        the reviewer where the rebase found them: the notice and the audit
        event are out, the comment says so, and the label has not moved.
        """
        with patch.object(
            self.gh, "set_workflow_label",
            MagicMock(side_effect=RuntimeError(DIED)),
        ):
            self._dies_mid_tick()

    def _crashes_after_the_relabel(self) -> None:
        """Publish, receipt, and die between the relabel and the write.

        The last window one finish has: the reviewer has already been routed
        at the rewritten head and the pinned comment still carries the whole
        record of the attempt, including the stage it started from.
        """
        relabelled = self.gh.set_workflow_label
        with patch.object(
            self.gh, "set_workflow_label", _DiesAfterTheRelabel(relabelled),
        ):
            self._dies_mid_tick()

    def _dies_mid_tick(self) -> None:
        """Run one clean rebase whose tick is stopped by the caller's seam."""
        with self.assertRaises(RuntimeError):
            _clean_rebase_scenario(THREE_BEHIND_STDOUT).run(self)
