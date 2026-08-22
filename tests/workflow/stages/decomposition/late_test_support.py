# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The one oversized candidate the late-mode tests adjudicate.

One frozen generation described once, so the hold, the prompt, the reply
parser, the pinned run record, and the coordinator over them all read the same
candidate: a field added to the record is exercised by every one of them
without five copies of the fixture drifting apart. The harness those tests run
a coordinator inside is the module beside this one.

The pinned keys are gathered on one record rather than spelled loose, because
they are the compatibility contract the late run round-trips through: a test
naming one of them is naming the durable key a live issue would carry.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from orchestrator.git.measurement.models import (
    AdditionMeasurement,
    MeasurementFailure,
)
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split.models import LateGeneration, LatePhase

from tests.support.fakes import FakeGitHubClient, FakeIssue, FakePR, make_issue
from tests.workflow.fixtures import LABEL_DECOMPOSING

SHA_LENGTH = 40
CANDIDATE_SHA = "a" * SHA_LENGTH
BASE_SHA = "b" * SHA_LENGTH
OTHER_SHA = "c" * SHA_LENGTH
MERGED_SHA = "f" * SHA_LENGTH

CYCLE_ID = 3
GENERATION_NUMBER = 1
NEXT_GENERATION = 2
ROOT_ISSUE = 41
LATE_ISSUE_NUMBER = 41
LINEAGE_DEPTH = 1
THRESHOLD = 4000
ADDITIONS = 9123
UNDERSIZED_ADDITIONS = 12
SCOPE = "the declared slice this generation owns"

PLAN_PR_NUMBER = 77
PLAN_PR_BODY = "the design this plan PR was opened with"
PLAN_BRANCH = "orchestrator/plan"

# What proves a recorded pull request is this issue's plan rather than an
# implementation. The discussion stage's own record, which the implementing
# stage tells the two apart by and the late hold reads through it.
KEY_PLAN_PATH = "discussion_plan_path"
PLAN_PATH = "plans/issue-41.md"

# What a re-measurement answers when a test did not ask for one. Every path
# that reaches the real counter shells out to git in the scratch worktree, so
# the seam is always held -- and held at a failure, because a test that
# reaches it without saying what it expects has not decided anything.
UNASKED_MEASUREMENT = AdditionMeasurement(
    failure=MeasurementFailure.DIFF_FAILED,
)

LATE_SESSION_ID = "late-sess"
LATE_SPEC = "claude --effort high"
LATE_BACKEND = "claude"
LATE_ARGS = ("--effort", "high")
ROLE_DECOMPOSER = "decomposer"

HOLD_MARKER_PREFIX = "<!--orchestrator-late-hold"

LATE_FENCE = "orchestrator-late-manifest"

EVENT_LATE_VERDICT = "late_verdict"
EVENT_LATE_FAILURE = "late_failure"


@dataclass(frozen=True)
class _StateKeys:
    """The pinned keys the late run and its generation round-trip through."""

    agent: str = "late_agent"
    role: str = "late_agent_role"
    session_id: str = "late_session_id"
    run_cycle_id: str = "late_run_cycle_id"
    source_sha: str = "late_source_sha"
    run_generation: str = "late_run_generation"
    verdict: str = "late_result_verdict"
    category: str = "late_result_category"
    question: str = "late_result_question"
    children: str = "late_result_children"
    plan_pr_number: str = "late_plan_pr_number"
    plan_pr_body: str = "late_plan_pr_body"
    candidate_sha: str = "late_candidate_sha"
    base_sha: str = "late_base_sha"
    threshold: str = "late_threshold"
    additions: str = "late_additions"
    phase: str = "late_phase"
    cancelled: str = "late_cancelled"
    cancelled_at: str = "late_cancelled_at"
    resources: str = "late_resources"
    owner_check_pending: str = "late_owner_check_pending"
    exempt_sha: str = "late_exempt_sha"
    retry_count: str = "retry_count"
    retry_window: str = "retry_window_start"
    agent_runs: str = "issue_agent_runs"
    awaiting: str = "awaiting_human"
    park_reason: str = "park_reason"
    park_notice: str = "late_park_notice"


KEYS = _StateKeys()


def late_block(payload: str) -> str:
    """Wrap a payload in the fence a late reply is read out of."""
    return f"```{LATE_FENCE}\n{payload}\n```"


SINGLE_REPLY = late_block(
    '{"decision": "single", "rationale": "one coherent change",'
    ' "category": "generated_artifacts"}'
)

SPLIT_REPLY = late_block(
    '{"decision": "split", "rationale": "two slices",'
    ' "children": [{"title": "A", "body": "a"},'
    ' {"title": "B", "body": "b", "depends_on": [0]}]}'
)

QUESTION_ASKED = "which half of this is in scope?"

QUESTION_REPLY = late_block(
    '{"decision": "question", "category": "scope_ambiguous",'
    f' "question": "{QUESTION_ASKED}"}}'
)

NO_BLOCK_REPLY = "I looked at the diff and it seems fine to me."


def late_generation(**overrides) -> LateGeneration:
    """The oversized generation every late-mode test starts from."""
    return replace(
        LateGeneration(
            cycle_id=CYCLE_ID,
            generation=GENERATION_NUMBER,
            root_issue=ROOT_ISSUE,
            current_issue=LATE_ISSUE_NUMBER,
            lineage_depth=LINEAGE_DEPTH,
            scope=SCOPE,
            candidate_sha=CANDIDATE_SHA,
            base_sha=BASE_SHA,
            threshold=THRESHOLD,
            additions=ADDITIONS,
            phase=LatePhase.MEASURING,
        ),
        **overrides,
    )


def seed_late_issue(
    github: FakeGitHubClient,
    generation: LateGeneration,
    **extra_state,
) -> FakeIssue:
    """Add the late issue to a fake client with its generation recorded.

    An absent `generation` seeds an issue that never entered the gate, since
    a record with no cycle identity writes no late fields at all.
    """
    issue = make_issue(LATE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
    github.add_issue(issue)
    recorded = PinnedState(data=dict(extra_state))
    _late_state.write_late_generation(recorded, generation)
    github.seed_state(LATE_ISSUE_NUMBER, **recorded.data)
    return issue


def generation_state(generation: LateGeneration) -> dict:
    """The pinned fields one generation round-trips through."""
    written = PinnedState(data={})
    _late_state.write_late_generation(written, generation)
    return written.data


def seeded_late_issue(**extra_state) -> tuple[FakeGitHubClient, FakeIssue]:
    """A fresh fake client carrying the standard oversized generation."""
    github = FakeGitHubClient()
    return github, seed_late_issue(github, late_generation(), **extra_state)


def seed_plan_pr(
    github: FakeGitHubClient,
    *,
    body: str = PLAN_PR_BODY,
    pr_state: str = "open",
) -> FakePR:
    """Add an open (or settled) plan PR the late hold can reconcile."""
    plan_pr = FakePR(
        number=PLAN_PR_NUMBER,
        head_branch=PLAN_BRANCH,
        body=body,
        state=pr_state,
    )
    github.add_pr(plan_pr)
    return plan_pr
