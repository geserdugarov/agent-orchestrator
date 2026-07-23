# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing anchor behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

ADVANCED_PR_COMMENT_WATERMARK = support.ADVANCED_PR_COMMENT_WATERMARK
ALLOWED_AUTHOR = support.ALLOWED_AUTHOR
ALLOWED_AUTHORS_CONFIG = support.ALLOWED_AUTHORS_CONFIG
BATCH_INLINE_IDS = support.BATCH_INLINE_IDS
BATCH_INLINE_SECOND_ID = support.BATCH_INLINE_SECOND_ID
BATCH_ISSUE_IDS = support.BATCH_ISSUE_IDS
BATCH_PR_CONVERSATION_ID = support.BATCH_PR_CONVERSATION_ID
BATCH_SUMMARY_ID = support.BATCH_SUMMARY_ID
BATCH_SUMMARY_IDS = support.BATCH_SUMMARY_IDS
BRANCH = support.BRANCH
FIXING = support.FIXING
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakePR = support.FakePR
FakePRRef = support.FakePRRef
FakeUser = support.FakeUser
ID_LIST_KEY = support.ID_LIST_KEY
ISSUE = support.ISSUE
MAX_ID_KEY = support.MAX_ID_KEY
MISSING_ANCHOR_ID = support.MISSING_ANCHOR_ID
ORCHESTRATOR = support.ORCHESTRATOR
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_AT_TS = support.PENDING_FIX_AT_TS
PENDING_FIX_ISSUE_IDS = support.PENDING_FIX_ISSUE_IDS
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PENDING_FIX_REVIEWER_COMMENT_ID = support.PENDING_FIX_REVIEWER_COMMENT_ID
PENDING_FIX_REVIEW_IDS = support.PENDING_FIX_REVIEW_IDS
PENDING_FIX_REVIEW_MAX_ID = support.PENDING_FIX_REVIEW_MAX_ID
PENDING_FIX_REVIEW_SUMMARY_IDS = support.PENDING_FIX_REVIEW_SUMMARY_IDS
PENDING_FIX_REVIEW_SUMMARY_MAX_ID = support.PENDING_FIX_REVIEW_SUMMARY_MAX_ID
PR_HEAD_SHA = support.PR_HEAD_SHA
PR_NUMBER = support.PR_NUMBER
_clear_pending_fix_bookmarks = support._clear_pending_fix_bookmarks
_pending_fix_id_set = support._pending_fix_id_set
_reconstruct_pending_fix_batch = support._reconstruct_pending_fix_batch
config = support.config
make_issue = support.make_issue
patch = support.patch
workflow = support.workflow


class _ReviewerAnchorFixtureMixin:
    def _pr_with_reviewer_anchor(
        self,
        *,
        anchor_id: int = BATCH_PR_CONVERSATION_ID,
    ):
        # Validating-route shape: no `pending_fix_*_ids`, no `pending_fix_at`,
        # just the orchestrator-authored reviewer-feedback PR comment whose id
        # `_handle_validating_changes_requested` recorded. It carries the hidden
        # orchestrator marker like the real post, so a rescan would filter it --
        # only the anchor id re-surfaces it for the replay.
        issue = make_issue(ISSUE, label=FIXING)
        reviewer = FakeComment(
            id=anchor_id,
            body=(
                ":eyes: codex review (round 1/3) requested changes:\n\n"
                "please fix the docstring ordering\n\n<!--orchestrator-comment-->"
            ),
            user=FakeUser(ORCHESTRATOR),
        )
        pr = FakePR(
            number=PR_NUMBER,
            head_branch=BRANCH,
            head=FakePRRef(sha=PR_HEAD_SHA),
            issue_comments=[reviewer],
        )
        gh = FakeGitHubClient()
        gh.add_issue(issue)
        gh.add_pr(pr)
        return gh, issue, pr


class ReviewerAnchorReconstructionTest(
    unittest.TestCase,
    _ReviewerAnchorFixtureMixin,
):
    def test_validating_route_restores_anchor(self) -> None:
        # No id lists / no `pending_fix_at`; the lone anchor is the recorded
        # reviewer PR comment. Reconstruction must re-fetch it by id even
        # though it is orchestrator-authored and the watermark has advanced.
        gh, issue, pr = self._pr_with_reviewer_anchor()
        gh.seed_state(
            ISSUE,
            pr_last_comment_id=ADVANCED_PR_COMMENT_WATERMARK,
            pending_fix_reviewer_comment_id=BATCH_PR_CONVERSATION_ID,
        )
        state = gh.read_pinned_state(issue)

        batch = _reconstruct_pending_fix_batch(gh, issue, pr, state)

        self.assertEqual(
            [feedback_item.id for feedback_item in batch],
            [BATCH_PR_CONVERSATION_ID],
        )
        self._prompt = workflow._build_pr_comment_followup(batch)
        self.assertIn("please fix the docstring ordering", self._prompt)

    def test_anchor_survives_author_allowlist(self) -> None:
        # The anchor is the orchestrator's own reviewer output, so it must be
        # replayed even when the allowlist does NOT list the orchestrator's
        # login -- it is prepended OUTSIDE `filter_trusted`.
        gh, issue, pr = self._pr_with_reviewer_anchor()
        gh.seed_state(
            ISSUE,
            pr_last_comment_id=ADVANCED_PR_COMMENT_WATERMARK,
            pending_fix_reviewer_comment_id=BATCH_PR_CONVERSATION_ID,
        )
        state = gh.read_pinned_state(issue)

        with patch.object(config, ALLOWED_AUTHORS_CONFIG, (ALLOWED_AUTHOR,)):
            batch = _reconstruct_pending_fix_batch(gh, issue, pr, state)

        self.assertEqual(
            [feedback_item.id for feedback_item in batch],
            [BATCH_PR_CONVERSATION_ID],
        )

    def test_pending_fix_at_ignores_anchor(self) -> None:
        # A stale anchor left behind by an earlier validating park must NOT be
        # prepended to an in_review-route batch (`pending_fix_at` set): the
        # route discriminator gates it out.
        gh, issue, pr = self._pr_with_reviewer_anchor()
        gh.seed_state(
            ISSUE,
            pr_last_comment_id=ADVANCED_PR_COMMENT_WATERMARK,
            pending_fix_at=PENDING_FIX_AT_TS,
            pending_fix_reviewer_comment_id=BATCH_PR_CONVERSATION_ID,
        )
        state = gh.read_pinned_state(issue)

        self.assertEqual(_reconstruct_pending_fix_batch(gh, issue, pr, state), [])

    def test_missing_anchor_comment_yields_empty(self) -> None:
        # The anchor id points at a comment that no longer exists (deleted, or
        # a PR read that returned without it): reconstruction yields an empty
        # batch so the caller's refusal holds.
        gh, issue, pr = self._pr_with_reviewer_anchor()
        gh.seed_state(
            ISSUE,
            pr_last_comment_id=ADVANCED_PR_COMMENT_WATERMARK,
            pending_fix_reviewer_comment_id=MISSING_ANCHOR_ID,
        )
        state = gh.read_pinned_state(issue)

        self.assertEqual(_reconstruct_pending_fix_batch(gh, issue, pr, state), [])

    def test_id_set_prefers_list_rejects_bool_max(self) -> None:
        from orchestrator.github import PinnedState

        # Full list present -> used verbatim (the max id is ignored).
        state = PinnedState(data={ID_LIST_KEY: [3, 1, 2], MAX_ID_KEY: 9})
        self.assertEqual(_pending_fix_id_set(state, ID_LIST_KEY, MAX_ID_KEY), {1, 2, 3})
        # Only the max id -> conservative single-item set.
        state = PinnedState(data={MAX_ID_KEY: 9})
        self.assertEqual(_pending_fix_id_set(state, ID_LIST_KEY, MAX_ID_KEY), {9})
        # A stray bool must not read as id 1 (bool is an int subclass).
        state = PinnedState(data={MAX_ID_KEY: True})
        self.assertEqual(_pending_fix_id_set(state, ID_LIST_KEY, MAX_ID_KEY), set())
        # Neither present -> empty.
        self.assertEqual(
            _pending_fix_id_set(PinnedState(data={}), ID_LIST_KEY, MAX_ID_KEY),
            set(),
        )

    def test_clear_bookmarks_clears_batch_id_lists(self) -> None:
        from orchestrator.github import PinnedState

        state = PinnedState(
            data={
                PENDING_FIX_AT: PENDING_FIX_AT_TS,
                PENDING_FIX_ISSUE_MAX_ID: BATCH_PR_CONVERSATION_ID,
                PENDING_FIX_REVIEW_MAX_ID: BATCH_INLINE_SECOND_ID,
                PENDING_FIX_REVIEW_SUMMARY_MAX_ID: BATCH_SUMMARY_ID,
                PENDING_FIX_ISSUE_IDS: list(BATCH_ISSUE_IDS),
                PENDING_FIX_REVIEW_IDS: list(BATCH_INLINE_IDS),
                PENDING_FIX_REVIEW_SUMMARY_IDS: list(BATCH_SUMMARY_IDS),
                PENDING_FIX_REVIEWER_COMMENT_ID: BATCH_PR_CONVERSATION_ID,
            }
        )

        _clear_pending_fix_bookmarks(state)

        for key in (
            PENDING_FIX_AT,
            PENDING_FIX_ISSUE_MAX_ID,
            PENDING_FIX_REVIEW_MAX_ID,
            PENDING_FIX_REVIEW_SUMMARY_MAX_ID,
            PENDING_FIX_ISSUE_IDS,
            PENDING_FIX_REVIEW_IDS,
            PENDING_FIX_REVIEW_SUMMARY_IDS,
            PENDING_FIX_REVIEWER_COMMENT_ID,
        ):
            self.assertIsNone(state.get(key))
