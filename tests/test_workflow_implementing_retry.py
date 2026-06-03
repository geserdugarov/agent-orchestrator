# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Backend selection and session retry behavior: per-day retry cap,
configurable dev/review backends, silent-session fallback after consecutive
silent parks, and stale-session immediate retry for the claude CLI."""
from __future__ import annotations

import os
import unittest
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _iso_hours_ago,
)


class HandleImplementingRetryCapTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Bound the implementing loop with MAX_RETRIES_PER_DAY in pinned state.

    Resumes on human reply and recovered-worktree pushes are explicitly NOT
    counted; only fresh codex spawns consume the budget.
    """

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(8, label="implementing")
        gh.add_issue(issue)
        if state:
            gh.seed_state(8, **state)
        return gh, issue

    def test_fourth_fresh_attempt_in_window_is_parked_before_codex(self) -> None:
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
                data = gh._pinned[8].data
                data["awaiting_human"] = False

            self.assertEqual(gh.pinned_data(8).get("retry_count"), 3)
            self.assertIsNotNone(gh.pinned_data(8).get("retry_window_start"))

            # Fourth tick: must park before codex spawns.
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="should not run"),
                has_new_commits=False,
            )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(8).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("hit retry cap (3/day)", last_comment)
        self.assertIn("Window opened at", last_comment)

    def test_successful_on_commits_clears_retry_counter(self) -> None:
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

        data = gh.pinned_data(8)
        self.assertEqual(data.get("retry_count"), 0)
        # window_start cleared back to falsy.
        self.assertFalse(data.get("retry_window_start"))
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

        mocks["run_agent"].assert_called_once()
        data = gh.pinned_data(8)
        # Reset to 0 by the window-expired branch, then incremented to 1.
        self.assertEqual(data.get("retry_count"), 1)
        # Park message must NOT be the cap message.
        last_comment = gh.posted_comments[-1][1]
        self.assertNotIn("hit retry cap", last_comment)

    def test_awaiting_human_resume_does_not_increment_counter(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(9, label="implementing")
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
        mocks["run_agent"].assert_called_once()
        # retry_count NOT incremented by the resume itself. The successful
        # _on_commits then clears it to 0.
        data = gh.pinned_data(9)
        self.assertEqual(data.get("retry_count"), 0)


class ConfigurableBackendTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The dev/review backends are picked from config, with the dev backend
    locked to whatever wrote `dev_session_id` (or legacy `codex_session_id`)
    so a config flip mid-flight does not break a resumable session.
    """

    def test_fresh_implementing_spawn_uses_dev_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label="implementing")
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-fresh", last_message="done"),
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "claude")
        data = gh.pinned_data(20)
        self.assertEqual(data["dev_agent"], "claude")
        self.assertEqual(data["dev_session_id"], "sess-fresh")
        self.assertNotIn("codex_session_id", data)

    def test_reviewer_spawn_uses_review_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(21, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pr_number=21,
            branch="orchestrator/issue-21",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
        )

        with patch.object(config, "REVIEW_AGENT", "codex"):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message="LGTM\n\nVERDICT: APPROVED",
                ),
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        data = gh.pinned_data(21)
        self.assertEqual(data["review_agent"], "codex")
        self.assertEqual(data["last_review_session_id"], "rev-sess")

    def test_dev_fix_uses_recorded_dev_backend_not_current_config(self) -> None:
        # Issue locked to codex via pinned state; even if config flips to
        # claude, the validating dev-fix call must stay on codex.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pr_number=22,
            branch="orchestrator/issue-22",
            dev_agent="codex",
            dev_session_id="dev-sess",
            review_round=0,
        )
        review = _agent(
            session_id="rev-sess",
            last_message="1. Tighten\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        with patch.object(config, "DEV_AGENT", "claude"), \
             patch.object(config, "REVIEW_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "aaa", "bbb"],
            )

        # Reviewer takes config; dev-fix takes pinned state.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(mocks["run_agent"].call_args_list[0].args[0], "claude")
        self.assertEqual(mocks["run_agent"].call_args_list[1].args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args_list[1].kwargs.get("resume_session_id"),
            "dev-sess",
        )

    def test_legacy_codex_session_id_resumes_with_codex(self) -> None:
        # Pinned state predates the rollout: only `codex_session_id`. Resume
        # on human reply must stick with codex even when DEV_AGENT=claude.
        gh = FakeGitHubClient()
        issue = make_issue(23, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            23,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-legacy",
            branch="orchestrator/issue-23",
        )

        with patch.object(config, "DEV_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-legacy", last_message="ok"),
                has_new_commits=[True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "sess-legacy",
        )
        # No proactive migration: legacy key stays put, no new keys written
        # by a resume (only fresh spawns write `dev_agent`/`dev_session_id`).
        data = gh.pinned_data(23)
        self.assertEqual(data.get("codex_session_id"), "sess-legacy")
        self.assertNotIn("dev_agent", data)
        self.assertNotIn("dev_session_id", data)


class SilentSessionResumeFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_resume_dev_with_text` drops a poisoned `dev_session_id` after
    `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent` parks
    and starts a fresh spawn instead. Without this fallback every human
    "retry" comment burns another fresh-spawn retry slot on the same dead
    session (the Claude rate-limit kill shape documented in #24).
    """

    def _seeded_issue(self, *, silent_park_count: int):
        gh = FakeGitHubClient()
        issue = make_issue(950, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            950,
            dev_agent="claude",
            dev_session_id="poisoned-sess",
            silent_park_count=silent_park_count,
        )
        return gh, issue

    def test_below_threshold_keeps_existing_session_id(self) -> None:
        # One prior silent park is treated as a transient blip, not a
        # poisoned session: the resume still passes the original session
        # id and the streak counter stays put for the next park to bump.
        gh, issue = self._seeded_issue(silent_park_count=1)
        state = gh.read_pinned_state(issue)

        captured: dict = {}

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            captured["resume_session_id"] = resume_session_id
            return _agent(session_id="ignored", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            captured["resume_session_id"], "poisoned-sess",
            "below threshold the original session id must still be resumed",
        )
        # Session id and streak are not touched on the below-threshold path.
        self.assertEqual(state.get("dev_session_id"), "poisoned-sess")
        self.assertEqual(state.get("silent_park_count"), 1)

    def test_at_threshold_drops_session_and_persists_fresh_one(self) -> None:
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
            captured["resume_session_id"] = resume_session_id
            return _agent(session_id="fresh-sess", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            captured["resume_session_id"],
            "fresh spawn must drop the poisoned dev_session_id",
        )
        self.assertEqual(captured["agent"], "claude")
        # New session id must be persisted so the next resume picks it up
        # instead of looking up an empty `dev_session_id` and re-spawning.
        self.assertEqual(state.get("dev_session_id"), "fresh-sess")
        # Streak resets so a future blip doesn't drop the new session
        # immediately.
        self.assertEqual(state.get("silent_park_count"), 0)

    def test_fresh_spawn_with_empty_session_id_still_clears_pinned(self) -> None:
        # If the fresh spawn comes back without a `session_id` (agent
        # backend hiccup, missing file, etc.), the poisoned id must STILL
        # be removed from pinned state. Otherwise `_read_dev_session` on
        # the next tick returns the dead session and the resume loop
        # re-poisons itself.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh, issue = self._seeded_issue(silent_park_count=threshold)
        state = gh.read_pinned_state(issue)

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(
                 workflow, "run_agent",
                 lambda *a, **kw: _agent(session_id="", last_message=""),
             ):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            state.get("dev_session_id"),
            "poisoned session id must be cleared even when the fresh "
            "spawn returns no session_id",
        )

    def test_fresh_spawn_clears_legacy_codex_session_id_too(self) -> None:
        # An issue still on the legacy `codex_session_id` schema must
        # also have that field cleared on fresh-spawn -- otherwise the
        # next tick's `_read_dev_session` falls through the new keys
        # (because `dev_session_id` is None) and resurrects the poisoned
        # legacy id.
        threshold = workflow._SILENT_PARKS_BEFORE_FRESH_SESSION
        gh = FakeGitHubClient()
        issue = make_issue(951, label="implementing")
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
            captured["resume_session_id"] = resume_session_id
            return _agent(session_id="fresh-legacy", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (legacy).
        self.assertEqual(captured["agent"], "codex")
        # Resume happened with no session id -- the poisoned legacy id
        # was dropped.
        self.assertIsNone(captured["resume_session_id"])
        # Pinned state migrated to the new keys with the fresh session
        # id, and the legacy field is cleared.
        self.assertEqual(state.get("dev_agent"), "codex")
        self.assertEqual(state.get("dev_session_id"), "fresh-legacy")
        self.assertIsNone(state.get("codex_session_id"))


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

    def _seeded_issue(self, *, dev_agent: str = "claude"):
        gh = FakeGitHubClient()
        issue = make_issue(960, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(
            960,
            dev_agent=dev_agent,
            dev_session_id="poisoned-sess",
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
                result = _agent(session_id="", last_message="", stderr=stderr)
                self.assertTrue(
                    workflow._is_stale_session_failure("claude", result),
                    f"{stderr!r} should be classified stale-session",
                )

    def test_marker_detector_ignores_unrelated_stderr(self) -> None:
        result = _agent(
            session_id="", last_message="",
            stderr="Error: rate limited, please retry shortly",
        )
        self.assertFalse(
            workflow._is_stale_session_failure("claude", result)
        )

    def test_marker_detector_only_triggers_for_claude(self) -> None:
        # Codex has no analogous stable marker today; the detector must
        # not misfire on a codex resume whose stderr happens to share text.
        result = _agent(
            session_id="", last_message="",
            stderr="No conversation found with session ID: xyz",
        )
        self.assertFalse(
            workflow._is_stale_session_failure("codex", result)
        )

    def test_claude_stale_session_retries_once_with_fresh_spawn(self) -> None:
        # Two calls expected: the first one resumes the poisoned session and
        # comes back with the marker; the second is a fresh spawn (no resume
        # session id) in the same worktree, with the new session id
        # persisted on success.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="",
                    stderr=self.STALE_STDERR,
                )
            return _agent(session_id="fresh-sess", last_message="ok")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, ["poisoned-sess", None],
            "expected one resume with the poisoned id then one fresh spawn",
        )
        self.assertEqual(
            state.get("dev_session_id"), "fresh-sess",
            "fresh spawn's session id must be persisted",
        )
        self.assertEqual(state.get("dev_agent"), "claude")
        self.assertIsNone(state.get("codex_session_id"))
        # Silent-park streak resets so a future blip does not immediately
        # re-drop the new session.
        self.assertEqual(state.get("silent_park_count"), 0)

    def test_stale_session_retry_clears_pinned_even_if_retry_empty(self) -> None:
        # If the fresh-spawn retry returns no session id (CLI hiccup), the
        # poisoned id must still be cleared from pinned state -- otherwise
        # the next tick's `_read_dev_session` resurrects it.
        gh, issue = self._seeded_issue()
        state = gh.read_pinned_state(issue)

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="",
                    stderr=self.STALE_STDERR,
                )
            return _agent(session_id="", last_message="")

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertIsNone(
            state.get("dev_session_id"),
            "poisoned session id must be cleared even when the retry "
            "returns no session id",
        )

    def test_stale_session_retry_does_not_loop_when_retry_also_stale(self) -> None:
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

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            _, result = workflow._resume_dev_with_text(
                gh, _TEST_SPEC, issue, state, "go",
            )

        self.assertEqual(
            calls, ["poisoned-sess", None],
            "retry must be bounded to a single fresh spawn",
        )
        # Result reflects the still-failing retry; caller's downstream
        # `_on_question` will handle the agent_silent park.
        self.assertEqual(result.stderr, self.STALE_STDERR)

    def test_codex_stale_stderr_does_not_trigger_immediate_retry(self) -> None:
        # Codex falls back to the silent-park-count path. A first resume
        # whose stderr happens to contain the marker must NOT retry
        # immediately for the codex backend.
        gh, issue = self._seeded_issue(dev_agent="codex")
        state = gh.read_pinned_state(issue)

        calls: list[Optional[str]] = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            return _agent(
                session_id="", last_message="", stderr=self.STALE_STDERR,
            )

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", fake_run):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        self.assertEqual(
            calls, ["poisoned-sess"],
            "codex backend must NOT trigger the claude-only immediate retry",
        )
        # Poisoned id remains; the existing silent-park-count path is what
        # will eventually drop it.
        self.assertEqual(state.get("dev_session_id"), "poisoned-sess")
