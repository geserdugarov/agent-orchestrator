# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Developer-session tracked-repository prompt tests."""
from __future__ import annotations

import unittest

from tests import workflow_tracked_repos_test_support as support


class FreshRespawnTrackedReposTest(unittest.TestCase):
    """A transcript-less fresh respawn is re-grounded with the preamble, which
    carries the block exactly once; a true in-place resume sends the bare
    stage followup and stays block-free (no duplication on the live session)."""

    def test_fresh_respawn_carries_block_exactly_once(self) -> None:
        # Budget reached -> rotation fresh-spawns; the preamble re-grounds the
        # transcript-less agent AND carries the block. Exactly once: the bare
        # followup ("fix it") contributes no second copy.
        gh, issue = support._resume_seed(resume_count=10)
        prompt = support._resume_prompt(gh, issue, threshold=10)
        self.assertEqual(prompt.count(support._BLOCK_MARKER), 1)
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        # The preamble and the appended stage followup both survive.
        self.assertIn("resuming work on GitHub issue", prompt)
        self.assertTrue(prompt.rstrip().endswith("fix it"))

    def test_true_resume_followup_is_block_free(self) -> None:
        # Below budget -> resume in place. The live session already carries the
        # issue context in its transcript, so the bare followup is sent with no
        # re-grounding and -- crucially -- no tracked-repos block.
        gh, issue = support._resume_seed(resume_count=1)
        prompt = support._resume_prompt(gh, issue, threshold=10)
        self.assertEqual(prompt, "fix it")
        self.assertNotIn(support._BLOCK_MARKER, prompt)
