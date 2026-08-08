# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The owners the question stage borrows from, and the boundary each one pins.

The stage owns its own session, but nothing else it runs: the tracked spawn, the
park, the prompt builders, the trusted conversation text, and the stderr
diagnostics all belong to `workflow/engine/`, and the scratch checkout it works
in -- from the branch name through the commit and dirty probes that enforce the
read-only contract to the teardown -- belongs to `git/`. Each is imported from
that owner rather than read off the `orchestrator.workflow` facade, so a patch
that has to intercept one lands on the owner. Every case patches BOTH -- the
owner mock has to answer and the facade guard has to stay untouched -- which is
what fails if a call site drifts back to `_wf`.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.git.worktrees import terminal as _worktree_terminal
from orchestrator.workflow.engine import (
    comments as _comments,
    guards as _guards,
    messages as _messages,
    prompts as _prompts,
    usage as _usage,
)
from orchestrator.workflow.stages.question import (
    handler as _question,
    models as _models,
    outcomes as _outcomes,
    run as _run,
    session as _session,
    state as _state,
)

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import _FAKE_WT, _TEST_SPEC, _agent
from tests.workflow_owner_boundaries import OwnerBoundaryMixin

BOUNDARY_ISSUE = 910
BOUNDARY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-910"
BOUNDARY_SESSION = "q-sess-boundary"
BOUNDARY_PROMPT = "answer the standing question"
BOUNDARY_THREAD = "@alice asked something"
BOUNDARY_ANSWER = "it lives in src/x.py"
BOUNDARY_DIAGNOSTICS = "\n\nstderr tail"
LABEL_DONE = "done"

CLEANUP_QUESTION_WORKTREE = "_cleanup_question_worktree"
PARK_AWAITING_HUMAN = "_park_awaiting_human"
RUN_AGENT_TRACKED = "_run_agent_tracked"
BUILD_QUESTION_PROMPT = "_build_question_prompt"
RECENT_COMMENTS_TEXT = "_recent_comments_text"
FORMAT_STDERR_DIAGNOSTICS = "_format_stderr_diagnostics"
BOUNDARY_DIRTY_FILE = "notes.md"


def _question_run(*, closed: bool = False) -> _models._QuestionRun:
    """One question tick over an open or manually closed issue."""
    gh = FakeGitHubClient()
    issue = make_issue(BOUNDARY_ISSUE, label="question")
    issue.closed = closed
    gh.add_issue(issue)
    return _models._QuestionRun.start(gh, _TEST_SPEC, issue)


class _QuestionBoundaryMixin(OwnerBoundaryMixin):
    """Hold the checkout seams the question stage does not own."""

    def _worktree_on_its_owners(self):
        return self.git_seams_on_owners(
            _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
            _ensure_worktree=MagicMock(return_value=_FAKE_WT),
        )


class WorktreeTeardownBoundaryTest(unittest.TestCase, _QuestionBoundaryMixin):
    """Both teardowns land on the worktree terminal owner.

    `_cleanup_question_worktree` resolves on `workflow` too, so a mock left on
    the facade would silently let a real teardown run.
    """

    def test_safe_exit_tears_down_on_owner(self) -> None:
        run = _question_run()
        with (
            self.facade_out_of_the_path(CLEANUP_QUESTION_WORKTREE),
            self._worktree_on_its_owners(),
            patch.object(
                _worktree_terminal, CLEANUP_QUESTION_WORKTREE,
            ) as cleanup,
        ):
            _question._cleanup_question_run(run)
            cleanup.assert_called_once()

    def test_closed_finalize_tears_down_on_owner(self) -> None:
        run = _question_run(closed=True)
        with (
            self.facade_out_of_the_path(CLEANUP_QUESTION_WORKTREE),
            self._worktree_on_its_owners(),
            patch.object(
                _worktree_terminal, CLEANUP_QUESTION_WORKTREE,
            ) as cleanup,
        ):
            self.assertTrue(_question._finalize_closed_question(run))
            cleanup.assert_called_once()
        self.assertIn((BOUNDARY_ISSUE, LABEL_DONE), run.gh.label_history)


class EngineRunBoundaryTest(unittest.TestCase, _QuestionBoundaryMixin):
    """The tracked spawn and the park land on their engine owners."""

    def test_prompt_execution_lands_on_usage_owner(self) -> None:
        run = _question_run()
        session = _models._QuestionSession(
            agent_spec="claude",
            backend="claude",
            extra_args=(),
            session_id=None,
        )
        with (
            self.facade_out_of_the_path(RUN_AGENT_TRACKED, returns=_agent()),
            patch.object(
                _usage,
                RUN_AGENT_TRACKED,
                return_value=_agent(session_id=BOUNDARY_SESSION),
            ) as spawn,
        ):
            _run._execute_question_prompt(
                run, session, BOUNDARY_PROMPT, _FAKE_WT,
            )
            spawn.assert_called_once()
        # The session id a run hands back is retained by this owner, not by
        # whichever caller started the run.
        self.assertEqual(
            run.state.get(_state._QUESTION_SESSION_KEY), BOUNDARY_SESSION,
        )

    def test_park_funnel_lands_on_guard_owner(self) -> None:
        run = _question_run()
        with (
            self.facade_out_of_the_path(PARK_AWAITING_HUMAN),
            patch.object(_guards, PARK_AWAITING_HUMAN) as park,
        ):
            _run._park_question(
                run, BOUNDARY_ANSWER, reason=_state._QUESTION_ANSWER,
            )
            park.assert_called_once()
        # The shared helper clears `park_reason`; the stage-specific one is
        # restored here, and the implementing relabel guard reads it back.
        self.assertEqual(run.state.get("park_reason"), _state._QUESTION_ANSWER)


class EnginePromptBoundaryTest(unittest.TestCase, _QuestionBoundaryMixin):
    """The question prompt and the thread it quotes land on their owners."""

    def test_fresh_spawn_builds_on_owners(self) -> None:
        run = _question_run()
        with (
            self.facade_out_of_the_path(
                BUILD_QUESTION_PROMPT, returns=BOUNDARY_PROMPT,
            ),
            self.facade_out_of_the_path(
                RECENT_COMMENTS_TEXT, returns=BOUNDARY_THREAD,
            ),
            self._worktree_on_its_owners(),
            patch.object(
                _comments, RECENT_COMMENTS_TEXT, return_value=BOUNDARY_THREAD,
            ) as thread,
            patch.object(
                _prompts, BUILD_QUESTION_PROMPT, return_value=BOUNDARY_PROMPT,
            ) as build,
            patch.object(
                _usage, RUN_AGENT_TRACKED, return_value=_agent(),
            ) as spawn,
        ):
            _run._spawn_fresh_question(run)
            thread.assert_called_once()
            build.assert_called_once()
            self.assertEqual(
                spawn.call_args.kwargs["prompt"], BOUNDARY_PROMPT,
            )

    def test_sessionless_resume_uses_first_round(self) -> None:
        with (
            self.facade_out_of_the_path(
                BUILD_QUESTION_PROMPT, returns=BOUNDARY_PROMPT,
            ),
            patch.object(_comments, RECENT_COMMENTS_TEXT, return_value=""),
            patch.object(
                _prompts, BUILD_QUESTION_PROMPT, return_value=BOUNDARY_PROMPT,
            ) as build,
        ):
            prompt = _session._build_question_resume_prompt(
                _TEST_SPEC, make_issue(BOUNDARY_ISSUE), [], None,
            )
            build.assert_called_once()
        self.assertEqual(prompt, BOUNDARY_PROMPT)


class ReadOnlyAssessmentBoundaryTest(unittest.TestCase, _QuestionBoundaryMixin):
    """The read-only verdict is read off the git probes, not the facade."""

    def test_dirty_tree_parks_off_probe_owners(self) -> None:
        # The whole contract this stage exists to enforce is decided by these
        # three reads, so a mock left on the facade would let a real worktree
        # scan answer for a run that never touched one.
        run = _question_run()
        with self.git_seams_on_owners(
            _worktree_path=MagicMock(return_value=_FAKE_WT),
            _has_new_commits=MagicMock(return_value=False),
            _worktree_dirty_files=MagicMock(return_value=[BOUNDARY_DIRTY_FILE]),
        ):
            outcome = _outcomes._assess_question_worktree(run, _agent())
        self.assertEqual(outcome.park_reason, _state._QUESTION_DIRTY)
        self.assertEqual(outcome.dirty_files, (BOUNDARY_DIRTY_FILE,))
        # A violation keeps the tree on disk for the operator to inspect.
        self.assertTrue(outcome.keep_worktree)


class EngineDiagnosticsBoundaryTest(unittest.TestCase, _QuestionBoundaryMixin):
    """The silent park's stderr block lands on the engine message owner."""

    def test_silent_park_reads_diagnostics_off_owner(self) -> None:
        run = _question_run()
        with (
            self.facade_out_of_the_path(
                FORMAT_STDERR_DIAGNOSTICS, returns=BOUNDARY_DIAGNOSTICS,
            ),
            patch.object(
                _messages,
                FORMAT_STDERR_DIAGNOSTICS,
                return_value=BOUNDARY_DIAGNOSTICS,
            ) as diagnostics,
        ):
            _outcomes._park_silent_question(run, _agent())
            diagnostics.assert_called_once()
        park_comment = run.gh.posted_comments[-1][1]
        self.assertIn(BOUNDARY_DIAGNOSTICS.strip(), park_comment)


if __name__ == "__main__":
    unittest.main()
