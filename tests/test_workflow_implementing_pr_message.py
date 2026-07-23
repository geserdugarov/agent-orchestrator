# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr message behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

BODY_SHORT_ISSUE = support.BODY_SHORT_ISSUE
CODE_FENCE_LINE_COUNT = support.CODE_FENCE_LINE_COUNT
DEV_SESSION = support.DEV_SESSION
FEATURE_PREFIX = support.FEATURE_PREFIX
FakeGitHubClient = support.FakeGitHubClient
GITHUB_BODY_LIMIT = support.GITHUB_BODY_LIMIT
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LONG_BODY_REPEAT_COUNT = support.LONG_BODY_REPEAT_COUNT
LONG_MESSAGE_WORD_COUNT = support.LONG_MESSAGE_WORD_COUNT
TEST_ISSUE_TITLE = support.TEST_ISSUE_TITLE
TOKEN_TAIL_LENGTH = support.TOKEN_TAIL_LENGTH
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_agent = support._agent
implementing = support.implementing
make_issue = support.make_issue


class FormatPrAgentMessageTest(unittest.TestCase):
    """`_format_pr_agent_message` caps the agent's final message with a visible
    truncation marker (issue #499): a short message is verbatim, a long one is
    trimmed on a boundary and explicitly marked, and a dangling code fence is
    closed so the marker still renders."""

    def test_short_message_returned_verbatim(self) -> None:
        msg = "all done — see the diff."
        out = implementing._format_pr_agent_message(msg)
        self.assertEqual(out, msg)
        self.assertNotIn(implementing._PR_BODY_TRUNCATION_MARKER, out)

    def test_message_at_cap_is_not_marked(self) -> None:
        msg = "x" * implementing._PR_BODY_AGENT_MESSAGE_CAP
        out = implementing._format_pr_agent_message(msg)
        self.assertEqual(out, msg)
        self.assertNotIn(implementing._PR_BODY_TRUNCATION_MARKER, out)

    def test_long_message_capped_with_marker(self) -> None:
        msg = "word " * LONG_MESSAGE_WORD_COUNT  # ~100k chars, well over the cap
        out = implementing._format_pr_agent_message(msg)
        # Explicit, visible truncation marker is present.
        self.assertIn(implementing._PR_BODY_TRUNCATION_MARKER, out)
        # The kept text stays within the cap (plus the appended marker).
        self.assertLessEqual(
            len(out),
            implementing._PR_BODY_AGENT_MESSAGE_CAP + len(implementing._PR_BODY_TRUNCATION_MARKER) + 8,
        )
        # And the body ends with the marker, not mid-word.
        self.assertTrue(out.rstrip().endswith(implementing._PR_BODY_TRUNCATION_MARKER))

    def test_cap_lands_on_token_boundary(self) -> None:
        # A single unbroken run with one space near the cap: the cut must
        # fall back to the word boundary rather than slicing mid-token.
        head = "a" * (implementing._PR_BODY_AGENT_MESSAGE_CAP - 5)
        tail = "b" * TOKEN_TAIL_LENGTH
        msg = f"{head} {tail}"
        out = implementing._format_pr_agent_message(msg)
        marker = implementing._PR_BODY_TRUNCATION_MARKER
        self._kept = out.split(f"\n\n{marker}")[0]
        # Cut at the space: no stray `b` characters leaked past the boundary.
        self.assertEqual(self._kept, head)

    def test_dangling_code_fence_is_closed(self) -> None:
        # Force the cut to land inside an open code fence and assert it gets
        # closed so the marker renders outside the block.
        code_lines = "x = 1\n" * CODE_FENCE_LINE_COUNT
        msg = f"intro\n\n```python\n{code_lines}"
        out = implementing._format_pr_agent_message(msg)
        self.assertEqual(out.count("```") % 2, 0)
        self.assertIn(implementing._PR_BODY_TRUNCATION_MARKER, out)


class OnCommitsBodyTruncationTest(unittest.TestCase, _PatchedWorkflowMixin):
    """End-to-end: a long dev message yields a PR body that fits GitHub's
    65,536-char limit and carries the visible truncation marker."""

    def test_long_body_is_capped_and_marked(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(60, label=LABEL_IMPLEMENTING, title=TEST_ISSUE_TITLE)
        gh.add_issue(issue)

        long_message = "This is a long closing note. " * LONG_BODY_REPEAT_COUNT  # ~145k chars

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=long_message),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject=f"{FEATURE_PREFIX}: {TEST_ISSUE_TITLE}",
        )

        self.assertEqual(len(gh.opened_prs), 1)
        body = gh.opened_prs[0].body
        self.assertIn("_Last agent message:_", body)
        # Visible marker present, and the whole body fits GitHub's limit.
        self.assertIn(implementing._PR_BODY_TRUNCATION_MARKER, body)
        self.assertLessEqual(len(body), GITHUB_BODY_LIMIT)

    def test_short_agent_message_body_no_marker(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(BODY_SHORT_ISSUE, label=LABEL_IMPLEMENTING, title=TEST_ISSUE_TITLE)
        gh.add_issue(issue)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="all done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject=f"{FEATURE_PREFIX}: {TEST_ISSUE_TITLE}",
        )

        body = gh.opened_prs[0].body
        self.assertIn("all done", body)
        self.assertNotIn(implementing._PR_BODY_TRUNCATION_MARKER, body)
