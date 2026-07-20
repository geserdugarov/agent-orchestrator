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


class _FullSpecFixtureMixin(_PatchedWorkflowMixin):
    """Issue #67: the pinned `dev_agent`/`decomposer_agent`/`review_agent`
    fields must store the full configured agent command (backend + CLI
    args), not just the parsed backend. This protects in-flight issues
    from a mid-flight env flip rewriting which CLI args run on subsequent
    resumes, and keeps legacy bare-backend pinned values and
    `codex_session_id` working unchanged.
    """

    _CODEX_SPEC = 'codex -m gpt-5.5 -c \'model_reasoning_effort="xhigh"\''
    _CODEX_ARGS = (
        "-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"',
    )
    _CLAUDE_SPEC = "claude --model claude-opus-4-7"
    _CLAUDE_ARGS = ("--model", "claude-opus-4-7")

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
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class FullSpecDevPersistenceTest(unittest.TestCase, _FullSpecFixtureMixin):

    def test_fresh_spawn_stores_spec_with_args(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67001, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        self._enter(self._patch_dev_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="sess-67001", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs["extra_args"],
            self._CODEX_ARGS,
        )
        data = gh.pinned_data(67001)
        # Full spec verbatim, NOT just the parsed backend.
        self.assertEqual(data["dev_agent"], self._CODEX_SPEC)
        self.assertEqual(data["dev_session_id"], "sess-67001")

    def test_resume_uses_spec_after_env_flip(self) -> None:
        # Pinned state recorded codex+args. Even after a config flip to
        # plain claude, the resume MUST keep the recorded backend AND
        # the recorded args -- the new backend's CLI would reject codex
        # flags, and silently dropping them on a resume changes what
        # the agent actually sees mid-flight.
        gh = FakeGitHubClient()
        issue = make_issue(67002, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            67002,
            pr_number=67002,
            branch=_issue_branch(67002),
            dev_agent=self._CODEX_SPEC,
            dev_session_id="dev-67002",
            review_round=0,
        )
        # Config now points to plain claude (no args).
        self._enter(self._patch_dev_config(self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS))
        # Reviewer too -- we just want the dev-fix call to use the stored spec.
        self._enter(self._patch_review_config("claude", "claude", ()))

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
            head_shas=["aaa", "aaa", "bbb"],
        )

        # Two calls: reviewer (claude per config) then dev-fix (codex per
        # pinned state).
        self.assertEqual(mocks["run_agent"].call_count, 2)
        dev_call = mocks["run_agent"].call_args_list[1]
        self.assertEqual(dev_call.args[0], "codex")
        self.assertEqual(dev_call.kwargs.get("resume_session_id"), "dev-67002")
        # Args came from the stored spec, NOT the current config.
        self.assertEqual(dev_call.kwargs.get("extra_args"), self._CODEX_ARGS)

    def test_legacy_bare_backend_still_works(self) -> None:
        # An issue pinned with the pre-#67 bare-backend value (`"codex"`)
        # must still resume on codex with no args -- that is what those
        # deployments had at the time the session was spawned.
        gh = FakeGitHubClient()
        issue = make_issue(67003, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=2100, body="please retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            67003,
            awaiting_human=True,
            last_action_comment_id=2000,
            dev_agent="codex",  # legacy bare-backend pinned form.
            dev_session_id="dev-legacy-spec",
            branch=_issue_branch(67003),
        )

        # Flip current config to a spec with args -- which the resume must IGNORE.
        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="dev-legacy-spec", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        # No args -- the legacy pinned form had none.
        self.assertEqual(call.kwargs.get("extra_args"), ())
        # No proactive migration -- a resume does NOT rewrite `dev_agent`.
        self.assertEqual(gh.pinned_data(67003).get("dev_agent"), "codex")

    def test_legacy_codex_session_resumes_no_args(self) -> None:
        # The pre-rollout schema only had `codex_session_id`. Resume MUST
        # use codex regardless of any current config flip, and MUST pass
        # no args (the spec at the time was bare codex).
        gh = FakeGitHubClient()
        issue = make_issue(67004, label=LABEL_IMPLEMENTING)
        issue.comments.append(
            FakeComment(id=2200, body="retry", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            67004,
            awaiting_human=True,
            last_action_comment_id=2100,
            codex_session_id="sess-legacy-67004",
            branch=_issue_branch(67004),
        )

        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="sess-legacy-67004", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "sess-legacy-67004")
        self.assertEqual(call.kwargs.get("extra_args"), ())

    def test_poisoned_drop_keeps_full_spec(self) -> None:
        # After hitting the silent-park threshold, the resume drops the
        # poisoned session id and starts a fresh spawn. The stored
        # full spec MUST be preserved so the fresh spawn uses the same
        # backend+args (a poisoned session is a transcript problem,
        # not a backend-selection problem).
        gh = FakeGitHubClient()
        issue = make_issue(67005, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            67005,
            dev_agent=self._CODEX_SPEC,
            dev_session_id="poisoned-67005",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        run_agent = MagicMock(
            return_value=_agent(session_id="fresh-67005", last_message="ok")
        )

        with patch.object(workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT), \
             patch.object(workflow, "run_agent", run_agent):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        call = run_agent.call_args
        self.assertEqual(call.args[0], "codex")
        self.assertIsNone(call.kwargs.get("resume_session_id"))
        # Critical: the args from the stored spec survived the drop.
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        # Stored spec is untouched -- not overwritten with the bare backend.
        self.assertEqual(state.get("dev_agent"), self._CODEX_SPEC)
        self.assertEqual(state.get("dev_session_id"), "fresh-67005")

    def test_poisoned_legacy_session_pins_codex(self) -> None:
        # Legacy schema (only `codex_session_id`): a poisoned-session drop
        # must pin `dev_agent="codex"` before clearing the legacy field,
        # so a subsequent env flip to claude cannot retroactively switch
        # the backend.
        gh = FakeGitHubClient()
        issue = make_issue(67006, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            67006,
            codex_session_id="poisoned-legacy-67006",
            silent_park_count=workflow._SILENT_PARKS_BEFORE_FRESH_SESSION,
        )
        state = gh.read_pinned_state(issue)

        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))

        run_agent = MagicMock(
            return_value=_agent(
                session_id="fresh-legacy-67006", last_message="ok",
            )
        )

        with patch.object(workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT), \
             patch.object(workflow, "run_agent", run_agent):
            workflow._resume_dev_with_text(gh, _TEST_SPEC, issue, state, "go")

        # Backend stays locked to codex (the legacy implicit spec).
        call = run_agent.call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("extra_args"), ())
        # Migrated to the new key with the legacy backend pinned, legacy
        # field cleared.
        self.assertEqual(state.get("dev_agent"), "codex")
        self.assertEqual(state.get("dev_session_id"), "fresh-legacy-67006")
        self.assertIsNone(state.get("codex_session_id"))


class FullSpecReviewerPersistenceTest(
    unittest.TestCase, _FullSpecFixtureMixin,
):

    def test_fresh_reviewer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67010, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            67010,
            pr_number=67010,
            branch=_issue_branch(67010),
            dev_agent="claude",
            dev_session_id="dev-67010",
            review_round=0,
        )

        self._enter(self._patch_review_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="rev-67010",
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
        )

        # Reviewer ran with backend+args from current config.
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        # And the FULL spec is what gets persisted -- not just the backend.
        data = gh.pinned_data(67010)
        self.assertEqual(data["review_agent"], self._CODEX_SPEC)
        self.assertEqual(data["last_review_session_id"], "rev-67010")

    def test_reviewer_comments_use_config_backend(self) -> None:
        # Issue #67: reviewer trace/comments must not hardcode `codex` --
        # when the operator configures claude as the reviewer, the PR
        # comments must say so. We test both approval and CHANGES_REQUESTED
        # paths since both posted hardcoded text before the fix.
        gh = FakeGitHubClient()
        issue = make_issue(67011, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            67011,
            pr_number=67011,
            branch=_issue_branch(67011),
            dev_agent="codex",
            dev_session_id="dev-67011",
            review_round=0,
        )

        self._enter(self._patch_review_config("claude", "claude", ()))

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
        issue = make_issue(67012, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        gh.seed_state(
            67012,
            pr_number=67012,
            branch=_issue_branch(67012),
            dev_agent="codex",
            dev_session_id="dev-67012",
            review_round=0,
        )

        self._enter(self._patch_review_config("claude", "claude", ()))

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
            head_shas=["aaa", "aaa", "bbb"],
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
            make_issue(67013),
            "",
            [_TEST_SPEC],
            dev_backend="claude",
        )
        self.assertIn("A separate claude session", prompt)
        self.assertNotIn("A separate codex session", prompt)


class FullSpecDecomposerPersistenceTest(
    unittest.TestCase, _FullSpecFixtureMixin,
):

    def test_fresh_decomposer_spawn_stores_full_spec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(67020, label="decomposing")
        gh.add_issue(issue)

        self._enter(self._patch_decompose_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
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

        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        data = gh.pinned_data(67020)
        # Full spec, not the bare backend.
        self.assertEqual(data["decomposer_agent"], self._CODEX_SPEC)
        self.assertEqual(data["decomposer_session_id"], "dec-67020")

    def test_decomposer_resume_uses_stored_spec(self) -> None:
        # Pinned with the full codex spec. After DECOMPOSE_AGENT flips to
        # claude, the awaiting-human resume must still resume on codex
        # with the codex args, not retarget the next call to claude.
        gh = FakeGitHubClient()
        issue = make_issue(67021, label="decomposing", comments=[
            FakeComment(id=3000, body="park", user=FakeUser("orchestrator")),
            FakeComment(id=3010, body="please split", user=FakeUser("alice")),
        ])
        gh.add_issue(issue)
        gh.seed_state(
            67021,
            awaiting_human=True,
            last_action_comment_id=3000,
            decomposer_agent=self._CODEX_SPEC,
            decomposer_session_id="dec-67021",
        )

        self._enter(self._patch_decompose_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
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
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dec-67021")
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
        # Stored spec untouched.
        self.assertEqual(
            gh.pinned_data(67021).get("decomposer_agent"), self._CODEX_SPEC,
        )

    def test_legacy_bare_decomposer_still_works(self) -> None:
        # `decomposer_agent="codex"` (no args) is the legacy pinned form;
        # it must continue to round-trip cleanly to `("codex", "codex", ())`.
        spec, backend, args, sid = workflow._read_decomposer_session(
            workflow.PinnedState(
                data={"decomposer_agent": "codex", "decomposer_session_id": "sid-x"},
            )
        )
        self.assertEqual(spec, "codex")
        self.assertEqual(backend, "codex")
        self.assertEqual(args, ())
        self.assertEqual(sid, "sid-x")


class FullSpecSessionReaderTest(unittest.TestCase, _FullSpecFixtureMixin):

    def test_read_dev_session_round_trips_full_spec(self) -> None:
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={
                    "dev_agent": self._CODEX_SPEC,
                    "dev_session_id": "sid-y",
                },
            )
        )
        self.assertEqual(spec, self._CODEX_SPEC)
        self.assertEqual(backend, "codex")
        self.assertEqual(args, self._CODEX_ARGS)
        self.assertEqual(sid, "sid-y")

    def test_read_dev_session_legacy_codex_session_id(self) -> None:
        # Even with a custom DEV_AGENT_SPEC in config, a legacy
        # codex_session_id-only state must yield codex with no args.
        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))
        spec, backend, args, sid = workflow._read_dev_session(
            workflow.PinnedState(
                data={"codex_session_id": "legacy-sid"},
            )
        )
        self.assertEqual(spec, "codex")
        self.assertEqual(backend, "codex")
        self.assertEqual(args, ())
        self.assertEqual(sid, "legacy-sid")

    def test_unseeded_dev_session_uses_config(self) -> None:
        self._enter(self._patch_dev_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ))
        spec, backend, args, sid = workflow._read_dev_session(workflow.PinnedState())
        self.assertEqual(spec, self._CLAUDE_SPEC)
        self.assertEqual(backend, "claude")
        self.assertEqual(args, self._CLAUDE_ARGS)
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
        issue = make_issue(67030, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        self._enter(self._patch_dev_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        # Empty session_id: backend hiccup, but the worktree got commits.
        self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        data = gh.pinned_data(67030)
        self.assertEqual(
            data.get("dev_agent"), self._CODEX_SPEC,
            "dev_agent must be pinned to the full spec even when the "
            "spawn returns no session_id",
        )
        # session_id was empty, so the legacy field stays absent.
        self.assertNotIn("dev_session_id", data)

    def test_dev_env_flip_resumes_recorded_spec(self) -> None:
        # Reviewer-requested scenario: spawn returns no session id but
        # commits/parks land. Operator then flips `DEV_AGENT` between
        # ticks. The next resume MUST stick with the spec that was
        # actually running, not retarget at the new config.
        gh = FakeGitHubClient()
        issue = make_issue(67031, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)

        # First tick: codex spawn, no session id, agent question -> park
        # awaiting human.
        self._enter(self._patch_dev_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))
        self._run_implementing(
            gh, issue,
            run_agent=_agent(session_id="", last_message="need input"),
            has_new_commits=False,
        )
        data = gh.pinned_data(67031)
        self.assertEqual(data.get("dev_agent"), self._CODEX_SPEC)
        self.assertTrue(data.get("awaiting_human"))

        # Second tick: operator flipped `DEV_AGENT` to claude AND
        # provided new args; a human reply lands on the issue. The
        # resume MUST stick with codex+args from pinned state, NOT
        # retarget at the current claude config.
        issue.comments.append(
            FakeComment(id=4000, body="ok proceed", user=FakeUser("alice"))
        )

        # Switch config to claude (different backend + different args).
        # `_enter` schedules cleanup; start a fresh override block.
        for p in self._patch_dev_config(self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS):
            p.start()
            self.addCleanup(p.stop)

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

        call = mocks["run_agent"].call_args
        self.assertEqual(
            call.args[0], "codex",
            "resume must stick with the spec the first tick recorded, "
            "NOT the new DEV_AGENT after the flip",
        )
        self.assertEqual(
            call.kwargs.get("extra_args"), self._CODEX_ARGS,
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
        issue = make_issue(67032, label="decomposing")
        gh.add_issue(issue)

        self._enter(self._patch_decompose_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))

        # No session_id, question-only output -> awaiting human park.
        # The spec must still land in pinned state so a later config
        # flip cannot retarget the awaiting-human resume.
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="", last_message="please clarify"),
        )

        data = gh.pinned_data(67032)
        self.assertEqual(
            data.get("decomposer_agent"), self._CODEX_SPEC,
            "decomposer_agent must be pinned to the full spec even "
            "when the spawn returns no session_id",
        )
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn("decomposer_session_id", data)

    def test_decomposer_env_flip_resumes_spec(self) -> None:
        # Same config-flip scenario, decomposer side: the awaiting-
        # human resume must stick with the recorded spec.
        gh = FakeGitHubClient()
        issue = make_issue(67033, label="decomposing")
        gh.add_issue(issue)

        # First tick: codex decomposer, no session id, parks on a
        # clarification request.
        self._enter(self._patch_decompose_config(
            self._CODEX_SPEC, "codex", self._CODEX_ARGS,
        ))
        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="", last_message="please clarify scope",
            ),
        )
        data = gh.pinned_data(67033)
        self.assertEqual(data.get("decomposer_agent"), self._CODEX_SPEC)
        self.assertTrue(data.get("awaiting_human"))

        # Human replies; operator flips `DECOMPOSE_AGENT` to claude
        # between ticks. The resume must stick with codex+args.
        issue.comments.append(
            FakeComment(id=4100, body="single is fine", user=FakeUser("alice"))
        )
        for p in self._patch_decompose_config(
            self._CLAUDE_SPEC, "claude", self._CLAUDE_ARGS,
        ):
            p.start()
            self.addCleanup(p.stop)

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

        call = mocks["run_agent"].call_args
        self.assertEqual(
            call.args[0], "codex",
            "decomposer resume must stick with the spec the first tick "
            "recorded, NOT the new DECOMPOSE_AGENT after the flip",
        )
        self.assertEqual(call.kwargs.get("extra_args"), self._CODEX_ARGS)
