# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The one rewrite the transfer's tests grant, refuse, or settle a permit for.

A squash-on-approval of the exact commit an adjudication accepted, described
once: the pinned comment that records the verdict, the evidence the squash
hands in, the publication the gate froze, and the world the two readings the
permit spends -- the checkout and the two fingerprints -- answer in. A case
about one refusal seeds exactly that one and leaves the rest ordinary.

The far end of the same rewrite is seeded from here too, because it is the
same world one write on: `granted` is the comment a permit's own write leaves
-- the permission and the debt the push it licenses still owes -- and
`open_pull_request` is the remote the push is made onto, standing either where
the permit was granted or already on the commit it licensed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator import config
from orchestrator.git.measurement.models import (
    AdditionMeasurement,
    ContributionFingerprint,
    FingerprintFailure,
    FrozenCommit,
    MeasurementFailure,
    _BaseObject,
)
from orchestrator.git.verification.probes import _WorktreeStatus
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    rewrites as _rewrites,
)
from orchestrator.workflow.stages.implementing import (
    late_parks as _parks,
    late_records as _records,
    state as _state,
)
from orchestrator.workflow.state import WorkflowLabel
from tests.support.fakes import (
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow.git_owners import seam_patch

ISSUE_NUMBER = 42
PR_NUMBER = 77
SOURCE_STAGE = WorkflowLabel.VALIDATING

SHA_LENGTH = 40
DIGEST_LENGTH = 64

# The commit a human adjudicated, the base it was measured over, and the
# object the squash replaced it with over that same base.
ACCEPTED_SHA = "a" * SHA_LENGTH
MERGE_BASE_SHA = "b" * SHA_LENGTH
REWRITTEN_SHA = "c" * SHA_LENGTH
STRANGER_SHA = "d" * SHA_LENGTH

# A whole object id this issue has nothing to do with: another commit that
# types exactly as any of the four above, which is what a hand edit can move a
# recorded end to without the reader refusing it.
FOREIGN_SHA = "7" * SHA_LENGTH

# The head the pull request is standing on, which is what the force-push is
# leased against. Deliberately NOT the commit the squash collapsed: the entry
# admits a tip a durable record says this issue's own push put there, so the
# two are separate facts and a fixture that spelled them alike would let one
# stand in for the other unnoticed.
LEASED_SHA = "9" * SHA_LENGTH

# What the accepted contribution fingerprints to, and what an unequal one does.
ACCEPTED_DIGEST = "e" * DIGEST_LENGTH
OTHER_DIGEST = "f" * DIGEST_LENGTH

WORKTREE = Path("/tmp/orchestrator-test-late-transfer")

SPEC = config.RepoSpec(
    slug="chippingway/orchestrator",
    target_root=Path("/tmp/orchestrator-test-target-root"),
    base_branch="main",
)

# The branch a gated push names, and the seam that stands in for the request.
BRANCH = "orchestrator/chippingway__orchestrator/issue-42"
PUSH_BRANCH = "_push_branch"

# The three seams the ordinary cumulative reading spends, which a case about
# a REFUSED permit has to seed: falling through to that reading is what the
# refusal costs, and what it publishes is what the settlement then sees.
FREEZE_BASE = "_freeze_base_commit"
BASE_PRESENT = "_base_object_present"
COUNT_ADDED_LINES = "_count_added_lines"

# A count the configured ceiling lets through, so the fallback reading ends in
# a push rather than in the adjudication.
UNDER_THE_CEILING = 3

# The seams a permit spends, named on the owners that define them.
PROVE_CANDIDATE = "_prove_candidate_commit"
WORKTREE_STATUS = "_worktree_status"
FINGERPRINT = "_fingerprint_contribution"

CLEAN = _WorktreeStatus(readable=True)

# The revision a checkout's own head is named by. Every other revision the
# permit asks about is a commit some record names by id.
_HEAD = "HEAD"


# Which reading a settlement was proved by, which the record now
# carries until the report it owes has been made.
_SETTLING_PROOF = _rewrites.LateRewriteProof.PUSHED



def rewrite(**overrides) -> _rewrites.LateRewrite:
    """The evidence the squash hands in, with any one term replaced."""
    return _rewrites.LateRewrite(**{
        "kind": _rewrites.LateRewriteKind.SQUASH,
        "from_sha": ACCEPTED_SHA,
        "from_base_sha": MERGE_BASE_SHA,
        "to_sha": REWRITTEN_SHA,
        "to_base_sha": MERGE_BASE_SHA,
        "pr_number": PR_NUMBER,
        "source_stage": SOURCE_STAGE,
        "lease": LEASED_SHA,
        **overrides,
    })


def entry(**overrides) -> _records._PublicationEntry:
    """The publication the gate froze before the rewrite was measured."""
    return _records._PublicationEntry(**{
        "stage": SOURCE_STAGE,
        "pr_number": PR_NUMBER,
        "published_sha": LEASED_SHA,
        **overrides,
    })


@dataclass(frozen=True)
class Adjudicated:
    """The issue a settled `single` verdict left, and what it was recorded on."""

    github: FakeGitHubClient
    issue: object
    state: object


def adjudicated(
    *,
    identity: bool = True,
    digest: str = ACCEPTED_DIGEST,
    base: str = MERGE_BASE_SHA,
    labels: tuple | None = None,
) -> Adjudicated:
    """The pinned comment a settled `single` verdict leaves behind.

    `identity=False` is the legacy shape: a comment written before the
    semantic record existed, or one whose fingerprint could not be taken, so
    only the exact commit is exempt.

    `base` is the pair's other end, replaceable because it is the one field a
    hand edit can move without the record refusing to read back: a whole
    object id naming some other commit types exactly as the frozen base does.

    `labels` is what the issue reads back as when the transfer re-fetches it,
    seeded on the issue rather than written through the client because the
    relabel that put the stage there happened long before this tick.
    """
    github = FakeGitHubClient()
    issue = make_issue(ISSUE_NUMBER)
    named = (str(SOURCE_STAGE),) if labels is None else labels
    issue.labels.extend(FakeLabel(name) for name in named)
    github.add_issue(issue)
    github.seed_state(ISSUE_NUMBER, **{_state._PR_NUMBER: PR_NUMBER})
    state = github.read_pinned_state(issue)
    _exemption.record_exemption(state, ACCEPTED_SHA)
    if identity:
        _exemption.record_semantic_identity(
            state,
            base_sha=base,
            candidate_sha=ACCEPTED_SHA,
            fingerprint=digest,
        )
    github.write_pinned_state(issue, state)
    return Adjudicated(github=github, issue=issue, state=state)


def spent(state) -> None:
    """The comment a settled transfer leaves, through the write that makes it.

    Three fields, because that is what "spent" means on the comment: the
    exemption and the identity beside it describe the pair the rewrite
    produced, and the phase says the move is done. Written through the record
    owner rather than spelled here, so a case about what a reader does past
    the receipt is seeded with exactly what the receipt leaves.
    """
    _rewrites.record_rewrite_publication(state, _SETTLING_PROOF)


def gate(github, issue, state, **overrides) -> _records._Gate:
    """The subject one gate call taken past publication is about."""
    return _records._Gate(**{
        "gh": github,
        "spec": SPEC,
        "issue": issue,
        "state": state,
        "worktree": WORKTREE,
        "reconciling": True,
        "candidate": REWRITTEN_SHA,
        "entry": entry(),
        "rewrite": rewrite(),
        **overrides,
    })


def _over(base_sha: str) -> str:
    """The digest a pair read over this base contributes, unseeded.

    One answer for the merge base the rewrite really sits on and another for
    every other end, since a contribution is what a candidate adds over ITS
    base and two bases are two contributions.
    """
    return ACCEPTED_DIGEST if base_sha == MERGE_BASE_SHA else OTHER_DIGEST


class Readings:
    """The three readings a permit spends, and what each answers this case.

    One controller installed once rather than a patch per case, because the
    refusals are a family: every one of them is the ordinary world with a
    single reading replaced, and a case that re-entered the whole patch set to
    move one of them would stack doubles over doubles.

    The fingerprints are keyed on the CANDIDATE rather than seeded as a
    sequence, so a case naming one side says which side it means: the accepted
    contribution and the rewritten one are read in order, and a positional
    seed would silently swap them if that order ever changed.

    What a case seeds nothing for still depends on the BASE, because that is
    what a contribution is: only the merge base both sides of this rewrite are
    read over answers with the digest the adjudication recorded, and any other
    end answers with a different one. Without that a hand-edited base would
    fingerprint identically to the frozen one and every reading of it would
    agree by construction.

    `absent` is the other half of the ordinary world being ordinary: every
    commit the evidence names is an object this host holds unless a case says
    otherwise.
    """

    def __init__(self) -> None:
        self.head = FrozenCommit(sha=REWRITTEN_SHA)
        self.tree = CLEAN
        self.digests: dict = {}
        self.absent: set = set()

    def stands_on(self, head) -> None:
        """Put the checkout on this commit, or on this failed proof."""
        self.head = head if isinstance(head, FrozenCommit) else FrozenCommit(
            sha=head,
        )

    def proved(self, worktree, revision) -> FrozenCommit:
        """What one revision the permit names proves to.

        Two answers behind one seam, because the permit asks it two different
        questions: what the checkout stands on, and whether a commit the
        EVIDENCE names is an object this host still holds. A revision a case
        put in `absent` answers the way one made on another host does -- it
        resolves to itself and will not peel -- which is the whole reason a
        whole-looking id is not proof of anything.
        """
        if revision == _HEAD:
            return self.head
        if revision in self.absent:
            return FrozenCommit(
                sha=revision, failure=MeasurementFailure.CANDIDATE_ABSENT,
            )
        return FrozenCommit(sha=revision)

    def status(self, worktree) -> _WorktreeStatus:
        """What `git status` said about the tree a push would publish from."""
        return self.tree

    def fingerprint(
        self, worktree, base_sha: str, candidate_sha: str,
    ) -> ContributionFingerprint:
        """What one pair contributes, as the digest naming it."""
        answered = self.digests.get(candidate_sha) or _over(base_sha)
        if isinstance(answered, FingerprintFailure):
            return ContributionFingerprint(
                base_sha=base_sha, candidate_sha=candidate_sha,
                failure=answered,
            )
        return ContributionFingerprint(
            base_sha=base_sha, candidate_sha=candidate_sha, digest=answered,
        )


def readings(fixture) -> Readings:
    """Install the ordinary world a transfer is granted in, and hand it back.

    The checkout stands on the rewritten commit over a provably clean tree,
    and both contributions fingerprint to the digest the adjudication
    recorded, so a case that touches nothing is a permit and a case about a
    refusal moves exactly one answer.
    """
    answers = Readings()
    fixture.enterContext(seam_patch(PROVE_CANDIDATE, answers.proved))
    fixture.enterContext(seam_patch(WORKTREE_STATUS, answers.status))
    fixture.enterContext(seam_patch(FINGERPRINT, answers.fingerprint))
    return answers


def open_pull_request(github, standing: str = LEASED_SHA) -> None:
    """Stand this issue's open pull request on one head.

    `standing` is the whole of what a settlement case is about: the head the
    permit was granted against is the ordinary remote, and the rewritten
    commit is the one a tick that pushed and died before its receipt comes
    back to.
    """
    github.add_pr(FakePR(
        number=PR_NUMBER,
        head_branch=BRANCH,
        head=FakePRRef(sha=standing),
    ))


def granted(state, **overrides) -> _rewrites.LateRewrite:
    """The comment a permit's own write leaves, and the rewrite it is for.

    Both halves, because the grant writes both: the permission that says what
    the push may carry over, and the debt that says the push is owed at all.
    A case seeding one without the other would be seeding a comment no grant
    ever produced.
    """
    permitted = rewrite(**overrides)
    _rewrites.record_rewrite_authorization(state, permitted, ACCEPTED_DIGEST)
    _parks._approve(state, permitted.to_sha, permitted.lease)
    return permitted


def measures(fixture, additions: int = UNDER_THE_CEILING) -> None:
    """Let the ordinary cumulative reading run, and come back this size.

    What a case about a REFUSED permit needs and no other case here does: the
    refusal is not a hold, so the rewritten commit falls through to the
    measurement, and only a count under the ceiling reaches the push whose
    receipt the settlement rides.
    """
    fixture.enterContext(seam_patch(
        FREEZE_BASE, lambda spec, worktree: FrozenCommit(sha=MERGE_BASE_SHA),
    ))
    fixture.enterContext(seam_patch(
        BASE_PRESENT,
        lambda spec, worktree, base_sha: _BaseObject(present=True),
    ))
    fixture.enterContext(seam_patch(
        COUNT_ADDED_LINES,
        lambda worktree, base_sha, candidate_sha: AdditionMeasurement(
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            additions=additions,
        ),
    ))
