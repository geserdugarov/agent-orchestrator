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
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import FakeComment, FakeGitHubClient, FakeUser, make_issue
from tests.workflow_helpers import _FAKE_WT, _PatchedWorkflowMixin, _TEST_SPEC, _agent

# Distinctive lead-in of `_build_tracked_repos_context`; its presence (and
# count) in a spawned prompt is the signal that the block was threaded.
_BLOCK_MARKER = "This orchestrator also tracks the repositories below"

# A second tracked repo so the block has something to render. `_TEST_SPEC`
# is the current repo (excluded from the listing); this is the sibling whose
# slug / checkout path the block must surface.
_OTHER_SPEC = config.RepoSpec(
    slug="acme/sibling",
    target_root=Path("/srv/sibling-checkout"),
    base_branch="develop",
)
_MULTI_SPECS = [_TEST_SPEC, _OTHER_SPEC]


@contextlib.contextmanager
def _multi_repo():
    """Enter a two-repo deployment with the awareness block enabled.

    Patches the exact `config` object the stage handlers and the block builder
    both read, so `config.default_repo_specs()` yields the sibling and the
    kill switch is on regardless of ambient env.
    """
    with patch.object(config, "EXPOSE_TRACKED_REPOS", True), \
         patch.object(config, "default_repo_specs", lambda: list(_MULTI_SPECS)):
        yield


def _prompt_of(run_agent_mock) -> str:
    call = run_agent_mock.call_args
    return call.kwargs.get("prompt") or call.args[1]


class ImplementerSpawnTrackedReposTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The initial implementer spawn carries the block in a multi-repo
    deployment and stays block-free in the single-repo default."""

    def _spawn_prompt(self) -> str:
        gh = FakeGitHubClient()
        issue = make_issue(701, label="implementing")
        gh.add_issue(issue)
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            push_branch=True,
        )
        return _prompt_of(mocks["run_agent"])

    def test_multi_repo_spawn_carries_block(self) -> None:
        with _multi_repo():
            prompt = self._spawn_prompt()
        self.assertIn(_BLOCK_MARKER, prompt)
        # The sibling's slug and durable checkout path are surfaced; the
        # current repo is not listed as a reference checkout.
        self.assertIn("acme/sibling", prompt)
        self.assertIn("/srv/sibling-checkout", prompt)
        # Still the implementer prompt -- the block is additive, not a swap.
        self.assertIn("You are the implementer", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        # The default single-repo deployment must see zero added tokens.
        with patch.object(config, "EXPOSE_TRACKED_REPOS", True), \
             patch.object(config, "default_repo_specs", lambda: [_TEST_SPEC]):
            prompt = self._spawn_prompt()
        self.assertNotIn(_BLOCK_MARKER, prompt)


class DocumentationSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Both documentation-prompt paths -- the initial final-docs pass and the
    awaiting-human resume -- thread the full specs list into the prompt."""

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(702, label="documenting")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=72,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-702",
            dev_agent="codex",
            dev_session_id="dev-sess",
        )
        defaults.update(state)
        gh.seed_state(702, **defaults)
        return gh, issue

    def test_initial_docs_pass_carries_block(self) -> None:
        gh, issue = self._seeded()
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                branch_ahead_behind=(0, 0),
            )
        prompt = _prompt_of(mocks["run_agent"])
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("acme/sibling", prompt)
        # Still the documentation prompt.
        self.assertIn("documentation pass", prompt)

    def test_human_reply_resume_carries_block(self) -> None:
        gh, issue = self._seeded(
            awaiting_human=True,
            last_action_comment_id=6000,
            park_reason="agent_timeout",
        )
        issue.comments.append(
            FakeComment(id=6100, body="please retry", user=FakeUser("alice"))
        )
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docs: documented thing",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                branch_ahead_behind=(0, 0),
            )
        prompt = _prompt_of(mocks["run_agent"])
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("documentation pass", prompt)

    def test_fresh_respawn_docs_pass_carries_block_once(self) -> None:
        # `dev_agent` set but NO `dev_session_id` -> the docs prompt (which
        # already carries the block) goes through `_resume_dev_with_text`'s
        # transcript-less fresh-spawn path, which prepends the re-grounding
        # preamble. The preamble must suppress its own copy of the block so
        # the composed prompt lists the tracked repos exactly once.
        gh, issue = self._seeded(dev_session_id=None)
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="fresh-sess", last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                branch_ahead_behind=(0, 0),
            )
        prompt = _prompt_of(mocks["run_agent"])
        self.assertEqual(prompt.count(_BLOCK_MARKER), 1)
        # Both the fresh-respawn preamble and the docs prompt body survive.
        self.assertIn("resuming work on GitHub issue", prompt)
        self.assertIn("documentation pass", prompt)

    def test_single_repo_docs_pass_has_no_block(self) -> None:
        gh, issue = self._seeded()
        with patch.object(config, "EXPOSE_TRACKED_REPOS", True), \
             patch.object(config, "default_repo_specs", lambda: [_TEST_SPEC]):
            mocks = self._run(
                lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docs: updated README",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                branch_ahead_behind=(0, 0),
            )
        self.assertNotIn(_BLOCK_MARKER, _prompt_of(mocks["run_agent"]))


class FreshRespawnTrackedReposTest(unittest.TestCase):
    """A transcript-less fresh respawn is re-grounded with the preamble, which
    carries the block exactly once; a true in-place resume sends the bare
    stage followup and stays block-free (no duplication on the live session)."""

    def _seeded_issue(self, *, resume_count: int):
        gh = FakeGitHubClient()
        issue = make_issue(703, label="in_review", body="implement the thing")
        gh.add_issue(issue)
        gh.seed_state(
            703,
            dev_agent="claude",
            dev_session_id="live-sess",
            silent_park_count=0,
            dev_resume_count=resume_count,
        )
        return gh, issue

    def _resume(self, gh, issue, *, threshold):
        prompts: list[str] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            prompts.append(prompt)
            return _agent(session_id="fresh-sess", last_message="ok")

        state = gh.read_pinned_state(issue)
        with _multi_repo(), \
             patch.object(config, "DEV_SESSION_MAX_RESUMES", threshold), \
             patch.object(workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "fix it")
        return prompts[0]

    def test_fresh_respawn_carries_block_exactly_once(self) -> None:
        # Budget reached -> rotation fresh-spawns; the preamble re-grounds the
        # transcript-less agent AND carries the block. Exactly once: the bare
        # followup ("fix it") contributes no second copy.
        gh, issue = self._seeded_issue(resume_count=10)
        prompt = self._resume(gh, issue, threshold=10)
        self.assertEqual(prompt.count(_BLOCK_MARKER), 1)
        self.assertIn("acme/sibling", prompt)
        # The preamble and the appended stage followup both survive.
        self.assertIn("resuming work on GitHub issue", prompt)
        self.assertTrue(prompt.rstrip().endswith("fix it"))

    def test_true_resume_followup_is_block_free(self) -> None:
        # Below budget -> resume in place. The live session already carries the
        # issue context in its transcript, so the bare followup is sent with no
        # re-grounding and -- crucially -- no tracked-repos block.
        gh, issue = self._seeded_issue(resume_count=1)
        prompt = self._resume(gh, issue, threshold=10)
        self.assertEqual(prompt, "fix it")
        self.assertNotIn(_BLOCK_MARKER, prompt)


class DecomposerSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The fresh decomposer spawn carries the block in a multi-repo deployment
    and stays block-free in the single-repo default. The decomposer is
    read-only -- the block is additive and must not override that contract."""

    _MANIFEST = (
        "fits one context\n\n"
        "```orchestrator-manifest\n"
        '{"decision": "single", "rationale": "small"}\n'
        "```\n"
    )

    def _spawn_prompt(self) -> str:
        gh = FakeGitHubClient()
        issue = make_issue(710, label="decomposing")
        gh.add_issue(issue)
        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-1", last_message=self._MANIFEST),
        )
        return _prompt_of(mocks["run_agent"])

    def test_multi_repo_spawn_carries_block(self) -> None:
        with _multi_repo():
            prompt = self._spawn_prompt()
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("acme/sibling", prompt)
        self.assertIn("/srv/sibling-checkout", prompt)
        # Still the decomposer prompt with its read-only contract intact.
        self.assertIn("You are the decomposer", prompt)
        self.assertIn("you are read-only", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        with patch.object(config, "EXPOSE_TRACKED_REPOS", True), \
             patch.object(config, "default_repo_specs", lambda: [_TEST_SPEC]):
            prompt = self._spawn_prompt()
        self.assertNotIn(_BLOCK_MARKER, prompt)


class ReviewerSpawnTrackedReposTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The reviewer spawn carries the block in a multi-repo deployment and
    stays block-free in the single-repo default. The block must not soften
    the reviewer-only no-edit contract."""

    def _seeded(self):
        gh = FakeGitHubClient()
        issue = make_issue(711, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            711,
            pr_number=11,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-711",
            codex_session_id="dev-sess",
            review_round=0,
        )
        return gh, issue

    def _spawn_prompt(self) -> str:
        gh, issue = self._seeded()
        # APPROVED keeps the reviewer the only agent spawned this tick, so the
        # captured prompt is unambiguously the review prompt.
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        return _prompt_of(mocks["run_agent"])

    def test_multi_repo_spawn_carries_block(self) -> None:
        with _multi_repo():
            prompt = self._spawn_prompt()
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("acme/sibling", prompt)
        # Still the reviewer prompt with the reviewer-only contract intact.
        self.assertIn("automated code reviewer", prompt)
        self.assertIn("you are a reviewer only", prompt)

    def test_single_repo_spawn_has_no_block(self) -> None:
        with patch.object(config, "EXPOSE_TRACKED_REPOS", True), \
             patch.object(config, "default_repo_specs", lambda: [_TEST_SPEC]):
            prompt = self._spawn_prompt()
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
        issue = make_issue(712, label="question", body="Where does X live?")
        gh.add_issue(issue)
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="q-1", last_message="X lives in src/x.py.",
                ),
            )
        prompt = _prompt_of(mocks["run_agent"])
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("acme/sibling", prompt)
        # Still the question prompt with its read-only contract intact.
        self.assertIn("answering a standing question", prompt)
        self.assertIn("You MUST NOT modify", prompt)

    def test_fresh_spawn_single_repo_has_no_block(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(712, label="question", body="Where does X live?")
        gh.add_issue(issue)
        with patch.object(config, "EXPOSE_TRACKED_REPOS", True), \
             patch.object(config, "default_repo_specs", lambda: [_TEST_SPEC]):
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="q-1", last_message="X lives in src/x.py.",
                ),
            )
        self.assertNotIn(_BLOCK_MARKER, _prompt_of(mocks["run_agent"]))

    def test_recovery_without_session_id_carries_block(self) -> None:
        # No `question_session_id` -> a transcript-less FRESH spawn. The
        # handler must send the full question prompt (block included) so the
        # recovery run sees the same context a first-tick spawn would, rather
        # than the bare followup a live session would get.
        gh = FakeGitHubClient()
        issue = make_issue(
            713, label="question",
            title="Where does X live?",
            body="We need to know where X lives.",
        )
        issue.comments.append(
            FakeComment(id=42000, body="any progress?", user=FakeUser("alice")),
        )
        gh.add_issue(issue)
        gh.seed_state(
            713,
            awaiting_human=True,
            last_action_comment_id=41000,
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
        prompt = _prompt_of(mocks["run_agent"])
        # Fresh spawn (no resume) carrying the full question prompt + block.
        self.assertIsNone(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id")
        )
        self.assertIn(_BLOCK_MARKER, prompt)
        self.assertIn("acme/sibling", prompt)
        self.assertIn("answering a standing question", prompt)

    def test_live_session_resume_followup_is_block_free(self) -> None:
        # A live `question_session_id` resumes in place: the followup prompt
        # carries only the human's reply, never the block (the session already
        # saw the initial block at spawn).
        gh = FakeGitHubClient()
        issue = make_issue(714, label="question", title="Q", body="body")
        issue.comments.append(
            FakeComment(
                id=52000, body="here is more detail", user=FakeUser("alice"),
            ),
        )
        gh.add_issue(issue)
        gh.seed_state(
            714,
            awaiting_human=True,
            last_action_comment_id=51000,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id="q-live",
            park_reason="question_answer",
        )
        with _multi_repo():
            mocks = self._run(
                lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="q-live", last_message="answer"),
            )
        prompt = _prompt_of(mocks["run_agent"])
        self.assertNotIn(_BLOCK_MARKER, prompt)
        # It IS the followup prompt carrying the human's reply.
        self.assertIn("here is more detail", prompt)


if __name__ == "__main__":
    unittest.main()
