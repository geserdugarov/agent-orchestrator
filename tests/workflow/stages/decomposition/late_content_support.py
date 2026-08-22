# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The issue thread the late content, guidance, and revision tests read.

One place builds a late issue whose generation is already baselined on its own
content, because that is the state every one of those tests starts from and
hand-writing two SHA-256 digests per fixture would pin the algorithm in the
fixtures instead of in the tests that are about it. The baseline is taken
through the production reader for the same reason: a fixture that computed its
own would keep agreeing with itself after the reader stopped agreeing with it.

The parks are described here too, because each of them is a durable pinned
pair a live issue would carry and the three modules beside this one seed the
same ones. Comment ids stay well below the fake client's own counter (which
starts at 1000), so a seeded human comment is never confused with one the
orchestrator posted during the call under test.

A park's own comment being REFUSED is described here for the same reason: it
is a thing that happens to this thread, every family of these tests has a park
that can hit it, and matching the refusal by content is what lets a tick whose
notice fails still say the other things it says.
"""
from __future__ import annotations

import unittest
from types import MappingProxyType

from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split.models import LateGeneration, LateVerdict
from orchestrator.workflow.stages.decomposition import (
    late_content as _late_content,
)

from tests.support.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeIssue,
    FakeUser,
    make_issue,
)
from tests.workflow.fixtures import LABEL_DECOMPOSING
from tests.workflow.stages.decomposition.late_run_support import (
    adjudicate,
    agent_reply,
)
from tests.workflow.stages.decomposition.late_test_support import (
    CANDIDATE_SHA,
    KEYS,
    KEY_PLAN_PATH,
    LATE_ISSUE_NUMBER,
    PLAN_PATH,
    PLAN_PR_NUMBER,
    QUESTION_ASKED,
    SHA_LENGTH,
)
from tests.workflow.stages.decomposition.late_test_support import (
    SPLIT_REPLY,
    late_generation,
    seed_plan_pr,
)

HUMAN = "geserdugarov"
OUTSIDER = "passer-by"

ISSUE_TITLE = "make the importer resumable"
ISSUE_BODY = "the importer has to survive a restart mid-batch"
EDITED_TITLE = "make the importer resumable AND idempotent"
EDITED_BODY = "the importer has to survive a restart and never double-write"

GUIDANCE_BODY = "the migration is a separate change; take it out of this one"
OTHER_GUIDANCE = "and leave the CLI flags alone"
BARE_CONTINUE = "/orchestrator continue"
CONTINUE_WITH_GUIDANCE = f"{BARE_CONTINUE}\n\nalso drop the retry loop"

GUIDANCE_ID = 11
CONTINUE_ID = 12
SECOND_ID = 13

# The id a park's own notice took. A park that announced itself ratcheted the
# shared consumed watermark past it, so a seeded park carries that too -- it is
# what makes a REPLY tell itself apart from conversation the issue was already
# carrying when the park fired, and what `reply` lands above.
PARK_NOTICE_ID = 100

REVISED_SHA = "d" * SHA_LENGTH
REVISED_BASE_SHA = "e" * SHA_LENGTH
REVISED_ADDITIONS = 310

KEY_TITLE_BODY_HASH = "late_title_body_hash"
KEY_COMMENT_HASH = "late_comment_hash"
KEY_COMMENT_WATERMARK = "late_comment_watermark_id"
KEY_LAST_ACTION_COMMENT_ID = "last_action_comment_id"
KEY_GENERATION = "late_generation"
KEY_ADDITIONS = "late_additions"

PARK_CONTENT_DRIFT = "late_content_drift"
PARK_REVISION_DIRTY = "late_revision_dirty"
PARK_REVISION_UNMEASURED = "late_revision_unmeasured"
PARK_REVISION_UNANSWERED = "late_revision_unanswered"
PARK_QUESTION = "late_question"

# What a refused comment fails with. GitHub's own refusals are typed, but what
# every caller here does with one is let it out, so the type is not the point.
COMMENT_REFUSED = "the comment was refused"

EVENT_LATE_MEASUREMENT = "late_measurement"
EVENT_AGENT_SPAWN = "agent_spawn"

ROLE_DEVELOPER = "developer"

STAGE_DECOMPOSING = "decomposing"


def human_comment(
    comment_id: int,
    body: str,
    *,
    login: str = HUMAN,
    user_type: str = "User",
) -> FakeComment:
    """One comment on the issue thread, authored by whoever is named."""
    return FakeComment(
        id=comment_id, body=body, user=FakeUser(login, user_type),
    )


def guidance_comment(comment_id: int = GUIDANCE_ID) -> FakeComment:
    """The trusted comment that says the work itself has to change."""
    return human_comment(comment_id, GUIDANCE_BODY)


def reply(issue: FakeIssue, body: str = GUIDANCE_BODY) -> FakeComment:
    """Append one trusted human reply after everything already on the thread.

    What a real reply to a park is: written once the human has read the notice,
    and therefore carrying an id above it. That means above everything the
    thread carries AND above `PARK_NOTICE_ID`, since a seeded park records the
    id its notice took rather than the comment itself. A test that seeds a
    comment by a fixed id is describing conversation the issue was already
    carrying when the park fired; this is the answer to what the workflow just
    said.
    """
    posted = human_comment(
        1 + max([
            PARK_NOTICE_ID,
            *(issue_comment.id for issue_comment in issue.comments),
        ]),
        body,
    )
    issue.comments.append(posted)
    return posted


class RefusedComment:
    """GitHub refusing one of the comments this tick is about to post.

    Matched on content rather than aimed at the client as a whole, because a
    tick that resumes a developer says two things -- that it is resuming, and
    later what stopped it -- and only the second is the park's own. An empty
    match refuses every comment, which is what a tick with one thing to say
    needs.

    Both the replacement and the scope it holds for, so a test says `with
    RefusedComment(github):` and the poster it displaces is the one put back.
    """

    def __init__(self, github: FakeGitHubClient, containing: str = "") -> None:
        self._github = github
        self._taken = github.comment
        self._containing = containing

    def __call__(self, issue: FakeIssue, body: str) -> FakeComment:
        if self._containing in body:
            raise RuntimeError(COMMENT_REFUSED)
        return self._taken(issue, body)

    def __enter__(self) -> "RefusedComment":
        self._github.comment = self
        return self

    def __exit__(self, *unused_error) -> bool:
        self._github.comment = self._taken
        return False


def baselined(
    generation: LateGeneration, issue: FakeIssue,
) -> LateGeneration:
    """The generation as a tick that took its content baseline records it.

    Read through the production fingerprint so a fixture cannot keep agreeing
    with a reader that changed.
    """
    signal = _late_content._read_content_signal(
        issue, PinnedState(data={}), generation,
    )
    return _late_content._rebaselined(generation, signal.fingerprint)


def late_issue(
    *,
    comments: tuple = (),
    generation: LateGeneration = None,
    baseline: bool = True,
    **extra_state,
) -> tuple[FakeGitHubClient, FakeIssue]:
    """A late issue carrying an oversized generation, baselined by default.

    `comments` are the ones the issue already carried when the candidate was
    frozen, so a default baseline covers them; a test about conversation that
    arrived AFTER the freeze appends to `issue.comments` instead.

    `baseline=False` is the generation a candidate was just frozen into and
    whose content fingerprints have still to be taken -- the one state in
    which nothing on the thread counts as drift.
    """
    github = FakeGitHubClient()
    issue = make_issue(
        LATE_ISSUE_NUMBER,
        label=LABEL_DECOMPOSING,
        title=ISSUE_TITLE,
        body=ISSUE_BODY,
        comments=list(comments),
    )
    github.add_issue(issue)
    recorded = late_generation() if generation is None else generation
    if baseline:
        recorded = baselined(recorded, issue)
    written = PinnedState(data=dict(extra_state))
    _late_state.write_late_generation(written, recorded)
    github.seed_state(LATE_ISSUE_NUMBER, **written.data)
    return github, issue


LATE_SESSION = "late-sess"

CATEGORY_SCOPE = "scope_ambiguous"

# The pinned run record a crashed tick left behind: a categorized question,
# recorded against this exact cycle, generation, and commit, which is what
# makes it an answer the next tick would otherwise reuse.
RECORDED_QUESTION = MappingProxyType({
    KEYS.verdict: str(LateVerdict.QUESTION),
    KEYS.category: CATEGORY_SCOPE,
    KEYS.question: QUESTION_ASKED,
    KEYS.run_cycle_id: late_generation().cycle_id,
    KEYS.run_generation: late_generation().generation,
    KEYS.source_sha: CANDIDATE_SHA,
    KEYS.session_id: LATE_SESSION,
})

# A completed adjudication that decided this candidate is one change: the
# record a later tick reuses instead of paying for a second run.
RECORDED_SINGLE = MappingProxyType({
    KEYS.verdict: str(LateVerdict.SINGLE),
    KEYS.run_cycle_id: late_generation().cycle_id,
    KEYS.run_generation: late_generation().generation,
    KEYS.source_sha: CANDIDATE_SHA,
    KEYS.session_id: LATE_SESSION,
})

ASKED_STATE = MappingProxyType({
    **RECORDED_QUESTION,
    KEYS.awaiting: True,
    KEYS.park_reason: PARK_QUESTION,
    KEY_LAST_ACTION_COMMENT_ID: PARK_NOTICE_ID,
})

DRIFT_PARKED = MappingProxyType({
    KEYS.awaiting: True,
    KEYS.park_reason: PARK_CONTENT_DRIFT,
    KEY_LAST_ACTION_COMMENT_ID: PARK_NOTICE_ID,
})

REVISION_PARKED = MappingProxyType({
    KEYS.awaiting: True,
    KEYS.park_reason: PARK_REVISION_DIRTY,
    KEY_LAST_ACTION_COMMENT_ID: PARK_NOTICE_ID,
})


class LateContentCase(unittest.TestCase):
    """One late issue and the adjudication a tick runs over it.

    The issue is held on the case rather than unpacked per test, so what each
    test says is what it did to the thread -- and so the seeding, the run, and
    the reads of what the run left are all spelled once.
    """

    def _seed(self, **state) -> None:
        github, issue = late_issue(**state)
        self.github = github
        self.issue = issue

    def _seed_with_plan_pr(self, **state) -> None:
        self._seed(
            pr_number=PLAN_PR_NUMBER, **{KEY_PLAN_PATH: PLAN_PATH}, **state,
        )
        self.plan_pr = seed_plan_pr(self.github)

    def _run(self, reply=SPLIT_REPLY, **run_fields):
        """One adjudication, defaulting to the verdict that leaves state put.

        These modules are about what a human's content does to a candidate,
        not about what a verdict earns, so the default reply is the one whose
        settlement neither posts nor rewrites: a split is handed on to the
        transaction that creates its children, which the harness holds unless
        a test asks for it -- so the generation, its fingerprints, and the
        thread stay exactly as this tick left them.
        """
        return adjudicate(
            self.github,
            self.issue,
            agent_reply(reply) if isinstance(reply, str) else reply,
            **run_fields,
        )

    def _pinned(self) -> dict:
        return self.github.pinned_data(LATE_ISSUE_NUMBER)

    def _bodies(self) -> list:
        return [body for _, body in self.github.posted_comments]

    def _events_named(self, family: str) -> list:
        return [
            record for record in self.github.recorded_events
            if record.get("event") == family
        ]
