# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The tracked-repos awareness block is threaded into both the developer-side
prompts (implementer / documentation / fresh-respawn) and the read-only /
reviewer-only reasoning prompts (decomposer / reviewer / question) so a
multi-repo deployment's spawns learn about the sibling read-only checkouts.
This module pins the wiring end-to-end: the production stage handlers must pass
the *full* specs list (not just the current repo) so the block actually
renders, the single-repo default must stay byte-for-byte block-free, a
transcript-less fresh respawn must carry the block exactly once while a true
in-place resume followup stays block-free, and the read-only / reviewer-only
stages must keep their no-write contract intact alongside the block.
"""
from __future__ import annotations

import contextlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow

from tests.fakes import FakeComment, FakeGitHubClient, FakeUser, make_issue
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _fake_worktree,
)

# Distinctive lead-in of `_build_tracked_repos_context`; its presence (and
# count) in a spawned prompt is the signal that the block was threaded.
_BLOCK_MARKER = "This orchestrator also tracks the repositories below"
_OTHER_REPO_SLUG = "acme/sibling"
_EXPOSE_REPOS_ATTR = "EXPOSE_TRACKED_REPOS"
_DEFAULT_SPECS_ATTR = "default_repo_specs"
_RUN_AGENT_ATTR = "run_agent"
_DEV_SESSION_ID = "dev-sess"
_BEFORE_SHA = "aaa"
_AFTER_SHA = "bbb"
_QUESTION_LABEL = "question"
_IMPLEMENTER_ISSUE_NUMBER = 701
_DOCUMENTATION_ISSUE_NUMBER = 702
_DOCUMENTATION_PR_NUMBER = 72
_RESUME_ISSUE_NUMBER = 703
_DECOMPOSER_ISSUE_NUMBER = 710
_REVIEW_ISSUE_NUMBER = 711
_REVIEW_PR_NUMBER = 11
_FRESH_QUESTION_ISSUE_NUMBER = 712
_RECOVERY_QUESTION_ISSUE_NUMBER = 713
_RECOVERY_COMMENT_ID = 42000
_RECOVERY_WATERMARK = 41000
_RESUMED_QUESTION_ISSUE_NUMBER = 714
_RESUME_COMMENT_ID = 52000
_RESUME_WATERMARK = 51000
_DOCUMENTATION_WATERMARK = 6000
_DOCUMENTATION_REPLY_ID = 6100

# A second tracked repo so the block has something to render. `_TEST_SPEC`
# is the current repo (excluded from the listing); this is the sibling whose
# slug / checkout path the block must surface.
_OTHER_SPEC = config.RepoSpec(
    slug=_OTHER_REPO_SLUG,
    target_root=Path("/srv/sibling-checkout"),
    base_branch="develop",
)
_MULTI_SPECS = (_TEST_SPEC, _OTHER_SPEC)
_DECOMPOSITION_MANIFEST = (
    "fits one context\n\n"
    "```orchestrator-manifest\n"
    '{"decision": "single", "rationale": "small"}\n'
    "```\n"
)


@contextlib.contextmanager
def _multi_repo():
    """Enter a two-repo deployment with the awareness block enabled.

    Patches the exact `config` object the stage handlers and the block builder
    both read, so `config.default_repo_specs()` yields the sibling and the
    kill switch is on regardless of ambient env.
    """
    with patch.object(config, _EXPOSE_REPOS_ATTR, True), \
         patch.object(config, _DEFAULT_SPECS_ATTR, lambda: list(_MULTI_SPECS)):
        yield


def _prompt_of(run_agent_mock) -> str:
    call = run_agent_mock.call_args
    return call.kwargs.get("prompt") or call.args[1]


def _implementer_prompt(case) -> str:
    gh = FakeGitHubClient()
    issue = make_issue(_IMPLEMENTER_ISSUE_NUMBER, label="implementing")
    gh.add_issue(issue)
    mocks = case._run(
        lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
        run_agent=_agent(session_id="sess-1", last_message="done"),
        has_new_commits=[False, True],
        push_branch=True,
    )
    return _prompt_of(mocks[_RUN_AGENT_ATTR])


def _documentation_seed(**state):
    gh = FakeGitHubClient()
    issue = make_issue(_DOCUMENTATION_ISSUE_NUMBER, label="documenting")
    gh.add_issue(issue)
    defaults = dict(
        pr_number=_DOCUMENTATION_PR_NUMBER,
        branch="orchestrator/geserdugarov__agent-orchestrator/issue-702",
        dev_agent="codex",
        dev_session_id=_DEV_SESSION_ID,
    )
    defaults.update(state)
    gh.seed_state(_DOCUMENTATION_ISSUE_NUMBER, **defaults)
    return gh, issue


def _resume_seed(*, resume_count: int):
    gh = FakeGitHubClient()
    issue = make_issue(
        _RESUME_ISSUE_NUMBER,
        label="in_review",
        body="implement the thing",
    )
    gh.add_issue(issue)
    gh.seed_state(
        _RESUME_ISSUE_NUMBER,
        dev_agent="claude",
        dev_session_id="live-sess",
        silent_park_count=0,
        dev_resume_count=resume_count,
    )
    return gh, issue


def _resume_prompt(gh, issue, *, threshold: int) -> str:
    run_mock = MagicMock(
        return_value=_agent(session_id="fresh-sess", last_message="ok"),
    )
    state = gh.read_pinned_state(issue)
    with _multi_repo(), \
         patch.object(config, "DEV_SESSION_MAX_RESUMES", threshold), \
         patch.object(workflow, "_ensure_worktree", _fake_worktree), \
         patch.object(workflow, _RUN_AGENT_ATTR, run_mock):
        workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "fix it")
    return _prompt_of(run_mock)


def _decomposer_prompt(case) -> str:
    gh = FakeGitHubClient()
    issue = make_issue(_DECOMPOSER_ISSUE_NUMBER, label="decomposing")
    gh.add_issue(issue)
    mocks = case._run(
        lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
        run_agent=_agent(
            session_id="dec-1",
            last_message=_DECOMPOSITION_MANIFEST,
        ),
    )
    return _prompt_of(mocks[_RUN_AGENT_ATTR])


def _review_seed():
    gh = FakeGitHubClient()
    issue = make_issue(_REVIEW_ISSUE_NUMBER, label="validating")
    gh.add_issue(issue)
    gh.seed_state(
        _REVIEW_ISSUE_NUMBER,
        pr_number=_REVIEW_PR_NUMBER,
        branch="orchestrator/geserdugarov__agent-orchestrator/issue-711",
        codex_session_id=_DEV_SESSION_ID,
        review_round=0,
    )
    return gh, issue


def _review_prompt(case) -> str:
    gh, issue = _review_seed()
    mocks = case._run(
        lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
        run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
    )
    return _prompt_of(mocks[_RUN_AGENT_ATTR])


class ImplementerSpawnTrackedReposTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The initial implementer spawn carries the block in a multi-repo
    deployment and stays block-free in the single-repo default."""

    def test_multi_repo_spawn_carries_block(self) -> None:
        with _multi_repo():
            prompt = _implementer_prompt(self)
        self.assertIn(_BLOCK_MARKER, prompt)
        # The sibling's slug and durable checkout path are surfaced; the
        # current repo is not listed as a reference checkout.
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        self.assertIn("/srv/sibling-checkout", prompt)
        # Still the implementer prompt -- the block is additive, not a swap.
        self.assertIn("You are the implementer", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        # The default single-repo deployment must see zero added tokens.
        with patch.object(config, _EXPOSE_REPOS_ATTR, True), \
             patch.object(config, _DEFAULT_SPECS_ATTR, lambda: [_TEST_SPEC]):
            prompt = _implementer_prompt(self)
        self.assertNotIn(_BLOCK_MARKER, prompt)


class DocumentationSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Both documentation-prompt paths -- the initial final-docs pass and the
    awaiting-human resume -- thread the full specs list into the prompt."""

    def test_initial_docs_pass_carries_block(self) -> None:
        gh, issue = _documentation_seed()
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id=_DEV_SESSION_ID,
                    last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=[_BEFORE_SHA, _AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        prompt = _prompt_of(mocks[_RUN_AGENT_ATTR])
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        # Still the documentation prompt.
        self.assertIn("documentation pass", prompt)

    def test_human_reply_resume_carries_block(self) -> None:
        gh, issue = _documentation_seed(
            awaiting_human=True,
            last_action_comment_id=_DOCUMENTATION_WATERMARK,
            park_reason="agent_timeout",
        )
        issue.comments.append(
            FakeComment(
                id=_DOCUMENTATION_REPLY_ID,
                body="please retry",
                user=FakeUser("alice"),
            )
        )
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id=_DEV_SESSION_ID,
                    last_message="docs: documented thing",
                ),
                push_branch=True,
                head_shas=[_BEFORE_SHA, _AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        prompt = _prompt_of(mocks[_RUN_AGENT_ATTR])
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("documentation pass", prompt)

    def test_fresh_docs_respawn_has_block_once(self) -> None:
        # `dev_agent` set but NO `dev_session_id` -> the docs prompt (which
        # already carries the block) goes through `_resume_dev_with_text`'s
        # transcript-less fresh-spawn path, which prepends the re-grounding
        # preamble. The preamble must suppress its own copy of the block so
        # the composed prompt lists the tracked repos exactly once.
        gh, issue = _documentation_seed(dev_session_id=None)
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="fresh-sess", last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=[_BEFORE_SHA, _AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        prompt = _prompt_of(mocks[_RUN_AGENT_ATTR])
        self.assertEqual(prompt.count(_BLOCK_MARKER), 1)
        # Both the fresh-respawn preamble and the docs prompt body survive.
        self.assertIn("resuming work on GitHub issue", prompt)
        self.assertIn("documentation pass", prompt)

    def test_single_repo_docs_pass_has_no_block(self) -> None:
        gh, issue = _documentation_seed()
        with patch.object(config, _EXPOSE_REPOS_ATTR, True), \
             patch.object(config, _DEFAULT_SPECS_ATTR, lambda: [_TEST_SPEC]):
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id=_DEV_SESSION_ID,
                    last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=[_BEFORE_SHA, _AFTER_SHA],
                branch_ahead_behind=(0, 0),
            )
        self.assertNotIn(_BLOCK_MARKER, _prompt_of(mocks[_RUN_AGENT_ATTR]))


class FreshRespawnTrackedReposTest(unittest.TestCase):
    """A transcript-less fresh respawn is re-grounded with the preamble, which
    carries the block exactly once; a true in-place resume sends the bare
    stage followup and stays block-free (no duplication on the live session)."""

    def test_fresh_respawn_carries_block_exactly_once(self) -> None:
        # Budget reached -> rotation fresh-spawns; the preamble re-grounds the
        # transcript-less agent AND carries the block. Exactly once: the bare
        # followup ("fix it") contributes no second copy.
        gh, issue = _resume_seed(resume_count=10)
        prompt = _resume_prompt(gh, issue, threshold=10)
        self.assertEqual(prompt.count(_BLOCK_MARKER), 1)
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        # The preamble and the appended stage followup both survive.
        self.assertIn("resuming work on GitHub issue", prompt)
        self.assertTrue(prompt.rstrip().endswith("fix it"))

    def test_true_resume_followup_is_block_free(self) -> None:
        # Below budget -> resume in place. The live session already carries the
        # issue context in its transcript, so the bare followup is sent with no
        # re-grounding and -- crucially -- no tracked-repos block.
        gh, issue = _resume_seed(resume_count=1)
        prompt = _resume_prompt(gh, issue, threshold=10)
        self.assertEqual(prompt, "fix it")
        self.assertNotIn(_BLOCK_MARKER, prompt)


class DecomposerSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The fresh decomposer spawn carries the block in a multi-repo deployment
    and stays block-free in the single-repo default. The decomposer is
    read-only -- the block is additive and must not override that contract."""

    def test_multi_repo_spawn_carries_block(self) -> None:
        with _multi_repo():
            prompt = _decomposer_prompt(self)
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        self.assertIn("/srv/sibling-checkout", prompt)
        # Still the decomposer prompt with its read-only contract intact.
        self.assertIn("You are the decomposer", prompt)
        self.assertIn("you are read-only", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        with patch.object(config, _EXPOSE_REPOS_ATTR, True), \
             patch.object(config, _DEFAULT_SPECS_ATTR, lambda: [_TEST_SPEC]):
            prompt = _decomposer_prompt(self)
        self.assertNotIn(_BLOCK_MARKER, prompt)


class ReviewerSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The reviewer spawn carries the block in a multi-repo deployment and
    stays block-free in the single-repo default. The block must not soften
    the reviewer-only no-edit contract."""

    def test_multi_repo_spawn_carries_block(self) -> None:
        with _multi_repo():
            prompt = _review_prompt(self)
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        # Still the reviewer prompt with the reviewer-only contract intact.
        self.assertIn("automated code reviewer", prompt)
        self.assertIn("you are a reviewer only", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        with patch.object(config, _EXPOSE_REPOS_ATTR, True), \
             patch.object(config, _DEFAULT_SPECS_ATTR, lambda: [_TEST_SPEC]):
            prompt = _review_prompt(self)
        self.assertNotIn(_BLOCK_MARKER, prompt)


class QuestionSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Question-stage prompt routing: the fresh spawn AND the no-session-id
    recovery spawn carry the block in a multi-repo deployment, while a true
    live-session resume sends the block-free followup. The block never softens
    the read-only contract."""

    def test_fresh_spawn_carries_block(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            _FRESH_QUESTION_ISSUE_NUMBER,
            label=_QUESTION_LABEL,
            body="Where does X live?",
        )
        gh.add_issue(issue)
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="q-1", last_message="X lives in src/x.py.",
                ),
            )
        prompt = _prompt_of(mocks[_RUN_AGENT_ATTR])
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        # Still the question prompt with its read-only contract intact.
        self.assertIn("answering a standing question", prompt)
        self.assertIn("You MUST NOT modify", prompt)

    def test_fresh_spawn_single_repo_has_no_block(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            _FRESH_QUESTION_ISSUE_NUMBER,
            label=_QUESTION_LABEL,
            body="Where does X live?",
        )
        gh.add_issue(issue)
        with patch.object(config, _EXPOSE_REPOS_ATTR, True), \
             patch.object(config, _DEFAULT_SPECS_ATTR, lambda: [_TEST_SPEC]):
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="q-1", last_message="X lives in src/x.py.",
                ),
            )
        self.assertNotIn(_BLOCK_MARKER, _prompt_of(mocks[_RUN_AGENT_ATTR]))

    def test_no_session_recovery_has_block(self) -> None:
        # No `question_session_id` -> a transcript-less FRESH spawn. The
        # handler must send the full question prompt (block included) so the
        # recovery run sees the same context a first-tick spawn would, rather
        # than the bare followup a live session would get.
        gh = FakeGitHubClient()
        issue = make_issue(
            _RECOVERY_QUESTION_ISSUE_NUMBER,
            label=_QUESTION_LABEL,
            title="Where does X live?",
            body="We need to know where X lives.",
        )
        issue.comments.append(
            FakeComment(
                id=_RECOVERY_COMMENT_ID,
                body="any progress?",
                user=FakeUser("alice"),
            ),
        )
        gh.add_issue(issue)
        gh.seed_state(
            _RECOVERY_QUESTION_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=_RECOVERY_WATERMARK,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            # No prior session id -- the previous run hiccupped.
            park_reason="question_answer",
        )
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="q-fresh", last_message="X lives in src/x.py.",
                ),
            )
        prompt = _prompt_of(mocks[_RUN_AGENT_ATTR])
        # Fresh spawn (no resume) carrying the full question prompt + block.
        self.assertIsNone(
            mocks[_RUN_AGENT_ATTR].call_args.kwargs.get("resume_session_id")
        )
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn(_OTHER_REPO_SLUG, prompt)
        self.assertIn("answering a standing question", prompt)

    def test_live_resume_followup_omits_block(self) -> None:
        # A live `question_session_id` resumes in place: the followup prompt
        # carries only the human's reply, never the block (the session already
        # saw the initial block at spawn).
        gh = FakeGitHubClient()
        issue = make_issue(
            _RESUMED_QUESTION_ISSUE_NUMBER,
            label=_QUESTION_LABEL,
            title="Q",
            body="body",
        )
        issue.comments.append(
            FakeComment(
                id=_RESUME_COMMENT_ID,
                body="here is more detail",
                user=FakeUser("alice"),
            ),
        )
        gh.add_issue(issue)
        gh.seed_state(
            _RESUMED_QUESTION_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=_RESUME_WATERMARK,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-live",
            park_reason="question_answer",
        )
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="q-live", last_message="answer"),
            )
        prompt = _prompt_of(mocks[_RUN_AGENT_ATTR])
        self.assertNotIn(_BLOCK_MARKER, prompt)
        # It IS the followup prompt carrying the human's reply.
        self.assertIn("here is more detail", prompt)


if __name__ == "__main__":
    unittest.main()
