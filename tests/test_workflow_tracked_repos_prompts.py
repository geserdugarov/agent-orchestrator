# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementer and documentation tracked-repository prompt tests."""
from __future__ import annotations

import unittest

from tests import workflow_tracked_repos_test_support as support


class ImplementerSpawnTrackedReposTest(unittest.TestCase, support._PatchedWorkflowMixin):
    """The initial implementer spawn carries the block in a multi-repo
    deployment and stays block-free in the single-repo default."""

    def test_multi_repo_spawn_carries_block(self) -> None:
        with support._multi_repo():
            prompt = support._implementer_prompt(self)
        self.assertIn(support._BLOCK_MARKER, prompt)
        # The sibling's slug and durable checkout path are surfaced; the
        # current repo is not listed as a reference checkout.
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        self.assertIn("/srv/sibling-checkout", prompt)
        # Still the implementer prompt -- the block is additive, not a swap.
        self.assertIn("You are the implementer", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        # The default single-repo deployment must see zero added tokens.
        with support.patch.object(support.config, support._EXPOSE_REPOS_ATTR, True), \
             support.patch.object(support.config, support._DEFAULT_SPECS_ATTR, lambda: [support._TEST_SPEC]):
            prompt = support._implementer_prompt(self)
        self.assertNotIn(support._BLOCK_MARKER, prompt)


class DocumentationSpawnTrackedReposTest(
    unittest.TestCase, support._PatchedWorkflowMixin
):
    """Both documentation-prompt paths -- the initial final-docs pass and the
    awaiting-human resume -- thread the full specs list into the prompt."""

    def test_initial_docs_pass_carries_block(self) -> None:
        gh, issue = support._documentation_seed()
        with support._multi_repo():
            mocks = self._run(
                lambda: support.workflow._handle_documenting(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id=support._DEV_SESSION_ID,
                    last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=[support._BEFORE_SHA, support._AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        prompt = support._prompt_of(mocks[support._RUN_AGENT_ATTR])
        self.assertIn(support._BLOCK_MARKER, prompt)
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        # Still the documentation prompt.
        self.assertIn("documentation pass", prompt)

    def test_human_reply_resume_carries_block(self) -> None:
        gh, issue = support._documentation_seed(
            awaiting_human=True,
            last_action_comment_id=support._DOCUMENTATION_WATERMARK,
            park_reason="agent_timeout",
        )
        issue.comments.append(
            support.FakeComment(
                id=support._DOCUMENTATION_REPLY_ID,
                body="please retry",
                user=support.FakeUser("alice"),
            )
        )
        with support._multi_repo():
            mocks = self._run(
                lambda: support.workflow._handle_documenting(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id=support._DEV_SESSION_ID,
                    last_message="docs: documented thing",
                ),
                push_branch=True,
                head_shas=[support._BEFORE_SHA, support._AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        prompt = support._prompt_of(mocks[support._RUN_AGENT_ATTR])
        self.assertIn(support._BLOCK_MARKER, prompt)
        self.assertIn("documentation pass", prompt)

    def test_fresh_docs_respawn_has_block_once(self) -> None:
        # `dev_agent` set but NO `dev_session_id` -> the docs prompt (which
        # already carries the block) goes through `_resume_dev_with_text`'s
        # transcript-less fresh-spawn path, which prepends the re-grounding
        # preamble. The preamble must suppress its own copy of the block so
        # the composed prompt lists the tracked repos exactly once.
        gh, issue = support._documentation_seed(dev_session_id=None)
        with support._multi_repo():
            mocks = self._run(
                lambda: support.workflow._handle_documenting(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id="fresh-sess", last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=[support._BEFORE_SHA, support._AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        prompt = support._prompt_of(mocks[support._RUN_AGENT_ATTR])
        self.assertEqual(prompt.count(support._BLOCK_MARKER), 1)
        # Both the fresh-respawn preamble and the docs prompt body survive.
        self.assertIn("resuming work on GitHub issue", prompt)
        self.assertIn("documentation pass", prompt)

    def test_single_repo_docs_pass_has_no_block(self) -> None:
        gh, issue = support._documentation_seed()
        with support.patch.object(support.config, support._EXPOSE_REPOS_ATTR, True), \
             support.patch.object(support.config, support._DEFAULT_SPECS_ATTR, lambda: [support._TEST_SPEC]):
            mocks = self._run(
                lambda: support.workflow._handle_documenting(gh, support._TEST_SPEC, issue),
                run_agent=support._agent(
                    session_id=support._DEV_SESSION_ID,
                    last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=[support._BEFORE_SHA, support._AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        self.assertNotIn(support._BLOCK_MARKER, support._prompt_of(mocks[support._RUN_AGENT_ATTR]))
