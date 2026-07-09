# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

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


class HandleReadyRoutesBackOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_routes_ready_back_to_decomposing(self) -> None:
        # `ready` is reached only after a `single` decomposition decision
        # (no children created), so re-decomposing is safe. The handler
        # must clear the locked decomposer session so the next tick spawns
        # a fresh manifest derived against the new body.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="ready", body="updated body")
        gh.add_issue(issue)
        gh.seed_state(
            50,
            user_content_hash="stale-hash-from-prior-tick",
            decomposer_agent="claude",
            decomposer_session_id="old-sess",
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Routed back to decomposing; the implementer must NOT have run
        # this tick.
        self.assertIn((50, "decomposing"), gh.label_history)
        state = gh.pinned_data(50)
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
            state.get("user_content_hash"), "stale-hash-from-prior-tick",
        )
        # A human-visible notice is posted.
        self.assertTrue(any(
            "issue content changed" in body
            for _, body in gh.posted_comments
        ))

    def test_unchanged_ready_does_not_route_back(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(51, label="ready", body="stable body")
        gh.add_issue(issue)
        current = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            51,
            user_content_hash=current,
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # Falls through to the normal `ready` -> `implementing` flow.
        self.assertIn((51, "implementing"), gh.label_history)
        self.assertNotIn((51, "decomposing"), gh.label_history)


class HandleDecomposingResetsSessionOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_hash_drift_drops_session_and_spawns_fresh_decomposer(
        self,
    ) -> None:
        # An issue parked at `decomposing awaiting_human` whose body the
        # human edited mid-thread should NOT resume the decomposer's
        # prior session (which would only see the human's reply, not the
        # new body). Drop the session id, clear the park flags, force a
        # fresh spawn against the new body.
        gh = FakeGitHubClient()
        issue = make_issue(
            90, label="decomposing", body="updated decomposition input",
        )
        # A pre-existing human comment so the resume path would otherwise
        # consume it; we want to verify the hash branch wins.
        issue.comments.append(FakeComment(
            id=2000, body="please reconsider", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            90,
            user_content_hash="stale-hash",
            decomposer_agent="claude",
            decomposer_session_id="old-sess",
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=1500,
            pickup_comment_id=900,
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess",
                last_message=(
                    "fits one\n\n```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "small"}\n'
                    "```"
                ),
            ),
            has_new_commits=False,
        )

        # The decomposer ran fresh (no resume of the stale session).
        mocks["run_agent"].assert_called_once()
        kwargs = mocks["run_agent"].call_args.kwargs
        self.assertIsNone(kwargs.get("resume_session_id"))
        state = gh.pinned_data(90)
        # The new session id from the fresh spawn was persisted, not the
        # stale one.
        self.assertEqual(state.get("decomposer_session_id"), "new-sess")
        # Notice posted.
        self.assertTrue(any(
            "issue content changed" in body
            for _, body in gh.posted_comments
        ))


class HandleBlockedHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `blocked` must route back to `decomposing` per
    the spec so a later `_handle_ready` does not skip the re-decomposer
    when the edited body now needs splitting. Both parent (children
    listed as orphans) and child (no orphans) cases route."""

    def test_parent_with_children_routes_to_decomposing(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(300, label="blocked", body="updated parent body")
        gh.add_issue(parent)
        # An in-flight child -- routing the parent orphans it on the
        # GitHub side; the notice must call this out so the operator can
        # close any obsolete children manually.
        child = make_issue(301, label="implementing")
        gh.add_issue(child)
        gh.seed_state(
            300,
            children=[301],
            decomposer_session_id="old-sess",
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Routed back to decomposing per spec ("Before validating: route
        # back to decomposing"). The next tick spawns a fresh decomposer
        # against the new body.
        self.assertIn((300, "decomposing"), gh.label_history)
        state = gh.pinned_data(300)
        self.assertFalse(state.get("awaiting_human"))
        # Manifest state cleared so half-finished-recovery does not fire.
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("decomposer_session_id"))
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        # Notice explicitly lists the now-orphaned child so the operator
        # knows to close it manually if it no longer applies.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
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
        child = make_issue(310, label="blocked", body="updated child body")
        gh.add_issue(child)
        gh.seed_state(
            310,
            parent_number=309,
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        self.assertIn((310, "decomposing"), gh.label_history)
        state = gh.pinned_data(310)
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        # Notice posted; no orphans for a child with no own children.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertNotIn("ORPHANED", notice)


class HandleUmbrellaHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `umbrella` parents never enter implementation,
    so a body edit cannot be picked up by any later stage's drift check.
    Route back to `decomposing` per spec so the new manifest is derived
    against the updated body; the previously-tracked children become
    orphans and are listed in the notice."""

    def test_edited_umbrella_routes_to_decomposing_before_closing(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        umbrella = make_issue(
            400, label="umbrella", body="updated umbrella body",
        )
        gh.add_issue(umbrella)
        # Children all done -- without the drift route, the umbrella
        # would close to `done` against the stale manifest on this
        # very tick.
        c1 = make_issue(401, label="done")
        c2 = make_issue(402, label="done")
        gh.add_issue(c1)
        gh.add_issue(c2)
        gh.seed_state(
            400,
            children=[401, 402],
            umbrella=True,
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, umbrella),
            run_agent=_agent(),
        )

        state = gh.pinned_data(400)
        # Routed back to decomposing per spec.
        self.assertIn((400, "decomposing"), gh.label_history)
        # Crucially: did NOT close the umbrella to `done`.
        self.assertNotIn((400, "done"), gh.label_history)
        self.assertFalse(umbrella.closed)
        # Manifest state cleared so half-finished-recovery does not fire
        # against the stale children list / umbrella flag.
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("umbrella"))
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        # Orphans listed in the notice.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#401", notice)
        self.assertIn("#402", notice)
        self.assertIn("ORPHANED", notice)
