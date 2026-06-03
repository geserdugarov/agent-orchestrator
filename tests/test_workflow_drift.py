# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class ComputeUserContentHashTest(unittest.TestCase):
    """The hash must include user-visible content (title, body, human
    comments) and exclude orchestrator-authored content (pinned-state
    marker comments, anything in `orchestrator_comment_ids`). Author-login
    matching is intentionally avoided because the orchestrator PAT is
    often shared with a human reviewer's GitHub account."""

    def test_hash_changes_when_body_changes(self) -> None:
        issue_a = make_issue(1, body="old body")
        issue_b = make_issue(1, body="new body")
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_a, set()),
            workflow._compute_user_content_hash(issue_b, set()),
        )

    def test_hash_changes_when_title_changes(self) -> None:
        issue_a = make_issue(1, title="old", body="b")
        issue_b = make_issue(1, title="new", body="b")
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_a, set()),
            workflow._compute_user_content_hash(issue_b, set()),
        )

    def test_orchestrator_comments_filtered_by_id(self) -> None:
        # A human comment with the same body as a bot comment must still
        # affect the hash; only the recorded bot id is filtered.
        human = FakeComment(id=100, body="please retry", user=FakeUser("alice"))
        bot = FakeComment(id=200, body="picking this up", user=FakeUser("alice"))
        issue_with_human = make_issue(1, comments=[human])
        issue_with_both = make_issue(1, comments=[human, bot])
        self.assertEqual(
            workflow._compute_user_content_hash(issue_with_human, {200}),
            workflow._compute_user_content_hash(issue_with_both, {200}),
        )
        # Without filtering 200, the hash differs.
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_with_human, set()),
            workflow._compute_user_content_hash(issue_with_both, set()),
        )

    def test_pinned_state_marker_comment_is_filtered_by_marker(self) -> None:
        pinned = FakeComment(
            id=300, body="<!--orchestrator-state {\"k\": 1}-->",
        )
        issue = make_issue(1)
        issue_with_pinned = make_issue(1, comments=[pinned])
        # Pinned-state comment id is NOT in orchestrator_ids but its marker
        # body causes it to be filtered.
        self.assertEqual(
            workflow._compute_user_content_hash(issue, set()),
            workflow._compute_user_content_hash(issue_with_pinned, set()),
        )


class DetectUserContentChangeTest(unittest.TestCase):
    def test_first_call_persists_durably_and_returns_none(self) -> None:
        # The first encounter has no baseline; we record the current value
        # AND write pinned state immediately so a parked/idle tick can't
        # silently absorb a later edit as the new baseline.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        state = gh.read_pinned_state(issue)
        before = gh.write_state_calls
        result = workflow._detect_user_content_change(gh, issue, state)
        self.assertIsNone(result)
        self.assertEqual(
            state.get("user_content_hash"),
            workflow._compute_user_content_hash(issue, set()),
        )
        # Durably written so a later edit after an early-return tick is
        # correctly classified as drift, not absorbed as the new baseline.
        self.assertEqual(gh.write_state_calls, before + 1)
        self.assertEqual(
            gh.pinned_data(1).get("user_content_hash"),
            state.get("user_content_hash"),
        )

    def test_unchanged_returns_none(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        prior_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(1, user_content_hash=prior_hash)
        state = gh.read_pinned_state(issue)
        before = gh.write_state_calls
        self.assertIsNone(
            workflow._detect_user_content_change(gh, issue, state)
        )
        # No extra write when the baseline already matches.
        self.assertEqual(gh.write_state_calls, before)

    def test_body_change_returns_new_hash_without_auto_persist(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue_a = make_issue(1, body="old")
        prior = workflow._compute_user_content_hash(issue_a, set())
        issue_b = make_issue(1, body="new body")
        gh.add_issue(issue_b)
        gh.seed_state(1, user_content_hash=prior)
        state = gh.read_pinned_state(issue_b)
        before = gh.write_state_calls
        result = workflow._detect_user_content_change(gh, issue_b, state)
        self.assertEqual(
            result, workflow._compute_user_content_hash(issue_b, set())
        )
        self.assertNotEqual(result, prior)
        # The helper does NOT auto-persist on a real change; the caller
        # decides whether to act and persist (so the routing branches can
        # use the comparison without committing to a state write).
        self.assertEqual(gh.write_state_calls, before)
        self.assertEqual(state.get("user_content_hash"), prior)


class HandlePickupInitializesUserContentHashTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_pickup_with_decompose_off_seeds_hash(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="q"),
                has_new_commits=False,
            )

        data = gh.pinned_data(1)
        self.assertIn("user_content_hash", data)
        # Hash filters the pickup comment by id (it has been recorded in
        # `orchestrator_comment_ids`), so it should match a re-computation
        # over the same set.
        orch_ids = set(data.get("orchestrator_comment_ids") or [])
        self.assertEqual(
            data["user_content_hash"],
            workflow._compute_user_content_hash(issue, orch_ids),
        )

    def test_pickup_with_decompose_on_seeds_hash(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess",
                    last_message=(
                        "fits one\n\n```orchestrator-manifest\n"
                        '{"decision": "single", "rationale": "small"}\n'
                        "```"
                    ),
                ),
            )

        data = gh.pinned_data(2)
        self.assertIn("user_content_hash", data)


class UserContentChangePromptIncludesCommentsTest(unittest.TestCase):
    """A drift triggered by a NEW human comment (not a body edit) must
    surface that comment to the dev. Quoting only title/body would leave
    the dev unaware of the acceptance criterion the human just posted."""

    def test_recent_comments_are_quoted_in_resume_prompt(self) -> None:
        issue = make_issue(1, title="t", body="b")
        issue.comments.append(FakeComment(
            id=500, body="new acceptance criterion: handle empty input",
            user=FakeUser("alice"),
        ))
        comments_text = workflow._recent_comments_text(issue)
        prompt = workflow._build_user_content_change_prompt(
            issue, comments_text,
        )
        self.assertIn("new acceptance criterion", prompt)
        self.assertIn("Conversation so far", prompt)
        self.assertIn("Updated issue body", prompt)


class FirstTimeHashSeedingIsDurableTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 3: `_detect_user_content_change` must persist the
    first-time baseline via `gh.write_pinned_state` immediately, so a
    later edit after a parked/idle tick is not silently absorbed as the
    new baseline."""

    def test_validating_awaiting_human_no_reply_persists_baseline(
        self,
    ) -> None:
        # Legacy state (no `user_content_hash`) parked on awaiting_human
        # with no new comments. `_handle_validating`'s awaiting-human
        # path returns without writing state on a real no-reply tick;
        # the durability fix in `_detect_user_content_change` must still
        # have written the baseline by then.
        gh = FakeGitHubClient()
        issue = make_issue(100, label="validating", body="initial body")
        gh.add_issue(issue)
        pr = FakePR(number=1000, head_branch="orchestrator/issue-100")
        gh.add_pr(pr)
        gh.seed_state(
            100,
            pr_number=pr.number,
            awaiting_human=True,
            last_action_comment_id=500,
            review_round=1,
        )

        # Tick: no new comments and no hash baseline. Park branch
        # returns early without writing state.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Baseline durably persisted by the first-call branch in
        # `_detect_user_content_change`.
        data = gh.pinned_data(100)
        self.assertIsNotNone(data.get("user_content_hash"))

    def test_blocked_child_no_op_persists_baseline(self) -> None:
        # A `blocked` child waiting on a sibling is a per-tick no-op.
        # Without the durability fix, a later edit during the wait would
        # silently become the new baseline because the no-op branch
        # returns without `write_pinned_state`.
        gh = FakeGitHubClient()
        child = make_issue(200, label="blocked", body="child body")
        gh.add_issue(child)
        gh.seed_state(200, parent_number=199)

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        data = gh.pinned_data(200)
        self.assertIsNotNone(data.get("user_content_hash"))


class NoCommitAckDoesNotParkTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 4: a harmless clarification edit can elicit a
    no-commit reply from the dev ('existing work satisfies'). The
    validating / in_review / resolving_conflict drift paths must treat
    that as an ack rather than parking awaiting_human."""

    def test_validating_ack_without_commit_does_not_park(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(600, label="validating", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=6000, head_branch="orchestrator/issue-600")
        gh.add_pr(pr)
        gh.seed_state(
            600,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            review_round=1,
            branch="orchestrator/issue-600",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Reviewed the clarified body.\n\n"
                    "ACK: existing commits already cover the clarified body"
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same-sha", "same-sha"],
        )

        data = gh.pinned_data(600)
        # Crucial: must NOT park as a question.
        self.assertFalse(data.get("awaiting_human"))
        # Dev's ACK justification was posted on the issue as an FYI.
        self.assertTrue(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))

    def test_in_review_ack_without_commit_bounces_to_validating(
        self,
    ) -> None:
        # A no-commit "ack" reply from the dev on an in_review drift
        # MUST bounce DIRECTLY back to `validating` (same destination
        # as the pushed-fix exit; docs do not run on the drift exit,
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff). `review_round`
        # resets so the validating cap counts fresh rounds.
        gh = FakeGitHubClient()
        issue = make_issue(700, label="in_review", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=7000, head_branch="orchestrator/issue-700")
        gh.add_pr(pr)
        gh.seed_state(
            700,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-700",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="ACK: no additional code change needed",
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same", "same"],
        )

        data = gh.pinned_data(700)
        # Must NOT park (the dev acknowledged, not asked a question).
        self.assertFalse(data.get("awaiting_human"))
        # MUST bounce directly to validating (no documenting hop) so
        # the reviewer re-evaluates against the updated body.
        self.assertIn((700, "validating"), gh.label_history)
        # And NOT through documenting -- no commit landed.
        self.assertNotIn((700, "documenting"), gh.label_history)
        # review_round reset so the validating cap counts fresh rounds.
        self.assertEqual(data.get("review_round"), 0)
        # Dev's reply still posted on the issue as an FYI.
        self.assertTrue(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))


class DriftMarksCommentsConsumedTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: the drift paths feed the dev session the full
    issue thread via `_recent_comments_text`, so `last_action_comment_id`
    must advance past every visible comment. Otherwise the next
    validating->in_review handoff's `_seed_watermark_past_self` stops at
    the same human comment and replays it as fresh PR feedback,
    triggering a duplicate dev resume."""

    def test_validating_drift_bumps_last_action_past_human_comment(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(900, label="validating", body="new body")
        # Pre-existing human comment with a high id -- representing the
        # comment that arrived at the same time as the body edit.
        human = FakeComment(
            id=5000, body="add this acceptance criterion",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=9000, head_branch="orchestrator/issue-900")
        gh.add_pr(pr)
        gh.seed_state(
            900,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            review_round=1,
            branch="orchestrator/issue-900",
            last_action_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="fixed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after"],
        )

        data = gh.pinned_data(900)
        # last_action_comment_id advanced past the human comment so the
        # eventual handoff to in_review does not classify it as fresh
        # feedback.
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 5000,
        )

    def test_in_review_fresh_human_comment_routes_to_fixing_not_drift(
        self,
    ) -> None:
        # Regression for the reviewer's bug: a fresh issue-thread human
        # comment used to trip `user_content_hash` (which covers comments
        # too) and the drift path would resume the dev + bounce to
        # `validating` instead of the contracted route to `fixing`. With
        # the in_review handler scanning fresh feedback BEFORE the drift
        # check, the issue-thread comment now routes to `fixing` and the
        # hash is recomputed so the drift path does not double-fire on the
        # same comment changes next tick.
        gh = FakeGitHubClient()
        issue = make_issue(910, label="in_review", body="new body")
        human = FakeComment(
            id=6000, body="please also handle X",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=9100, head_branch="orchestrator/issue-910")
        gh.add_pr(pr)
        gh.seed_state(
            910,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-910",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No dev spawn, no bounce to `validating`: the fixing route owns
        # this signal.
        mocks["run_agent"].assert_not_called()
        self.assertIn((910, "fixing"), gh.label_history)
        self.assertNotIn((910, "validating"), gh.label_history)
        data = gh.pinned_data(910)
        # The triggering comment is bookmarked for the fixing handler.
        self.assertEqual(data.get("pending_fix_issue_max_id"), 6000)
        # Hash is updated so the drift check does not re-fire on the
        # same comment change after the fixing handler (or an operator
        # relabel) bounces the issue back to `in_review`.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Watermark is deliberately left at the route-time value so the
        # fixing handler can read the triggering comment to build its
        # dev-resume prompt (the bookmark above tells it where to start).
        # The fixing handler advances this watermark itself once the
        # consumed feedback has been fed to the dev.
        self.assertEqual(data.get("pr_last_comment_id"), 0)

    def test_implementing_drift_bumps_last_action_past_human_comment(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(920, label="implementing", body="new body")
        human = FakeComment(
            id=7000, body="here are more requirements",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        gh.seed_state(
            920,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            awaiting_human=True,
            last_action_comment_id=100,
            branch="orchestrator/issue-920",
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="implemented"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before-resume", "after-resume"],
        )

        data = gh.pinned_data(920)
        # The dev's commit goes through `_on_commits` which flips to
        # validating; the validating->in_review handoff later reads
        # last_action_comment_id, so we must have bumped past 7000.
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 7000,
        )

    def test_resolving_conflict_drift_bumps_last_action(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            930, label="resolving_conflict", body="new body",
        )
        human = FakeComment(
            id=8000, body="more context", user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=9300, head_branch="orchestrator/issue-930")
        gh.add_pr(pr)
        gh.seed_state(
            930,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            conflict_round=0,
            branch="orchestrator/issue-930",
            last_action_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="resolved"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after", "after"],
        )

        data = gh.pinned_data(930)
        # After the pushed resolution flips to validating, the
        # subsequent handoff back to in_review must not replay the human
        # comment that arrived during conflict resolution.
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 8000,
        )


class OrchCommentMarkerSurvivesIdCapTest(unittest.TestCase):
    """Reviewer point 3: `orchestrator_comment_ids` is capped, but the
    hash scans every comment. Once an old orchestrator-comment id is
    evicted from the cap, an id-only filter would start including the
    bot comment in the hash and trigger false drift each tick. The body
    marker (`_ORCH_COMMENT_MARKER`) must keep the hash stable."""

    def test_marker_excludes_orchestrator_comment_even_when_id_unknown(
        self,
    ) -> None:
        # Simulate an orchestrator comment whose id has been evicted
        # from the bounded cap. Its body still carries the marker
        # (because every orchestrator comment is posted with it), so
        # the hash filter must drop it.
        bot_body = "picking this up\n\n" + workflow._ORCH_COMMENT_MARKER
        bot = FakeComment(id=12345, body=bot_body, user=FakeUser("alice"))
        human = FakeComment(
            id=12346, body="please reconsider", user=FakeUser("alice"),
        )
        issue_with_just_human = make_issue(1, comments=[human])
        issue_with_both = make_issue(1, comments=[bot, human])
        # `orchestrator_ids` is EMPTY (the id was evicted from the cap),
        # but the hash must still match because the marker identifies
        # the bot comment.
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_just_human, set()
            ),
            workflow._compute_user_content_hash(
                issue_with_both, set()
            ),
        )

    def test_marker_is_appended_by_post_helpers(self) -> None:
        # Every orchestrator-posted comment must carry the marker so
        # the hash filter survives id-cap eviction.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        state = workflow.PinnedState()
        workflow._post_issue_comment(gh, issue, state, "hello")
        # The body actually written to the issue carries the marker.
        last_body = issue.comments[-1].body
        self.assertIn(workflow._ORCH_COMMENT_MARKER, last_body)
        # And it starts with the original body text.
        self.assertTrue(last_body.startswith("hello"))

    def test_marker_is_idempotent_on_double_wrap(self) -> None:
        # Defensive: a caller that already passes a body containing the
        # marker (e.g. a future helper forwards a pre-built body) must
        # not get the marker appended twice -- two markers in one body
        # is harmless but ugly, and an idempotent wrap also keeps
        # `_with_orch_marker` safe to call from helper chains.
        marked = workflow._with_orch_marker("hi")
        twice = workflow._with_orch_marker(marked)
        self.assertEqual(marked, twice)
        self.assertEqual(twice.count(workflow._ORCH_COMMENT_MARKER), 1)


class HashFiltersBotUsersTest(unittest.TestCase):
    """Reviewer point 2: third-party Bot/App accounts (Dependabot,
    Renovate, CI bots) post comments structurally on long-lived issues.
    The hash must filter them by GitHub's `user.type == "Bot"` flag so
    a periodic bot comment doesn't re-trigger drift on every tick it
    posts. Login matching is intentionally avoided because the
    orchestrator PAT may be shared with a human reviewer's account."""

    def test_bot_authored_comment_is_filtered(self) -> None:
        # A Dependabot-style comment must NOT affect the hash even
        # though its body is unique and its id is not tracked.
        human = FakeComment(
            id=900, body="real human comment", user=FakeUser("alice"),
        )
        bot_comment = FakeComment(
            id=901,
            body="Bumps `requests` from 2.31.0 to 2.32.0",
            user=FakeUser("dependabot[bot]", type="Bot"),
        )
        issue_with_just_human = make_issue(1, comments=[human])
        issue_with_bot = make_issue(1, comments=[human, bot_comment])
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_just_human, set()
            ),
            workflow._compute_user_content_hash(
                issue_with_bot, set()
            ),
        )

    def test_user_type_human_still_contributes(self) -> None:
        # A regular human user's `type == "User"` must NOT be filtered.
        comment = FakeComment(
            id=910,
            body="adds an acceptance criterion",
            user=FakeUser("alice", type="User"),
        )
        empty = make_issue(1)
        with_human = make_issue(1, comments=[comment])
        self.assertNotEqual(
            workflow._compute_user_content_hash(empty, set()),
            workflow._compute_user_content_hash(with_human, set()),
        )


class DriftAckRequiresExplicitMarkerTest(unittest.TestCase):
    """Reviewer point: a generic non-empty no-commit response is OFTEN a
    clarification question, not an ack. Only an explicit `ACK: ...`
    marker should be treated as acknowledgement; everything else parks
    awaiting human via `_on_question`."""

    def test_explicit_ack_marker_extracts_reason(self) -> None:
        msg = (
            "I reviewed the change.\n\n"
            "ACK: existing tests already cover the new requirement"
        )
        self.assertEqual(
            workflow._drift_ack_reason(msg),
            "existing tests already cover the new requirement",
        )

    def test_ack_is_case_insensitive_and_last_wins(self) -> None:
        # Case insensitive (mirrors VERDICT parsing) and the LAST marker
        # wins so a sample/template `ACK:` quoted earlier in the message
        # doesn't override the agent's real concluding marker.
        msg = (
            "I considered ack: stale-template-text but on re-reading\n\n"
            "ack: real final justification"
        )
        self.assertEqual(
            workflow._drift_ack_reason(msg),
            "real final justification",
        )

    def test_no_marker_returns_none(self) -> None:
        # Generic "satisfied" prose without the marker is NOT an ack.
        # `_post_user_content_change_result` parks via `_on_question`
        # on this branch so a real question isn't swallowed.
        msg = "Existing code already covers this; no change needed."
        self.assertIsNone(workflow._drift_ack_reason(msg))

    def test_clarification_question_returns_none(self) -> None:
        msg = "Should I also handle the empty-input case?"
        self.assertIsNone(workflow._drift_ack_reason(msg))


class DriftNonAckResponseParksTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """A non-empty no-commit response WITHOUT the `ACK:` marker -- e.g.
    a clarification question -- must park awaiting human, not silently
    advance the workflow with a misleading "satisfies" comment."""

    def test_validating_drift_clarification_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(601, label="validating", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=6001, head_branch="orchestrator/issue-601")
        gh.add_pr(pr)
        gh.seed_state(
            601,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            review_round=1,
            branch="orchestrator/issue-601",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Should the empty-input case also raise, or return "
                    "an empty list? Need clarification."
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same-sha", "same-sha"],
        )

        data = gh.pinned_data(601)
        # Must park awaiting human so the real question isn't lost.
        self.assertTrue(data.get("awaiting_human"))
        # Must NOT have posted the misleading "satisfies" comment.
        self.assertFalse(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))
        # The question text was surfaced via `_on_question`.
        self.assertTrue(any(
            "Should the empty-input case" in body
            for _, body in gh.posted_comments
        ))

    def test_in_review_drift_clarification_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701, label="in_review", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=7001, head_branch="orchestrator/issue-701")
        gh.add_pr(pr)
        gh.seed_state(
            701,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-701",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Does the updated body imply I should also rename "
                    "`old_fn`? Please confirm."
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same", "same"],
        )

        data = gh.pinned_data(701)
        # Park flagged.
        self.assertTrue(data.get("awaiting_human"))
        # NOT bounced to validating: the dev didn't ack OR commit, so
        # the in_review label is preserved and the human resolves the
        # question.
        self.assertNotIn((701, "validating"), gh.label_history)
        # Misleading "satisfies" comment NOT posted.
        self.assertFalse(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))

    def test_implementing_drift_clarification_question_parks(
        self,
    ) -> None:
        # The implementing-stage inline drift handler shares the same
        # contract: non-empty + no-commit + no ACK -> park as question.
        gh = FakeGitHubClient()
        issue = make_issue(
            602, label="implementing", body="updated requirements",
        )
        gh.add_issue(issue)
        gh.seed_state(
            602,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=False,
            branch="orchestrator/issue-602",
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "I'd like to clarify: should the schema migration "
                    "run forward-only or also support rollback?"
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["sha-before", "sha-before"],
        )

        data = gh.pinned_data(602)
        self.assertTrue(data.get("awaiting_human"))
        self.assertFalse(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))
        # The dev's question was surfaced.
        self.assertTrue(any(
            "schema migration" in body
            for _, body in gh.posted_comments
        ))



if __name__ == "__main__":
    unittest.main()
