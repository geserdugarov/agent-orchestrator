# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests.decomposition_test_support import (
    _comment_with_marker,
    _comments_for_issue,
)
from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

LABEL_DECOMPOSING = "decomposing"
LABEL_DONE = "done"
KEY_USER_CONTENT_HASH = "user_content_hash"
STALE_USER_CONTENT_HASH = "stale-hash"
PRIOR_TICK_USER_CONTENT_HASH = "stale-hash-from-prior-tick"
PICKUP_COMMENT_ID = 900
READY_DRIFT_ISSUE_NUMBER = 50
STABLE_READY_ISSUE_NUMBER = 51
DECOMPOSING_DRIFT_ISSUE_NUMBER = 90
HUMAN_COMMENT_ID = 2000
LAST_ACTION_COMMENT_ID = 1500
BLOCKED_PARENT_NUMBER = 300
BLOCKED_PARENT_CHILD_NUMBER = 301
BLOCKED_CHILD_NUMBER = 310
BLOCKED_CHILD_PARENT_NUMBER = 309
UMBRELLA_NUMBER = 400
UMBRELLA_CHILD_NUMBERS = (401, 402)


def _decomposing_drift_fixture():
    github = FakeGitHubClient()
    issue = make_issue(
        DECOMPOSING_DRIFT_ISSUE_NUMBER,
        label=LABEL_DECOMPOSING,
        body="updated decomposition input",
    )
    issue.comments.append(
        FakeComment(
            id=HUMAN_COMMENT_ID,
            body="please reconsider",
            user=FakeUser("alice"),
        )
    )
    github.add_issue(issue)
    github.seed_state(
        DECOMPOSING_DRIFT_ISSUE_NUMBER,
        user_content_hash=STALE_USER_CONTENT_HASH,
        decomposer_agent="claude",
        decomposer_session_id="old-sess",
        awaiting_human=True,
        park_reason=None,
        last_action_comment_id=LAST_ACTION_COMMENT_ID,
        pickup_comment_id=PICKUP_COMMENT_ID,
    )
    return github, issue


def _blocked_parent_drift_fixture():
    github = FakeGitHubClient()
    parent = make_issue(
        BLOCKED_PARENT_NUMBER,
        label="blocked",
        body="updated parent body",
    )
    github.add_issue(parent)
    github.add_issue(make_issue(BLOCKED_PARENT_CHILD_NUMBER, label="implementing"))
    github.seed_state(
        BLOCKED_PARENT_NUMBER,
        children=[BLOCKED_PARENT_CHILD_NUMBER],
        decomposer_session_id="old-sess",
        user_content_hash=STALE_USER_CONTENT_HASH,
    )
    return github, parent


def _umbrella_drift_fixture():
    github = FakeGitHubClient()
    umbrella = make_issue(
        UMBRELLA_NUMBER,
        label="umbrella",
        body="updated umbrella body",
    )
    github.add_issue(umbrella)
    for child_number in UMBRELLA_CHILD_NUMBERS:
        github.add_issue(make_issue(child_number, label=LABEL_DONE))
    github.seed_state(
        UMBRELLA_NUMBER,
        children=list(UMBRELLA_CHILD_NUMBERS),
        umbrella=True,
        user_content_hash=STALE_USER_CONTENT_HASH,
    )
    return github, umbrella


class HandleReadyRoutesBackOnHashChangeTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    def test_body_drift_routes_ready_back(self) -> None:
        # `ready` is reached only after a `single` decomposition decision
        # (no children created), so re-decomposing is safe. The handler
        # must clear the locked decomposer session so the next tick spawns
        # a fresh manifest derived against the new body.
        gh = FakeGitHubClient()
        issue = make_issue(
            READY_DRIFT_ISSUE_NUMBER,
            label="ready",
            body="updated body",
        )
        gh.add_issue(issue)
        gh.seed_state(
            READY_DRIFT_ISSUE_NUMBER,
            user_content_hash=PRIOR_TICK_USER_CONTENT_HASH,
            decomposer_agent="claude",
            decomposer_session_id="old-sess",
            pickup_comment_id=PICKUP_COMMENT_ID,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Routed back to decomposing; the implementer must NOT have run
        # this tick.
        self.assertIn(
            (READY_DRIFT_ISSUE_NUMBER, LABEL_DECOMPOSING),
            gh.label_history,
        )
        state = gh.pinned_data(READY_DRIFT_ISSUE_NUMBER)
        # Session id dropped so the next tick spawns fresh, but the
        # recorded `decomposer_agent` spec is PRESERVED -- the
        # lock-on-first-spawn rule (see FullSpecPersistenceTest) means
        # a mid-flight config flip must not retarget the issue's
        # recorded role identity. The fresh spawn uses the recorded
        # spec via `_read_decomposer_session`.
        self.assertIsNone(state.get("decomposer_session_id"))
        self.assertEqual(state.get("decomposer_agent"), "claude")
        # New hash now persisted so the next decomposing tick sees a
        # stable baseline.
        self.assertNotEqual(
            state.get(KEY_USER_CONTENT_HASH),
            PRIOR_TICK_USER_CONTENT_HASH,
        )
        # A human-visible notice is posted.
        self.assertTrue(
            any(
                "issue content changed" in body
                for body in _comments_for_issue(
                    gh,
                    READY_DRIFT_ISSUE_NUMBER,
                )
            )
        )

    def test_unchanged_ready_does_not_route_back(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            STABLE_READY_ISSUE_NUMBER,
            label="ready",
            body="stable body",
        )
        gh.add_issue(issue)
        current = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            STABLE_READY_ISSUE_NUMBER,
            user_content_hash=current,
            pickup_comment_id=PICKUP_COMMENT_ID,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="done"),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # Falls through to the normal `ready` -> `implementing` flow.
        self.assertIn(
            (STABLE_READY_ISSUE_NUMBER, "implementing"),
            gh.label_history,
        )
        self.assertNotIn(
            (STABLE_READY_ISSUE_NUMBER, LABEL_DECOMPOSING),
            gh.label_history,
        )


class DecomposingHashChangeResetsSessionTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    def test_drops_session_and_spawns_fresh(
        self,
    ) -> None:
        # An issue parked at `decomposing awaiting_human` whose body the
        # human edited mid-thread should NOT resume the decomposer's
        # prior session (which would only see the human's reply, not the
        # new body). Drop the session id, clear the park flags, force a
        # fresh spawn against the new body.
        gh, issue = _decomposing_drift_fixture()

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess",
                last_message=(
                    'fits one\n\n```orchestrator-manifest\n{"decision": "single", "rationale": "small"}\n```'
                ),
            ),
            has_new_commits=False,
        )

        # The decomposer ran fresh (no resume of the stale session).
        mocks["run_agent"].assert_called_once()
        kwargs = mocks["run_agent"].call_args.kwargs
        self.assertIsNone(kwargs.get("resume_session_id"))
        state = gh.pinned_data(DECOMPOSING_DRIFT_ISSUE_NUMBER)
        # The new session id from the fresh spawn was persisted, not the
        # stale one.
        self.assertEqual(state.get("decomposer_session_id"), "new-sess")
        # Notice posted.
        self.assertIn(
            "issue content changed",
            "\n".join(_comments_for_issue(gh, DECOMPOSING_DRIFT_ISSUE_NUMBER)),
        )


class HandleBlockedHashDriftTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """Reviewer point 2: `blocked` must route back to `decomposing` per
    the spec so a later `_handle_ready` does not skip the re-decomposer
    when the edited body now needs splitting. Both parent (children
    listed as orphans) and child (no orphans) cases route."""

    def test_parent_with_children_routes_back(self) -> None:
        gh, parent = _blocked_parent_drift_fixture()

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Routed back to decomposing per spec ("Before validating: route
        # back to decomposing"). The next tick spawns a fresh decomposer
        # against the new body.
        self.assertIn(
            (BLOCKED_PARENT_NUMBER, LABEL_DECOMPOSING),
            gh.label_history,
        )
        state = gh.pinned_data(BLOCKED_PARENT_NUMBER)
        self.assertFalse(state.get("awaiting_human"))
        # Manifest state cleared so half-finished-recovery does not fire.
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("decomposer_session_id"))
        self.assertNotEqual(
            state.get(KEY_USER_CONTENT_HASH),
            STALE_USER_CONTENT_HASH,
        )
        # Notice explicitly lists the now-orphaned child so the operator
        # knows to close it manually if it no longer applies.
        notice = _comment_with_marker(
            gh,
            BLOCKED_PARENT_NUMBER,
            "re-running decomposer",
        )
        self.assertIn("#301", notice)
        self.assertIn("ORPHANED", notice)

    def test_child_waiting_routes_to_decomposing(self) -> None:
        # A blocked child waiting on a sibling. Without routing to
        # `decomposing`, `_handle_ready` would later see the matching
        # baseline (because we silently absorbed the new hash) and skip
        # the re-decomposer, even if the edited child now needs
        # splitting -- the explicit reviewer concern.
        gh = FakeGitHubClient()
        child = make_issue(
            BLOCKED_CHILD_NUMBER,
            label="blocked",
            body="updated child body",
        )
        gh.add_issue(child)
        gh.seed_state(
            BLOCKED_CHILD_NUMBER,
            parent_number=BLOCKED_CHILD_PARENT_NUMBER,
            user_content_hash=STALE_USER_CONTENT_HASH,
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        self.assertIn(
            (BLOCKED_CHILD_NUMBER, LABEL_DECOMPOSING),
            gh.label_history,
        )
        state = gh.pinned_data(BLOCKED_CHILD_NUMBER)
        self.assertNotEqual(
            state.get(KEY_USER_CONTENT_HASH),
            STALE_USER_CONTENT_HASH,
        )
        # Notice posted; no orphans for a child with no own children.
        notice = _comment_with_marker(
            gh,
            BLOCKED_CHILD_NUMBER,
            "re-running decomposer",
        )
        self.assertNotIn("ORPHANED", notice)


class HandleUmbrellaHashDriftTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """Reviewer point 2: `umbrella` parents never enter implementation,
    so a body edit cannot be picked up by any later stage's drift check.
    Route back to `decomposing` per spec so the new manifest is derived
    against the updated body; the previously-tracked children become
    orphans and are listed in the notice."""

    def test_edited_umbrella_routes_back_before_close(
        self,
    ) -> None:
        gh, umbrella = _umbrella_drift_fixture()

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, umbrella),
            run_agent=_agent(),
        )

        state = gh.pinned_data(UMBRELLA_NUMBER)
        # Routed back to decomposing per spec.
        self.assertIn(
            (UMBRELLA_NUMBER, LABEL_DECOMPOSING),
            gh.label_history,
        )
        # Crucially: did NOT close the umbrella to `done`.
        self.assertNotIn((UMBRELLA_NUMBER, LABEL_DONE), gh.label_history)
        self.assertFalse(umbrella.closed)
        # Manifest state cleared so half-finished-recovery does not fire
        # against the stale children list / umbrella flag.
        self.assertEqual(
            (state.get("children"), state.get("umbrella")),
            ([], None),
        )
        self.assertNotEqual(
            state.get(KEY_USER_CONTENT_HASH),
            STALE_USER_CONTENT_HASH,
        )
        # Orphans listed in the notice.
        notice = _comment_with_marker(
            gh,
            UMBRELLA_NUMBER,
            "re-running decomposer",
        )
        self.assertIn("#401", notice)
        self.assertIn("#402", notice)
        self.assertIn("ORPHANED", notice)
