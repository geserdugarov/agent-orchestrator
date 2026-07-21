# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Issue #67: the pinned `dev_agent` / `decomposer_agent` / `review_agent`
fields must store the full configured agent command (backend + CLI args),
not just the parsed backend, and resumes / poisoned-session drops must
preserve the recorded spec across config flips."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

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
    KEY_AWAITING_HUMAN,
    LABEL_DECOMPOSING,
    LABEL_IMPLEMENTING,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    REVIEW_CHANGES_REQUESTED_MESSAGE,
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
)

CODEX_SPEC = 'codex -m gpt-5.5 -c \'model_reasoning_effort="xhigh"\''
CODEX_ARGS = (
    "-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"',
)
CLAUDE_SPEC = "claude --model claude-opus-4-7"
CLAUDE_ARGS = ("--model", "claude-opus-4-7")

RUN_AGENT = "run_agent"
EXTRA_ARGS = "extra_args"
RESUME_SESSION_ID = "resume_session_id"
DEV_AGENT_KEY = "dev_agent"
DEV_SESSION_ID = "dev_session_id"
DECOMPOSER_AGENT = "decomposer_agent"
OK_MESSAGE = "ok"
TEST_AUTHOR = "alice"
UNCHANGED_SHA = "aaa"

FRESH_DEV_ISSUE = 67001
RESUMED_DEV_ISSUE = 67002
LEGACY_BACKEND_ISSUE = 67003
LEGACY_SESSION_ISSUE = 67004
POISONED_DROP_ISSUE = 67005
POISONED_LEGACY_ISSUE = 67006
FRESH_REVIEW_ISSUE = 67010
REVIEW_COMMENT_ISSUE = 67011
CHANGE_REQUEST_ISSUE = 67012
REVIEW_PROMPT_ISSUE = 67013
FRESH_DECOMPOSER_ISSUE = 67020
RESUMED_DECOMPOSER_ISSUE = 67021
NO_SESSION_DEV_ISSUE = 67030
ENV_FLIP_DEV_ISSUE = 67031
NO_SESSION_DECOMPOSER_ISSUE = 67032
ENV_FLIP_DECOMPOSER_ISSUE = 67033

LEGACY_REPLY_ID = 2100
LEGACY_ACTION_WATERMARK = 2000
CODEX_SESSION_REPLY_ID = 2200
DECOMPOSER_PARK_ID = 3000
DECOMPOSER_REPLY_ID = 3010
DEV_ENV_FLIP_REPLY_ID = 4000
DECOMPOSER_ENV_FLIP_REPLY_ID = 4100


class _FullSpecFixtureMixin(_PatchedWorkflowMixin):
    """Issue #67: the pinned `dev_agent`/`decomposer_agent`/`review_agent`
    fields must store the full configured agent command (backend + CLI
    args), not just the parsed backend. This protects in-flight issues
    from a mid-flight env flip rewriting which CLI args run on subsequent
    resumes, and keeps legacy bare-backend pinned values and
    `codex_session_id` working unchanged.
    """

    # --- helpers ---------------------------------------------------------

    def _patch_dev_config(
        self, spec: str, backend: str, args: tuple[str, ...],
    ):
        return [
            patch.object(config, "DEV_AGENT_SPEC", spec),
            patch.object(config, "DEV_AGENT", backend),
            patch.object(config, "DEV_AGENT_ARGS", args),
        ]

    def _patch_review_config(
        self, spec: str, backend: str, args: tuple[str, ...],
    ):
        return [
            patch.object(config, "REVIEW_AGENT_SPEC", spec),
            patch.object(config, "REVIEW_AGENT", backend),
            patch.object(config, "REVIEW_AGENT_ARGS", args),
        ]

    def _patch_decompose_config(
        self, spec: str, backend: str, args: tuple[str, ...],
    ):
        return [
            patch.object(config, "DECOMPOSE_AGENT_SPEC", spec),
            patch.object(config, "DECOMPOSE_AGENT", backend),
            patch.object(config, "DECOMPOSE_AGENT_ARGS", args),
        ]

    def _enter(self, patches):
        for config_patch in patches:
            config_patch.start()
            self.addCleanup(config_patch.stop)


class FullSpecDevPersistenceTest(unittest.TestCase, _FullSpecFixtureMixin):

    def test_fresh_spawn_stores_spec_with_args(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_DEV_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        self._enter(self._patch_dev_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="sess-67001", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(mocks[RUN_AGENT].call_args.args[0], BACKEND_CODEX)
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs[EXTRA_ARGS],
            CODEX_ARGS,
        )
        pinned_data = gh.pinned_data(FRESH_DEV_ISSUE)
        # Full spec verbatim, NOT just the parsed backend.
        self.assertEqual(pinned_data[DEV_AGENT_KEY], CODEX_SPEC)
        self.assertEqual(pinned_data[DEV_SESSION_ID], "sess-67001")

    def test_resume_uses_spec_after_env_flip(self) -> None:
        # Pinned state recorded codex+args. Even after a config flip to
        # plain claude, the resume MUST keep the recorded backend AND
        # the recorded args -- the new backend's CLI would reject codex
        # flags, and silently dropping them on a resume changes what
        # the agent actually sees mid-flight.
        gh = FakeGitHubClient()
        issue = make_issue(RESUMED_DEV_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            RESUMED_DEV_ISSUE,
            pr_number=RESUMED_DEV_ISSUE,
            branch=_issue_branch(RESUMED_DEV_ISSUE),
            dev_agent=CODEX_SPEC,
            dev_session_id="dev-67002",
            review_round=0,
        )
        # Config now points to plain claude (no args).
        self._enter(self._patch_dev_config(CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS))
        # Reviewer too -- we just want the dev-fix call to use the stored spec.
        self._enter(self._patch_review_config(BACKEND_CLAUDE, BACKEND_CLAUDE, ()))

        review = _agent(
            session_id="rev-67002",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )
        dev_fix = _agent(session_id="dev-67002", last_message="fixed")

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=[UNCHANGED_SHA, UNCHANGED_SHA, "bbb"],
        )

        # Two calls: reviewer (claude per config) then dev-fix (codex per
        # pinned state).
        self.assertEqual(mocks[RUN_AGENT].call_count, 2)
        dev_call = mocks[RUN_AGENT].call_args_list[1]
        self.assertEqual(dev_call.args[0], BACKEND_CODEX)
        self.assertEqual(dev_call.kwargs.get(RESUME_SESSION_ID), "dev-67002")
        # Args came from the stored spec, NOT the current config.
        self.assertEqual(dev_call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)

    def test_legacy_bare_backend_still_works(self) -> None:
        # An issue pinned with the pre-#67 bare-backend value (`"codex"`)
        # must still resume on codex with no args -- that is what those
        # deployments had at the time the session was spawned.
        gh = FakeGitHubClient()
        issue = make_issue(LEGACY_BACKEND_ISSUE, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=LEGACY_REPLY_ID, body="please retry", user=FakeUser(TEST_AUTHOR))
        )
        gh.add_issue(issue)
        gh.seed_state(
            LEGACY_BACKEND_ISSUE,
            awaiting_human=True,
            last_action_comment_id=LEGACY_ACTION_WATERMARK,
            dev_agent=BACKEND_CODEX,  # legacy bare-backend pinned form.
            dev_session_id="dev-legacy-spec",
            branch=_issue_branch(LEGACY_BACKEND_ISSUE),
        )

        # Flip current config to a spec with args -- which the resume must IGNORE.
        self._enter(self._patch_dev_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ))

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="dev-legacy-spec", last_message=OK_MESSAGE),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        # No args -- the legacy pinned form had none.
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), ())
        # No proactive migration -- a resume does NOT rewrite `dev_agent`.
        self.assertEqual(gh.pinned_data(LEGACY_BACKEND_ISSUE).get(DEV_AGENT_KEY), BACKEND_CODEX)

    def test_legacy_codex_session_resumes_no_args(self) -> None:
        # The pre-rollout schema only had `codex_session_id`. Resume MUST
        # use codex regardless of any current config flip, and MUST pass
        # no args (the spec at the time was bare codex).
        gh = FakeGitHubClient()
        issue = make_issue(LEGACY_SESSION_ISSUE, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=CODEX_SESSION_REPLY_ID, body="retry", user=FakeUser(TEST_AUTHOR))
        )
        gh.add_issue(issue)
        gh.seed_state(
            LEGACY_SESSION_ISSUE,
            awaiting_human=True,
            last_action_comment_id=LEGACY_REPLY_ID,
            codex_session_id="sess-legacy-67004",
            branch=_issue_branch(LEGACY_SESSION_ISSUE),
        )

        self._enter(self._patch_dev_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ))

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="sess-legacy-67004", last_message=OK_MESSAGE),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(RESUME_SESSION_ID), "sess-legacy-67004")
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), ())

    def test_poisoned_drop_keeps_full_spec(self) -> None:
        # After hitting the silent-park threshold, the resume drops the
        # poisoned session id and starts a fresh spawn. The stored
        # full spec MUST be preserved so the fresh spawn uses the same
        # backend+args (a poisoned session is a transcript problem,
        # not a backend-selection problem).
        gh = FakeGitHubClient()
        issue = make_issue(POISONED_DROP_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            POISONED_DROP_ISSUE,
            dev_agent=CODEX_SPEC,
            dev_session_id="poisoned-67005",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(
            return_value=_agent(session_id="fresh-67005", last_message=OK_MESSAGE)
        )

        with patch.object(workflow, "_ensure_worktree", lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, run_agent):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        call = run_agent.call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertIsNone(call.kwargs.get(RESUME_SESSION_ID))
        # Critical: the args from the stored spec survived the drop.
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        # Stored spec is untouched -- not overwritten with the bare backend.
        self.assertEqual(state.get(DEV_AGENT_KEY), CODEX_SPEC)
        self.assertEqual(state.get(DEV_SESSION_ID), "fresh-67005")

    def test_poisoned_legacy_session_pins_codex(self) -> None:
        # Legacy schema (only `codex_session_id`): a poisoned-session drop
        # must pin `dev_agent="codex"` before clearing the legacy field,
        # so a subsequent env flip to claude cannot retroactively switch
        # the backend.
        gh = FakeGitHubClient()
        issue = make_issue(POISONED_LEGACY_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            POISONED_LEGACY_ISSUE,
            codex_session_id="poisoned-legacy-67006",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        self._enter(self._patch_dev_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ))

        run_agent = MagicMock(
            return_value=_agent(
                session_id="fresh-legacy-67006", last_message=OK_MESSAGE,
            )
        )

        with patch.object(workflow, "_ensure_worktree", lambda spec, issue_number, **_: _FAKE_WT), \
             patch.object(workflow, RUN_AGENT, run_agent):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (the legacy implicit spec).
        call = run_agent.call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), ())
        # Migrated to the new key with the legacy backend pinned, legacy
        # field cleared.
        self.assertEqual(state.get(DEV_AGENT_KEY), BACKEND_CODEX)
        self.assertEqual(state.get(DEV_SESSION_ID), "fresh-legacy-67006")
        self.assertIsNone(state.get("codex_session_id"))


class FullSpecReviewerPersistenceTest(
    unittest.TestCase, _FullSpecFixtureMixin,
):

    def test_fresh_reviewer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_REVIEW_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            FRESH_REVIEW_ISSUE,
            pr_number=FRESH_REVIEW_ISSUE,
            branch=_issue_branch(FRESH_REVIEW_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id="dev-67010",
            review_round=0,
        )

        self._enter(self._patch_review_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67010",
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
        )

        # Reviewer ran with backend+args from current config.
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        # And the FULL spec is what gets persisted -- not just the backend.
        pinned_data = gh.pinned_data(FRESH_REVIEW_ISSUE)
        self.assertEqual(pinned_data["review_agent"], CODEX_SPEC)
        self.assertEqual(pinned_data["last_review_session_id"], "rev-67010")

    def test_reviewer_comments_use_config_backend(self) -> None:
        # Issue #67: reviewer trace/comments must not hardcode `codex` --
        # when the operator configures claude as the reviewer, the PR
        # comments must say so. We test both approval and CHANGES_REQUESTED
        # paths since both posted hardcoded text before the fix.
        gh = FakeGitHubClient()
        issue = make_issue(REVIEW_COMMENT_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            REVIEW_COMMENT_ISSUE,
            pr_number=REVIEW_COMMENT_ISSUE,
            branch=_issue_branch(REVIEW_COMMENT_ISSUE),
            dev_agent=BACKEND_CODEX,
            dev_session_id="dev-67011",
            review_round=0,
        )

        self._enter(self._patch_review_config(BACKEND_CLAUDE, BACKEND_CLAUDE, ()))

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67011",
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
        )

        approval_comments = [
            body for (_, body) in gh.posted_pr_comments
            if "review approved" in body
        ]
        self.assertEqual(len(approval_comments), 1, approval_comments)
        self.assertIn("claude review approved", approval_comments[0])
        self.assertNotIn("codex review approved", approval_comments[0])

    def test_change_request_uses_config_backend(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(CHANGE_REQUEST_ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            CHANGE_REQUEST_ISSUE,
            pr_number=CHANGE_REQUEST_ISSUE,
            branch=_issue_branch(CHANGE_REQUEST_ISSUE),
            dev_agent=BACKEND_CODEX,
            dev_session_id="dev-67012",
            review_round=0,
        )

        self._enter(self._patch_review_config(BACKEND_CLAUDE, BACKEND_CLAUDE, ()))

        review = _agent(
            session_id="rev-67012",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )
        dev_fix = _agent(session_id="dev-67012", last_message="fixed")

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=[UNCHANGED_SHA, UNCHANGED_SHA, "bbb"],
        )

        bodies = [body for (_, body) in gh.posted_pr_comments]
        review_bodies = [body for body in bodies if "review (round" in body]
        self.assertEqual(len(review_bodies), 1, review_bodies)
        self.assertIn("claude review (round", review_bodies[0])
        self.assertNotIn("codex review (round", review_bodies[0])

    def test_review_prompt_names_dev_backend(self) -> None:
        # The reviewer prompt's intro line described the implementer as
        # "a separate codex session" before the fix, which is wrong when
        # claude is the dev backend. Build the prompt directly and
        # assert it reflects the dev backend.
        prompt = workflow._build_review_prompt(
            _TEST_SPEC,
            make_issue(REVIEW_PROMPT_ISSUE),
            "",
            [_TEST_SPEC],
            dev_backend=BACKEND_CLAUDE,
        )
        self.assertIn("A separate claude session", prompt)
        self.assertNotIn("A separate codex session", prompt)


class FullSpecDecomposerPersistenceTest(
    unittest.TestCase, _FullSpecFixtureMixin,
):

    def test_fresh_decomposer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        self._enter(self._patch_decompose_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))

        # Manifest "single" -- simplest successful decompose path.
        manifest = (
            "OK\n\n"
            "```orchestrator-manifest\n"
            '{"decision": "single", "rationale": "fits one context"}\n'
            "```\n"
        )
        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-67020", last_message=manifest),
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        pinned_data = gh.pinned_data(FRESH_DECOMPOSER_ISSUE)
        # Full spec, not the bare backend.
        self.assertEqual(pinned_data[DECOMPOSER_AGENT], CODEX_SPEC)
        self.assertEqual(pinned_data["decomposer_session_id"], "dec-67020")

    def test_decomposer_resume_uses_stored_spec(self) -> None:
        # Pinned with the full codex spec. After DECOMPOSE_AGENT flips to
        # claude, the awaiting-human resume must still resume on codex
        # with the codex args, not retarget the next call to claude.
        gh = FakeGitHubClient()
        issue = make_issue(RESUMED_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING, comments=[
            FakeComment(id=DECOMPOSER_PARK_ID, body="park", user=FakeUser("orchestrator")),
            FakeComment(id=DECOMPOSER_REPLY_ID, body="please split", user=FakeUser(TEST_AUTHOR)),
        ])
        gh.add_issue(issue)
        gh.seed_state(
            RESUMED_DECOMPOSER_ISSUE,
            awaiting_human=True,
            last_action_comment_id=DECOMPOSER_PARK_ID,
            decomposer_agent=CODEX_SPEC,
            decomposer_session_id="dec-67021",
        )

        self._enter(self._patch_decompose_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-67021",
                last_message=(
                    "OK\n\n"
                    "```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "ok"}\n'
                    "```\n"
                ),
            ),
        )

        # Resume call used the stored backend AND the stored args.
        call = mocks[RUN_AGENT].call_args
        self.assertEqual(call.args[0], BACKEND_CODEX)
        self.assertEqual(call.kwargs.get(RESUME_SESSION_ID), "dec-67021")
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
        # Stored spec untouched.
        self.assertEqual(
            gh.pinned_data(RESUMED_DECOMPOSER_ISSUE).get(DECOMPOSER_AGENT), CODEX_SPEC,
        )

    def test_legacy_bare_decomposer_still_works(self) -> None:
        # `decomposer_agent="codex"` (no args) is the legacy pinned form;
        # it must continue to round-trip cleanly to `("codex", "codex", ())`.
        spec, backend, args, sid = workflow._read_decomposer_session(
            workflow.PinnedState(
                data={DECOMPOSER_AGENT: BACKEND_CODEX, "decomposer_session_id": "sid-x"},
            )
        )
        self.assertEqual(spec, BACKEND_CODEX)
        self.assertEqual(backend, BACKEND_CODEX)
        self.assertEqual(args, ())
        self.assertEqual(sid, "sid-x")


class FullSpecSessionReaderTest(unittest.TestCase, _FullSpecFixtureMixin):

    def test_read_dev_session_round_trips_full_spec(self) -> None:
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={
                    DEV_AGENT_KEY: CODEX_SPEC,
                    DEV_SESSION_ID: "sid-y",
                },
            )
        )
        self.assertEqual(spec, CODEX_SPEC)
        self.assertEqual(backend, BACKEND_CODEX)
        self.assertEqual(args, CODEX_ARGS)
        self.assertEqual(sid, "sid-y")

    def test_read_dev_session_legacy_codex_session_id(self) -> None:
        # Even with a custom DEV_AGENT_SPEC in config, a legacy
        # codex_session_id-only state must yield codex with no args.
        self._enter(self._patch_dev_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ))
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={"codex_session_id": "legacy-sid"},
            )
        )
        self.assertEqual(spec, BACKEND_CODEX)
        self.assertEqual(backend, BACKEND_CODEX)
        self.assertEqual(args, ())
        self.assertEqual(sid, "legacy-sid")

    def test_unseeded_dev_session_uses_config(self) -> None:
        self._enter(self._patch_dev_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ))
        spec, backend, args, sid = workflow._read_dev_session(workflow.PinnedState())
        self.assertEqual(spec, CLAUDE_SPEC)
        self.assertEqual(backend, BACKEND_CLAUDE)
        self.assertEqual(args, CLAUDE_ARGS)
        self.assertIsNone(sid)


class FullSpecDevNoSessionTest(unittest.TestCase, _FullSpecFixtureMixin):

    def test_dev_spec_pinned_without_session_id(self) -> None:
        # A fresh dev spawn that produces commits but no session id (a
        # codex `-o` file the agent left empty, an unparseable claude
        # JSONL line, etc.) MUST still pin `dev_agent` to the full
        # configured spec. Without this, a subsequent `DEV_AGENT` flip
        # would silently retarget the next validating dev-fix resume
        # at a backend that never ran on this issue.
        gh = FakeGitHubClient()
        issue = make_issue(NO_SESSION_DEV_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        self._enter(self._patch_dev_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))

        # Empty session_id: backend hiccup, but the worktree got commits.
        self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        pinned_data = gh.pinned_data(NO_SESSION_DEV_ISSUE)
        self.assertEqual(
            pinned_data.get(DEV_AGENT_KEY), CODEX_SPEC,
            "dev_agent must be pinned to the full spec even when the "
            "spawn returns no session_id",
        )
        # session_id was empty, so the legacy field stays absent.
        self.assertNotIn(DEV_SESSION_ID, pinned_data)

    def test_dev_env_flip_resumes_recorded_spec(self) -> None:
        # Reviewer-requested scenario: spawn returns no session id but
        # commits/parks land. Operator then flips `DEV_AGENT` between
        # ticks. The next resume MUST stick with the spec that was
        # actually running, not retarget at the new config.
        gh = FakeGitHubClient()
        issue = make_issue(ENV_FLIP_DEV_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        # First tick: codex spawn, no session id, agent question -> park
        # awaiting human.
        self._enter(self._patch_dev_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))
        self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="", last_message="need input"),
            has_new_commits=False,
        )
        pinned_data = gh.pinned_data(ENV_FLIP_DEV_ISSUE)
        self.assertEqual(pinned_data.get(DEV_AGENT_KEY), CODEX_SPEC)
        self.assertTrue(pinned_data.get(KEY_AWAITING_HUMAN))

        # Second tick: operator flipped `DEV_AGENT` to claude AND
        # provided new args; a human reply lands on the issue. The
        # resume MUST stick with codex+args from pinned state, NOT
        # retarget at the current claude config.
        issue.comments.append(
            FakeComment(id=DEV_ENV_FLIP_REPLY_ID, body="ok proceed", user=FakeUser(TEST_AUTHOR))
        )

        # Switch config to claude (different backend + different args).
        # `_enter` schedules cleanup; start a fresh override block.
        for config_patch in self._patch_dev_config(CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS):
            config_patch.start()
            self.addCleanup(config_patch.stop)

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="sess-67031", last_message="done"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
            # The user-content-drift branch (which fires here because the
            # human's "ok proceed" comment changes the issue hash from
            # tick 1) snapshots HEAD before and after the resume to decide
            # whether THIS resume committed, so we need two SHA values
            # (different ones, so the post-resume "did the agent commit"
            # check goes through `_on_commits`). The drift path still
            # calls `_resume_dev_with_text` with the recorded codex spec,
            # so the call-arg assertions below still hold.
            head_shas=["before-sha", "after-sha"],
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(
            call.args[0], BACKEND_CODEX,
            "resume must stick with the spec the first tick recorded, "
            "NOT the new DEV_AGENT after the flip",
        )
        self.assertEqual(
            call.kwargs.get(EXTRA_ARGS), CODEX_ARGS,
            "stored codex args must survive across the config flip",
        )



class FullSpecDecomposerNoSessionTest(
    unittest.TestCase, _FullSpecFixtureMixin,
):
    def test_decomposer_spec_pinned_no_session(self) -> None:
        # Same reviewer concern, decomposer side: a fresh decomposer
        # that emits a manifest without surfacing a session id (or
        # parks awaiting human after a question) must still pin
        # `decomposer_agent` to the full spec.
        gh = FakeGitHubClient()
        issue = make_issue(NO_SESSION_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        self._enter(self._patch_decompose_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))

        # No session_id, question-only output -> awaiting human park.
        # The spec must still land in pinned state so a later config
        # flip cannot retarget the awaiting-human resume.
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="please clarify"),
        )

        pinned_data = gh.pinned_data(NO_SESSION_DECOMPOSER_ISSUE)
        self.assertEqual(
            pinned_data.get(DECOMPOSER_AGENT), CODEX_SPEC,
            "decomposer_agent must be pinned to the full spec even "
            "when the spawn returns no session_id",
        )
        self.assertTrue(pinned_data.get(KEY_AWAITING_HUMAN))
        self.assertNotIn("decomposer_session_id", pinned_data)

    def test_decomposer_env_flip_resumes_spec(self) -> None:
        # Same config-flip scenario, decomposer side: the awaiting-
        # human resume must stick with the recorded spec.
        gh = FakeGitHubClient()
        issue = make_issue(ENV_FLIP_DECOMPOSER_ISSUE, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        # First tick: codex decomposer, no session id, parks on a
        # clarification request.
        self._enter(self._patch_decompose_config(
            CODEX_SPEC, BACKEND_CODEX, CODEX_ARGS,
        ))
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="", last_message="please clarify scope",
            ),
        )
        pinned_data = gh.pinned_data(ENV_FLIP_DECOMPOSER_ISSUE)
        self.assertEqual(pinned_data.get(DECOMPOSER_AGENT), CODEX_SPEC)
        self.assertTrue(pinned_data.get(KEY_AWAITING_HUMAN))

        # Human replies; operator flips `DECOMPOSE_AGENT` to claude
        # between ticks. The resume must stick with codex+args.
        issue.comments.append(
            FakeComment(id=DECOMPOSER_ENV_FLIP_REPLY_ID, body="single is fine", user=FakeUser(TEST_AUTHOR))
        )
        for config_patch in self._patch_decompose_config(
            CLAUDE_SPEC, BACKEND_CLAUDE, CLAUDE_ARGS,
        ):
            config_patch.start()
            self.addCleanup(config_patch.stop)

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-67033",
                last_message=(
                    "OK\n\n"
                    "```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "fits one context"}\n'
                    "```\n"
                ),
            ),
        )

        call = mocks[RUN_AGENT].call_args
        self.assertEqual(
            call.args[0], BACKEND_CODEX,
            "decomposer resume must stick with the spec the first tick "
            "recorded, NOT the new DECOMPOSE_AGENT after the flip",
        )
        self.assertEqual(call.kwargs.get(EXTRA_ARGS), CODEX_ARGS)
