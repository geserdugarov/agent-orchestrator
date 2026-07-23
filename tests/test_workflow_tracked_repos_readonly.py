# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Read-only agent tracked-repository prompt tests."""
from __future__ import annotations

import unittest

from tests import workflow_tracked_repos_test_support as support
from tests import workflow_tracked_readonly_test_support as readonly_support


class DecomposerSpawnTrackedReposTest(
    unittest.TestCase, support._PatchedWorkflowMixin
):
    """The fresh decomposer spawn carries the block in a multi-repo deployment
    and stays block-free in the single-repo default. The decomposer is
    read-only -- the block is additive and must not override that contract."""

    def test_multi_repo_spawn_carries_block(self) -> None:
        with support._multi_repo():
            prompt = support._decomposer_prompt(self)
        self.assertIn(support._BLOCK_MARKER, prompt)
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        self.assertIn("/srv/sibling-checkout", prompt)
        # Still the decomposer prompt with its read-only contract intact.
        self.assertIn("You are the decomposer", prompt)
        self.assertIn("you are read-only", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        with support.patch.object(support.config, support._EXPOSE_REPOS_ATTR, True), \
             support.patch.object(support.config, support._DEFAULT_SPECS_ATTR, lambda: [support._TEST_SPEC]):
            prompt = support._decomposer_prompt(self)
        self.assertNotIn(support._BLOCK_MARKER, prompt)


class ReviewerSpawnTrackedReposTest(
    unittest.TestCase, support._PatchedWorkflowMixin
):
    """The reviewer spawn carries the block in a multi-repo deployment and
    stays block-free in the single-repo default. The block must not soften
    the reviewer-only no-edit contract."""

    def test_multi_repo_spawn_carries_block(self) -> None:
        with support._multi_repo():
            prompt = readonly_support._review_prompt(self)
        self.assertIn(support._BLOCK_MARKER, prompt)
        self.assertIn(support._OTHER_REPO_SLUG, prompt)
        # Still the reviewer prompt with the reviewer-only contract intact.
        self.assertIn("automated code reviewer", prompt)
        self.assertIn("you are a reviewer only", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        with support.patch.object(support.config, support._EXPOSE_REPOS_ATTR, True), \
             support.patch.object(support.config, support._DEFAULT_SPECS_ATTR, lambda: [support._TEST_SPEC]):
            prompt = readonly_support._review_prompt(self)
        self.assertNotIn(support._BLOCK_MARKER, prompt)
