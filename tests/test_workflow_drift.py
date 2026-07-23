# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""User-content hash and drift detection tests."""
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests import workflow_drift_test_support as support


class ComputeUserContentHashTest(unittest.TestCase):
    """The hash must include user-visible content (title, body, human
    comments) and exclude orchestrator-authored content (pinned-state
    marker comments, anything in `orchestrator_comment_ids`). Author-login
    matching is intentionally avoided because the orchestrator PAT is
    often shared with a human reviewer's GitHub account."""

    def test_hash_changes_when_body_changes(self) -> None:
        issue_a = support.make_issue(1, body="old body")
        issue_b = support.make_issue(1, body=support.NEW_BODY)
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_a, set()),
            workflow._compute_user_content_hash(issue_b, set()),
        )

    def test_hash_changes_when_title_changes(self) -> None:
        issue_a = support.make_issue(1, title="old", body="b")
        issue_b = support.make_issue(1, title="new", body="b")
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_a, set()),
            workflow._compute_user_content_hash(issue_b, set()),
        )

    def test_orchestrator_comments_filtered_by_id(self) -> None:
        # A human comment with the same body as a bot comment must still
        # affect the hash; only the recorded bot id is filtered.
        human = support.FakeComment(
            id=100,
            body="please retry",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        bot = support.FakeComment(
            id=support._BOT_COMMENT_ID,
            body="picking this up",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        issue_with_human = support.make_issue(1, comments=[human])
        issue_with_both = support.make_issue(1, comments=[human, bot])
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_human,
                {support._BOT_COMMENT_ID},
            ),
            workflow._compute_user_content_hash(
                issue_with_both,
                {support._BOT_COMMENT_ID},
            ),
        )
        # Without filtering the bot comment, the hash differs.
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_with_human, set()),
            workflow._compute_user_content_hash(issue_with_both, set()),
        )

    def test_state_marker_filtered_by_marker(self) -> None:
        pinned = support.FakeComment(
            id=support._PINNED_COMMENT_ID,
            body="<!--orchestrator-state {\"k\": 1}-->",
        )
        issue = support.make_issue(1)
        issue_with_pinned = support.make_issue(1, comments=[pinned])
        # Pinned-state comment id is NOT in orchestrator_ids but its marker
        # body causes it to be filtered.
        self.assertEqual(
            workflow._compute_user_content_hash(issue, set()),
            workflow._compute_user_content_hash(issue_with_pinned, set()),
        )

    def test_bare_continue_ignored_guidance_counts(
        self,
    ) -> None:
        # A bare `/orchestrator continue` is an operator command, not
        # requirements content: it must not shift the hash (else it routes
        # through generic drift handling instead of the intentional
        # session-limit retry, issue #729). The same command carrying real
        # guidance is NOT bare, so it still shifts the hash.
        issue = support.make_issue(1)
        bare = support.FakeComment(
            id=100, body=support.CONTINUE_COMMAND, user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        guided = support.FakeComment(
            id=support._GUIDED_CONTINUE_COMMENT_ID,
            body="/orchestrator continue\nalso rename the flag",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        self.assertEqual(
            workflow._compute_user_content_hash(issue, set()),
            workflow._compute_user_content_hash(
                support.make_issue(1, comments=[bare]), set()
            ),
        )
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue, set()),
            workflow._compute_user_content_hash(
                support.make_issue(1, comments=[guided]), set()
            ),
        )


class ContinueCommandActionTest(unittest.TestCase):
    """`_continue_command_action` classifies an operator `/orchestrator
    continue` on a parked `implementing` / `documenting` issue. Retryable
    session-failure parks with a content-free nudge retry; parks needing a
    real answer refuse; anything else (no command, or a command carrying
    guidance) passes through to the normal resume / drift path."""

    def test_retryable_park_bare_continue_retries(self) -> None:
        for reason in (support.PARK_AGENT_SILENT, "agent_timeout"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    workflow._continue_command_action(
                        [support._continue_comment(support.CONTINUE_COMMAND)], reason,
                    ),
                    "retry",
                )

    def test_non_retryable_park_bare_continue_refuses(self) -> None:
        for reason in (None, "dirty_worktree", "diverged_branch"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    workflow._continue_command_action(
                        [support._continue_comment(support.CONTINUE_COMMAND)], reason,
                    ),
                    "refuse",
                )

    def test_command_with_guidance_passes_through(self) -> None:
        # The command is present but the comment also carries guidance, so
        # the normal resume/drift path should feed that guidance to the dev.
        self.assertEqual(
            workflow._continue_command_action(
                [support._continue_comment("/orchestrator continue\nrename the flag")],
                support.PARK_AGENT_SILENT,
            ),
            "passthrough",
        )

    def test_no_command_passes_through(self) -> None:
        self.assertEqual(
            workflow._continue_command_action(
                [support._continue_comment("just a normal reply")], support.PARK_AGENT_SILENT,
            ),
            "passthrough",
        )


class DetectUserContentChangeTest(unittest.TestCase):
    def test_first_call_persists_and_returns_none(self) -> None:
        # The first encounter has no baseline; we record the current value
        # AND write pinned state immediately so a parked/idle tick can't
        # silently absorb a later edit as the new baseline.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1)
        gh.add_issue(issue)
        state = gh.read_pinned_state(issue)
        before = gh.write_state_calls
        detected_hash = workflow._detect_user_content_change(gh, issue, state)
        self.assertIsNone(detected_hash)
        self.assertEqual(
            state.get(support.KEY_USER_CONTENT_HASH),
            workflow._compute_user_content_hash(issue, set()),
        )
        # Durably written so a later edit after an early-return tick is
        # correctly classified as drift, not absorbed as the new baseline.
        self.assertEqual(gh.write_state_calls, before + 1)
        self.assertEqual(
            gh.pinned_data(1).get(support.KEY_USER_CONTENT_HASH),
            state.get(support.KEY_USER_CONTENT_HASH),
        )

    def test_unchanged_returns_none(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(1)
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

    def test_body_change_returns_hash_without_persist(
        self,
    ) -> None:
        context = support._content_change_case("old", support.NEW_BODY)
        detected_hash = workflow._detect_user_content_change(
            context.github,
            context.issue,
            context.state,
        )
        self.assertEqual(detected_hash, context.current_hash)
        self.assertNotEqual(detected_hash, context.prior_hash)
        # The helper does NOT auto-persist on a real change; the caller
        # decides whether to act and persist (so the routing branches can
        # use the comparison without committing to a state write).
        self.assertEqual(
            context.github.write_state_calls,
            context.before_writes,
        )
        self.assertEqual(
            context.state.get(support.KEY_USER_CONTENT_HASH),
            context.prior_hash,
        )

    def test_legacy_bare_continue_baseline_absorbs(
        self,
    ) -> None:
        # A baseline written by the pre-#729 algorithm counted a bare
        # `/orchestrator continue` comment. After deploy the new algorithm
        # excludes it, so the hashes differ even though the requirements did
        # not change. `_detect_user_content_change` must recognize the stored
        # baseline as the legacy hash and absorb it (persist the new baseline,
        # report no drift) rather than firing one false "issue body changed".
        continue_comment = support.FakeComment(
            id=100, body=support.CONTINUE_COMMAND, user=support.FakeUser("dave"),
        )
        context = support._content_change_case(
            "",
            "",
            comments=(continue_comment,),
            include_bare_continue=True,
        )
        self.assertNotEqual(
            context.prior_hash,
            context.current_hash,
        )

        detected_hash = workflow._detect_user_content_change(
            context.github,
            context.issue,
            context.state,
        )

        # No drift reported; the baseline is normalized to the new algorithm
        # and durably persisted so the next tick is stable.
        self.assertIsNone(detected_hash)
        self.assertEqual(
            context.state.get(support.KEY_USER_CONTENT_HASH),
            context.current_hash,
        )
        self.assertEqual(
            context.github.pinned_data(1).get(support.KEY_USER_CONTENT_HASH),
            context.current_hash,
        )

    def test_real_edit_with_bare_continue_drifts(self) -> None:
        # The legacy-normalization path must not swallow a genuine edit: when
        # the body actually changed AND a bare continue is present, the legacy
        # hash (old algorithm over the NEW content) still differs from the
        # stored baseline, so drift is reported.
        continue_comment = support.FakeComment(
            id=100, body=support.CONTINUE_COMMAND, user=support.FakeUser("dave"),
        )
        context = support._content_change_case(
            "old",
            support.NEW_BODY,
            comments=(continue_comment,),
            include_bare_continue=True,
        )
        detected_hash = workflow._detect_user_content_change(
            context.github,
            context.issue,
            context.state,
        )

        self.assertEqual(detected_hash, context.current_hash)
        self.assertIsNotNone(detected_hash)
