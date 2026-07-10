# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Backend selection and session retry behavior: per-day retry cap,
configurable dev/review backends, silent-session fallback after consecutive
silent parks, and stale-session immediate retry for the claude CLI."""
from __future__ import annotations

import unittest
from typing import Optional
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    LABEL_DOCUMENTING,
    LABEL_IMPLEMENTING,
    LABEL_RESOLVING_CONFLICT,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _iso_hours_ago,
)

KEY_CODEX_SESSION_ID = "codex_session_id"
KEY_DEV_AGENT = "dev_agent"
KEY_DEV_RESUME_COUNT = "dev_resume_count"
KEY_DEV_SESSION_ID = "dev_session_id"
KEY_RETRY_COUNT = "retry_count"
KEY_SILENT_PARK_COUNT = "silent_park_count"

ENSURE_WORKTREE = "_ensure_worktree"
RUN_AGENT = "run_agent"
RESUME_SESSION_ID = "resume_session_id"

POISONED_SESSION = "poisoned-sess"
FRESH_SESSION = "fresh-sess"
LIVE_SESSION = "live-sess"
LEGACY_SESSION = "sess-legacy"

IMPLEMENT_PROMPT_FRAGMENT = "implement the thing"
FIX_PROMPT_FRAGMENT = "fix it"
RESUME_PROMPT_FRAGMENT = "resuming work on GitHub issue"
PROMPT_TOO_LONG_MESSAGE = "Prompt is too long"


class HandleImplementingRetryCapTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Bound the implementing loop with MAX_RETRIES_PER_DAY in pinned state.

    Resumes on human reply and recovered-worktree pushes are explicitly NOT
    counted; only fresh codex spawns consume the budget.
    """

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(8, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        if state:
            gh.seed_state(8, **state)
        return gh, issue

    def test_fourth_fresh_attempt_parks_before_codex(self) -> None:
        # Run three fresh attempts that each park as a question, then assert
        # the fourth tick parks before run_agent is called. Pin the cap at 3
        # so the test is hermetic against a `MAX_RETRIES_PER_DAY` env
        # override that would otherwise let the fourth tick spawn through.
        gh, issue = self._seeded()

        with patch.object(config, "MAX_RETRIES_PER_DAY", 3):
            # First three ticks: codex returns no commits + a question, parking on
            # awaiting_human. Each tick consumes one retry from the budget.
            for tick in range(3):
                self._run(
                    lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                    run_agent=_agent(last_message=f"q{tick}"),
                    has_new_commits=False,
                )
                # Clear the awaiting-human flag manually so the next tick takes
                # the fresh-spawn branch again (simulating that the human answered
                # but the agent still failed to commit). We do NOT update
                # last_action_comment_id, but we also drop awaiting_human so the
                # else branch runs.
                pinned_data = gh._pinned[8].data
                pinned_data["awaiting_human"] = False

            self.assertEqual(gh.pinned_data(8).get(KEY_RETRY_COUNT), 3)
            self.assertIsNotNone(gh.pinned_data(8).get("retry_window_start"))

            # Fourth tick: must park before codex spawns.
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="should not run"),
                has_new_commits=False,
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertTrue(gh.pinned_data(8).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("hit retry cap (3/day)", last_comment)
        self.assertIn("Window opened at", last_comment)

    def test_successful_commits_clear_counter(self) -> None:
        # Pre-seed near-cap state, then run a successful tick (commits + clean
        # tree + push succeeds). The PR-open path must clear the budget.
        gh, issue = self._seeded(
            retry_count=2,
            retry_window_start=_iso_hours_ago(1),
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        pinned_data = gh.pinned_data(8)
        self.assertEqual(pinned_data.get(KEY_RETRY_COUNT), 0)
        # window_start cleared back to falsy.
        self.assertFalse(pinned_data.get("retry_window_start"))
        self.assertEqual(len(gh.opened_prs), 1)

    def test_window_older_than_24h_resets_counter(self) -> None:
        # Cap exhausted but the window is 25h old: next fresh attempt opens a
        # new window with count=1 and codex actually spawns.
        gh, issue = self._seeded(
            retry_count=3,
            retry_window_start=_iso_hours_ago(25),
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="ask again"),
            has_new_commits=False,
        )

        mocks[RUN_AGENT].assert_called_once()
        pinned_data = gh.pinned_data(8)
        # Reset to 0 by the window-expired branch, then incremented to 1.
        self.assertEqual(pinned_data.get(KEY_RETRY_COUNT), 1)
        # Park message must NOT be the cap message.
        last_comment = gh.posted_comments[-1][1]
        self.assertNotIn("hit retry cap", last_comment)

    def test_human_resume_keeps_counter(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(9, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            9,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            retry_count=2,
            retry_window_start=_iso_hours_ago(1),
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        # Resume happened (codex was called once with the followup comment).
        mocks[RUN_AGENT].assert_called_once()
        # retry_count NOT incremented by the resume itself. The successful
        # _on_commits then clears it to 0.
        pinned_data = gh.pinned_data(9)
        self.assertEqual(pinned_data.get(KEY_RETRY_COUNT), 0)


class ConfigurableBackendTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The dev/review backends are picked from config, with the dev backend
    locked to whatever wrote `dev_session_id` (or legacy `codex_session_id`)
    so a config flip mid-flight does not break a resumable session.
    """

    def test_fresh_spawn_uses_dev_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", BACKEND_CLAUDE):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-fresh", last_message="done"),
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CLAUDE)
        pinned_data = gh.pinned_data(20)
        self.assertEqual(pinned_data[KEY_DEV_AGENT], BACKEND_CLAUDE)
        self.assertEqual(pinned_data[KEY_DEV_SESSION_ID], "sess-fresh")
        self.assertNotIn(KEY_CODEX_SESSION_ID, pinned_data)

    def test_reviewer_spawn_uses_review_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(21, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pr_number=21,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-21",
            dev_agent=BACKEND_CLAUDE,
            dev_session_id="dev-sess",
            review_round=0,
        )

        with patch.object(config, "REVIEW_AGENT", BACKEND_CODEX):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message=REVIEW_APPROVED_MESSAGE,
                ),
            )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CODEX)
        pinned_data = gh.pinned_data(21)
        self.assertEqual(pinned_data["review_agent"], BACKEND_CODEX)
        self.assertEqual(pinned_data["last_review_session_id"], "rev-sess")

    def test_dev_fix_uses_recorded_backend_not_config(self) -> None:
        # Issue locked to codex via pinned state; even if config flips to
        # claude, the validating dev-fix call must stay on codex.
        gh = FakeGitHubClient()
        issue = make_issue(22, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pr_number=22,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-22",
            dev_agent=BACKEND_CODEX,
            dev_session_id="dev-sess",
            review_round=0,
        )
        review = _agent(
            session_id="rev-sess",
            last_message="1. Tighten\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        with patch.object(config, "DEV_AGENT", BACKEND_CLAUDE), \
             patch.object(config, "REVIEW_AGENT", BACKEND_CLAUDE):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "aaa", "bbb"],
            )

        # Reviewer takes config; dev-fix takes pinned state.
        self.assertEqual(mocks[RUN_AGENT].call_count, 2)
        self.assertEqual(mocks[RUN_AGENT].call_args_list[0].args[0], BACKEND_CLAUDE)
        self.assertEqual(mocks[RUN_AGENT].call_args_list[1].args[0], BACKEND_CODEX)
        self.assertEqual(
            mocks[RUN_AGENT].call_args_list[1].kwargs.get(RESUME_SESSION_ID),
            "dev-sess",
        )

    def test_legacy_codex_session_resumes(self) -> None:
        # Pinned state predates the rollout: only `codex_session_id`. Resume
        # on human reply must stick with codex even when DEV_AGENT=claude.
        gh = FakeGitHubClient()
        issue = make_issue(23, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=1100, body="use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            23,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id=LEGACY_SESSION,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-23",
        )

        with patch.object(config, "DEV_AGENT", BACKEND_CLAUDE):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id=LEGACY_SESSION, last_message="ok"),
                has_new_commits=[True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CODEX)
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs.get(RESUME_SESSION_ID),
            LEGACY_SESSION,
        )
        # No proactive migration: legacy key stays put, no new keys written
        # by a resume (only fresh spawns write `dev_agent`/`dev_session_id`).
        pinned_data = gh.pinned_data(23)
        self.assertEqual(pinned_data.get(KEY_CODEX_SESSION_ID), LEGACY_SESSION)
        self.assertNotIn(KEY_DEV_AGENT, pinned_data)
        self.assertNotIn(KEY_DEV_SESSION_ID, pinned_data)


class SilentSessionResumeFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_resume_dev_with_text` drops a poisoned `dev_session_id` after
    `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent` parks
    and starts a fresh spawn instead. Without this fallback every human
    "retry" comment burns another fresh-spawn retry slot on the same dead
    session (the Claude rate-limit kill shape documented in #24).
    """

    def _seeded_issue(self, *, silent_park_count: int):
        gh = FakeGitHubClient()
        issue = make_issue(950, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            950,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=POISONED_SESSION,
            silent_park_count=silent_park_count,
        )
        return gh, issue

    def test_below_threshold_keeps_session(self) -> None:
        # One prior silent park is treated as a transient blip, not a
        # poisoned session: the resume still passes the original session
        # id and the streak counter stays put for the next park to bump.
        gh, issue = self._seeded_issue(silent_park_count=1)
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured[RESUME_SESSION_ID] = resume_session_id
            return _agent(session_id="ignored", last_message="ok")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            captured[RESUME_SESSION_ID], POISONED_SESSION,
            "below threshold the original session id must still be resumed",
        )
        # Session id and streak are not touched on the below-threshold path.
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), POISONED_SESSION)
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 1)

    def test_threshold_drops_and_persists_session(self) -> None:
        # `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive silent parks ==
        # session is poisoned. The resume must call `run_agent` with
        # `resume_session_id=None`, persist the new session id from the
        # result, and reset the silent-park streak so the new session
        # starts with a clean budget.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["agent"] = agent
            captured[RESUME_SESSION_ID] = resume_session_id
            return _agent(session_id=FRESH_SESSION, last_message="ok")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            captured[RESUME_SESSION_ID],
            "fresh spawn must drop the poisoned dev_session_id",
        )
        self.assertEqual(captured["agent"], BACKEND_CLAUDE)
        # New session id must be persisted so the next resume picks it up
        # instead of looking up an empty `dev_session_id` and re-spawning.
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), FRESH_SESSION)
        # Streak resets so a future blip doesn't drop the new session
        # immediately.
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 0)

    def test_empty_fresh_session_clears_pinned(self) -> None:
        # If the fresh spawn comes back without a `session_id` (agent
        # backend hiccup, missing file, etc.), the poisoned id must STILL
        # be removed from pinned state. Otherwise `_read_dev_session` on
        # the next tick returns the dead session and the resume loop
        # re-poisons itself.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(
                 workflow, RUN_AGENT,
                 lambda *args, **kwargs: _agent(session_id="", last_message=""),
             ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            state.get(KEY_DEV_SESSION_ID),
            "poisoned session id must be cleared even when the fresh "
            "spawn returns no session_id",
        )

    def test_fresh_spawn_clears_legacy_session(self) -> None:
        # An issue still on the legacy `codex_session_id` schema must
        # also have that field cleared on fresh-spawn -- otherwise the
        # next tick's `_read_dev_session` falls through the new keys
        # (because `dev_session_id` is None) and resurrects the poisoned
        # legacy id.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh = FakeGitHubClient()
        issue = make_issue(951, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            951,
            # Legacy schema: only `codex_session_id` is set, no `dev_agent`.
            codex_session_id="poisoned-legacy",
            silent_park_count=threshold,
        )
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["agent"] = agent
            captured[RESUME_SESSION_ID] = resume_session_id
            return _agent(session_id="fresh-legacy", last_message="ok")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (legacy).
        self.assertEqual(captured["agent"], BACKEND_CODEX)
        # Resume happened with no session id -- the poisoned legacy id
        # was dropped.
        self.assertIsNone(captured[RESUME_SESSION_ID])
        # Pinned state migrated to the new keys with the fresh session
        # id, and the legacy field is cleared.
        self.assertEqual(state.get(KEY_DEV_AGENT), BACKEND_CODEX)
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), "fresh-legacy")
        self.assertIsNone(state.get(KEY_CODEX_SESSION_ID))


class StaleSessionImmediateRetryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """When Claude's `--resume <sid>` lands on a transcript that no longer
    exists, the CLI prints `No conversation found with session ID` on stderr
    and exits with empty stdout. Without an immediate retry, the resume
    would park `agent_silent` and the `_SILENT_PARKS_BEFORE_FRESH_SESSION`
    threshold path would wait for a second silent park before recovering.
    `_resume_dev_with_text` short-circuits that by detecting the marker and
    retrying once with a cleared session id in the same worktree.
    """

    STALE_STDERR = "Error: No conversation found with session ID: poisoned-sess\n"

    def _seeded_issue(self, *, dev_agent: str = BACKEND_CLAUDE):
        gh = FakeGitHubClient()
        issue = make_issue(960, label=LABEL_RESOLVING_CONFLICT)
        gh.add_issue(issue)
        gh.seed_state(
            960,
            dev_agent=dev_agent,
            dev_session_id=POISONED_SESSION,
            silent_park_count=0,
        )
        return gh, issue

    def test_marker_detector_matches_known_phrasings(self) -> None:
        # The detector is keyed off lowercase substrings so phrasing tweaks
        # across Claude CLI releases still trip the recovery path.
        for stderr in (
            "Error: No conversation found with session ID: abc-123",
            "no conversation found with id abc",
            "No conversation with session ID xyz",
            "Conversation not found.",
            # Mixed casing still matches.
            "NO CONVERSATION FOUND WITH SESSION ID foo",
        ):
            with self.subTest(stderr=stderr):
                agent_result = _agent(session_id="", last_message="", stderr=stderr)
                self.assertTrue(
                    workflow._is_stale_session_failure(BACKEND_CLAUDE, agent_result),
                    f"{stderr!r} should be classified stale-session",
                )

    def test_marker_detector_ignores_unrelated_stderr(self) -> None:
        agent_result = _agent(
            session_id="", last_message="",
            stderr="Error: rate limited, please retry shortly",
        )
        self.assertFalse(
            workflow._is_stale_session_failure(BACKEND_CLAUDE, agent_result)
        )

    def test_marker_detector_only_triggers_for_claude(self) -> None:
        # Codex has no analogous stable marker today; the detector must
        # not misfire on a codex resume whose stderr happens to share text.
        agent_result = _agent(
            session_id="", last_message="",
            stderr="No conversation found with session ID: xyz",
        )
        self.assertFalse(
            workflow._is_stale_session_failure(BACKEND_CODEX, agent_result)
        )

    def test_claude_stale_session_retries_fresh(self) -> None:
        # Two calls expected: the first one resumes the poisoned session and
        # comes back with the marker; the second is a fresh spawn (no resume
        # session id) in the same worktree, with the new session id
        # persisted on success.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == POISONED_SESSION:
                return _agent(
                    session_id="", last_message="",
                    stderr=self.STALE_STDERR,
                )
            return _agent(session_id=FRESH_SESSION, last_message="ok")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, [POISONED_SESSION, None],
            "expected one resume with the poisoned id then one fresh spawn",
        )
        self.assertEqual(
            state.get(KEY_DEV_SESSION_ID), FRESH_SESSION,
            "fresh spawn's session id must be persisted",
        )
        self.assertEqual(state.get(KEY_DEV_AGENT), BACKEND_CLAUDE)
        self.assertIsNone(state.get(KEY_CODEX_SESSION_ID))
        # Silent-park streak resets so a future blip does not immediately
        # re-drop the new session.
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 0)

    def test_empty_stale_retry_clears_pinned(self) -> None:
        # If the fresh-spawn retry returns no session id (CLI hiccup), the
        # poisoned id must still be cleared from pinned state -- otherwise
        # the next tick's `_read_dev_session` resurrects it.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            if resume_session_id == POISONED_SESSION:
                return _agent(
                    session_id="", last_message="",
                    stderr=self.STALE_STDERR,
                )
            return _agent(session_id="", last_message="")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            state.get(KEY_DEV_SESSION_ID),
            "poisoned session id must be cleared even when the retry "
            "returns no session id",
        )

    def test_stale_retry_does_not_loop(self) -> None:
        # If the fresh spawn ALSO trips a stale-session marker something
        # deeper is broken (e.g. a misconfigured CLI). Surface that result
        # to the caller instead of looping infinitely.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(
                session_id="", last_message="", stderr=self.STALE_STDERR,
            )

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            _, agent_result, _ = workflow._resume_dev_with_text(
                gh, _TEST_SPEC, issue, state, "go",
            )

        self.assertEqual(
            calls, [POISONED_SESSION, None],
            "retry must be bounded to a single fresh spawn",
        )
        # Result reflects the still-failing retry; caller's downstream
        # `_on_question` will handle the agent_silent park.
        self.assertEqual(agent_result.stderr, self.STALE_STDERR)

    def test_codex_stale_stderr_no_immediate_retry(self) -> None:
        # Codex falls back to the silent-park-count path. A first resume
        # whose stderr happens to contain the marker must NOT retry
        # immediately for the codex backend.
        gh, issue = self._seeded_issue(dev_agent=BACKEND_CODEX)
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(
                session_id="", last_message="", stderr=self.STALE_STDERR,
            )

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, [POISONED_SESSION],
            "codex backend must NOT trigger the claude-only immediate retry",
        )
        # Poisoned id remains; the existing silent-park-count path is what
        # will eventually drop it.
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), POISONED_SESSION)


class ContextOverflowImmediateRetryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A claude `--resume` whose replayed transcript outgrew the model context
    window comes back with "Prompt is too long" and does no work. Resuming the
    same session only re-fails (every human "continue" / "decompose and
    continue" reply just appends to an already-over-budget transcript), so it
    is treated as a poisoned session: drop the id and retry once as a fresh
    spawn in the same worktree, exactly like the stale-session path.
    """

    OVERFLOW_MSG = PROMPT_TOO_LONG_MESSAGE

    def _seeded_issue(self, *, dev_agent: str = BACKEND_CLAUDE):
        gh = FakeGitHubClient()
        issue = make_issue(961, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            961,
            dev_agent=dev_agent,
            dev_session_id=POISONED_SESSION,
            silent_park_count=0,
        )
        return gh, issue

    def test_detector_matches_known_phrasings(self) -> None:
        # The detector keys off a lowercase PREFIX of the last agent message
        # so the bare phrase, a token-count suffix, and mixed casing all trip
        # recovery.
        for last_message in (
            PROMPT_TOO_LONG_MESSAGE,
            "prompt is too long: 215000 tokens > 200000 maximum",
            "PROMPT IS TOO LONG",
            "Input is too long",
            "input length and `max_tokens` exceed context limit: ...",
        ):
            with self.subTest(last_message=last_message):
                agent_result = _agent(session_id="", last_message=last_message)
                self.assertTrue(
                    workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result),
                    f"{last_message!r} should be classified context overflow",
                )

    def test_overflow_detector_matches_stderr(self) -> None:
        # The CLI may print the diagnostic to stderr without emitting a result
        # event (empty last_message); a substring match still trips recovery.
        agent_result = _agent(
            session_id="", last_message="",
            stderr="API Error: prompt is too long: 210000 tokens > 200000",
        )
        self.assertTrue(
            workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result)
        )

    def test_detector_ignores_midanswer_phrase(self) -> None:
        # An agent that merely MENTIONS the phrase inside a normal answer must
        # not be misclassified -- last_message is matched as a prefix only.
        agent_result = _agent(
            session_id="sess-1",
            last_message="I split the work because the prompt is too long "
            "to handle in one pass; see the sub-issues.",
        )
        self.assertFalse(
            workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result)
        )

    def test_overflow_detector_ignores_unrelated(self) -> None:
        agent_result = _agent(
            session_id="sess-1", last_message="done",
            stderr="Error: rate limited, please retry shortly",
        )
        self.assertFalse(
            workflow._is_context_overflow_failure(BACKEND_CLAUDE, agent_result)
        )

    def test_detector_only_triggers_for_claude(self) -> None:
        agent_result = _agent(session_id="", last_message=self.OVERFLOW_MSG)
        self.assertFalse(
            workflow._is_context_overflow_failure(BACKEND_CODEX, agent_result)
        )

    def test_poisoned_covers_stale_and_overflow(self) -> None:
        stale = _agent(
            session_id="", last_message="",
            stderr="No conversation found with session ID: x",
        )
        overflow = _agent(session_id="", last_message=self.OVERFLOW_MSG)
        unrelated = _agent(session_id="sess-1", last_message="a question?")
        self.assertTrue(workflow._is_poisoned_session_failure(BACKEND_CLAUDE, stale))
        self.assertTrue(workflow._is_poisoned_session_failure(BACKEND_CLAUDE, overflow))
        self.assertFalse(
            workflow._is_poisoned_session_failure(BACKEND_CLAUDE, unrelated)
        )

    def test_claude_overflow_retries_fresh(self) -> None:
        # First call resumes the poisoned (overflowed) session and returns the
        # marker; second is a fresh spawn (no resume id) whose new session id
        # is persisted on success.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == POISONED_SESSION:
                return _agent(session_id="", last_message=self.OVERFLOW_MSG)
            return _agent(session_id=FRESH_SESSION, last_message="ok")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, [POISONED_SESSION, None],
            "expected one resume with the poisoned id then one fresh spawn",
        )
        self.assertEqual(
            state.get(KEY_DEV_SESSION_ID), FRESH_SESSION,
            "fresh spawn's session id must be persisted",
        )
        self.assertEqual(state.get(KEY_SILENT_PARK_COUNT), 0)

    def test_empty_overflow_retry_clears_pinned(self) -> None:
        # If the fresh-spawn retry returns no session id, the poisoned id must
        # still be cleared so the next tick's `_read_dev_session` cannot
        # resurrect the overflowed session.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            if resume_session_id == POISONED_SESSION:
                return _agent(session_id="", last_message=self.OVERFLOW_MSG)
            return _agent(session_id="", last_message="")

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(state.get(KEY_DEV_SESSION_ID))

    def test_repeated_overflow_does_not_loop(self) -> None:
        # A fresh spawn that ALSO overflows (issue body so large even a small
        # prompt exceeds the window) is bounded to a single retry; the still-
        # failing result is surfaced so the caller's `_on_question` parks it
        # for human intervention (split the issue) rather than looping.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(session_id="", last_message=self.OVERFLOW_MSG)

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            _, agent_result, _ = workflow._resume_dev_with_text(
                gh, _TEST_SPEC, issue, state, "go",
            )

        self.assertEqual(
            calls, [POISONED_SESSION, None],
            "retry must be bounded to a single fresh spawn",
        )
        self.assertEqual(agent_result.last_message, self.OVERFLOW_MSG)

    def test_codex_overflow_no_immediate_retry(self) -> None:
        # Codex has no analogous stable marker; a codex resume whose message
        # happens to share the text must not trip the claude-only retry.
        gh, issue = self._seeded_issue(dev_agent=BACKEND_CODEX)
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(session_id="", last_message=self.OVERFLOW_MSG)

        with patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, [POISONED_SESSION],
            "codex backend must NOT trigger the claude-only immediate retry",
        )
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), POISONED_SESSION)


class SessionLimitMessageClassifierTest(unittest.TestCase):
    """A session/usage-quota notice returned as the CLI's FINAL message is a
    retryable session-failure, not a real agent question. `_on_question` keys
    the retryable `agent_silent` park off `_is_session_limit_message`, so the
    classifier must accept the known phrasings (including a curly apostrophe)
    as a prefix while ignoring a plain question or a mid-answer mention.
    """

    def test_matches_known_session_limit_phrasings(self) -> None:
        for last_message in (
            # The #705 shape, verbatim.
            "You've hit your session limit · resets 7pm (Asia/Novosibirsk)",
            # Curly apostrophe still hits (normalized before matching).
            "You’ve hit your session limit · resets 7pm",
            "You've reached your usage limit for now",
            "Claude AI usage limit reached|1712345678",
            # Mixed casing / leading whitespace still trip the prefix match.
            "  CLAUDE USAGE LIMIT REACHED",
        ):
            with self.subTest(last_message=last_message):
                agent_result = _agent(session_id="sess-1", last_message=last_message)
                self.assertTrue(
                    workflow._is_session_limit_message(agent_result),
                    f"{last_message!r} should classify as a session limit",
                )

    def test_ignores_question_and_midanswer_mention(self) -> None:
        for last_message in (
            "",
            "Should I prefer ruff or black for this?",
            # A dev discussing the concept mid-answer must not be caught --
            # the marker is matched as a prefix, not anywhere in the body.
            "I added a note about the session limit handling in fixing.py.",
        ):
            with self.subTest(last_message=last_message):
                agent_result = _agent(session_id="sess-1", last_message=last_message)
                self.assertFalse(
                    workflow._is_session_limit_message(agent_result),
                    f"{last_message!r} must not classify as a session limit",
                )


class ProactiveSessionRotationTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`--resume` replays the whole transcript every time, so a session resumed
    past `DEV_SESSION_MAX_RESUMES` is retired proactively and rebuilt fresh from
    durable state -- capping context creep BEFORE it overflows the window. Each
    resume charges one against the per-session `dev_resume_count`; a fresh spawn
    resets it and is re-grounded with the issue requirements + branch pointer.
    """

    def _seeded_issue(self, *, resume_count: int = 0, dev_agent: str = BACKEND_CLAUDE,
                      sid: str = LIVE_SESSION):
        gh = FakeGitHubClient()
        issue = make_issue(962, label="in_review", body=IMPLEMENT_PROMPT_FRAGMENT)
        gh.add_issue(issue)
        gh.seed_state(
            962,
            dev_agent=dev_agent,
            dev_session_id=sid,
            silent_park_count=0,
            dev_resume_count=resume_count,
        )
        return gh, issue

    def _run_resume(self, gh, issue, *, fake_run, threshold):
        state = gh.read_pinned_state(issue)
        with patch.object(config, "DEV_SESSION_MAX_RESUMES", threshold), \
             patch.object(workflow, ENSURE_WORKTREE, lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, fake_run):
            wt, agent_result, _ = workflow._resume_dev_with_text(
                gh, _TEST_SPEC, issue, state, FIX_PROMPT_FRAGMENT,
            )
        return state, agent_result

    def test_below_threshold_bumps_and_keeps_session(self) -> None:
        gh, issue = self._seeded_issue(resume_count=3)
        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(session_id=LIVE_SESSION, last_message="done")

        state, _ = self._run_resume(gh, issue, fake_run=fake_run, threshold=10)

        self.assertEqual(calls, [LIVE_SESSION], "below budget must resume in place")
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT), 4,
            "each resume charges one against the per-session budget",
        )
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), LIVE_SESSION)

    def test_threshold_rotates_to_fresh_spawn(self) -> None:
        gh, issue = self._seeded_issue(resume_count=10)
        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(session_id=FRESH_SESSION, last_message="ok")

        state, _ = self._run_resume(gh, issue, fake_run=fake_run, threshold=10)

        self.assertEqual(
            calls, [None],
            "budget reached must fresh-spawn (no resume id), not resume",
        )
        self.assertEqual(state.get(KEY_DEV_SESSION_ID), FRESH_SESSION)
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT), 0,
            "rotation resets the budget for the new session",
        )

    def test_zero_threshold_disables_rotation(self) -> None:
        gh, issue = self._seeded_issue(resume_count=99)
        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(session_id=LIVE_SESSION, last_message="done")

        state, _ = self._run_resume(gh, issue, fake_run=fake_run, threshold=0)

        self.assertEqual(
            calls, [LIVE_SESSION],
            "0 = unbounded: must keep resuming regardless of count",
        )
        self.assertEqual(state.get(KEY_DEV_RESUME_COUNT), 100)

    def test_rotation_prompt_is_regrounded(self) -> None:
        # The rotated fresh spawn has no transcript, so its prompt must carry
        # the re-grounding preamble (issue body + branch pointer) AND the
        # stage followup appended after it.
        gh, issue = self._seeded_issue(resume_count=5)
        prompts: list[str] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            prompts.append(prompt)
            return _agent(session_id=FRESH_SESSION, last_message="ok")

        self._run_resume(gh, issue, fake_run=fake_run, threshold=5)

        self.assertEqual(len(prompts), 1)
        self.assertIn(RESUME_PROMPT_FRAGMENT, prompts[0])
        self.assertIn(IMPLEMENT_PROMPT_FRAGMENT, prompts[0], "issue body re-grounds")
        self.assertTrue(
            prompts[0].rstrip().endswith(FIX_PROMPT_FRAGMENT),
            "stage followup must be appended after the preamble",
        )

    def test_resume_in_place_prompt_has_no_preamble(self) -> None:
        # A live resume already carries the issue context in its transcript, so
        # the bare followup is sent -- no re-grounding, no token duplication.
        gh, issue = self._seeded_issue(resume_count=1)
        prompts: list[str] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            prompts.append(prompt)
            return _agent(session_id=LIVE_SESSION, last_message="done")

        self._run_resume(gh, issue, fake_run=fake_run, threshold=10)

        self.assertEqual(prompts, [FIX_PROMPT_FRAGMENT])

    def test_overflow_recovery_is_regrounded(self) -> None:
        # Ties the two features together: an overflowed ("Prompt is too long")
        # resume drops the session and the recovery fresh spawn -- like the
        # rotation spawn -- is re-grounded with the preamble.
        gh, issue = self._seeded_issue(resume_count=0, sid=POISONED_SESSION)
        seen: list[tuple[Optional[str], str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            seen.append((resume_session_id, prompt))
            if resume_session_id == POISONED_SESSION:
                return _agent(session_id="", last_message=PROMPT_TOO_LONG_MESSAGE)
            return _agent(session_id=FRESH_SESSION, last_message="ok")

        self._run_resume(gh, issue, fake_run=fake_run, threshold=10)

        self.assertEqual([sid for sid, _ in seen], [POISONED_SESSION, None])
        self.assertEqual(seen[0][1], FIX_PROMPT_FRAGMENT, "the initial resume sends the bare followup")
        self.assertIn(
            RESUME_PROMPT_FRAGMENT, seen[1][1],
            "the overflow-recovery fresh spawn must be re-grounded",
        )

    def test_preamble_includes_requirements_branch(self) -> None:
        issue = make_issue(963, body="do the work", title="My Issue")
        text = workflow._build_fresh_respawn_preamble(
            _TEST_SPEC, issue, "@alice: please add tests", [_TEST_SPEC])
        self.assertIn("do the work", text)
        self.assertIn("@alice: please add tests", text)
        self.assertIn("git diff", text, "must point the fresh agent at the branch")
        self.assertIn("do NOT restart", text)

    def test_no_session_entry_spawns_and_persists(self) -> None:
        # `dev_agent` is pinned but `dev_session_id` is absent -- e.g. an
        # earlier backend hiccup that committed work but surfaced no session
        # id. There is nothing to resume, so the spawn must open a NEW session
        # (no resume id), re-ground it, persist the returned id, and zero the
        # stale resume count -- otherwise later resumes find no live session
        # and fresh-spawn from scratch every tick.
        gh = FakeGitHubClient()
        issue = make_issue(964, label=LABEL_DOCUMENTING, body=IMPLEMENT_PROMPT_FRAGMENT)
        gh.add_issue(issue)
        gh.seed_state(
            964,
            dev_agent=BACKEND_CLAUDE,
            silent_park_count=0,
            dev_resume_count=7,
        )
        seen: list[tuple[Optional[str], str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            seen.append((resume_session_id, prompt))
            return _agent(session_id="hiccup-recovered", last_message="ok")

        state, _ = self._run_resume(gh, issue, fake_run=fake_run, threshold=10)

        self.assertEqual(
            [sid for sid, _ in seen], [None],
            "no live session -> fresh spawn with no resume id",
        )
        self.assertIn(
            RESUME_PROMPT_FRAGMENT, seen[0][1],
            "the fresh spawn must be re-grounded",
        )
        self.assertEqual(
            state.get(KEY_DEV_SESSION_ID), "hiccup-recovered",
            "the returned session id must be persisted, not dropped",
        )
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT), 0,
            "the new session starts its resume budget from zero",
        )

    def test_missing_session_empty_result_keeps_clear(self) -> None:
        # The recovery spawn ALSO returns no session id (another hiccup): the
        # session stays unpinned so the next tick fresh-spawns again rather
        # than resuming a phantom id, and the resume budget is not charged.
        gh = FakeGitHubClient()
        issue = make_issue(965, label=LABEL_DOCUMENTING, body=IMPLEMENT_PROMPT_FRAGMENT)
        gh.add_issue(issue)
        gh.seed_state(
            965,
            dev_agent=BACKEND_CLAUDE,
            silent_park_count=0,
            dev_resume_count=2,
        )
        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(session_id="", last_message="")

        state, _ = self._run_resume(gh, issue, fake_run=fake_run, threshold=10)

        self.assertEqual(calls, [None], "still a fresh spawn, no resume id")
        self.assertIsNone(state.get(KEY_DEV_SESSION_ID))
        self.assertEqual(
            state.get(KEY_DEV_RESUME_COUNT), 2,
            "a no-session fresh spawn must not charge the resume budget",
        )
