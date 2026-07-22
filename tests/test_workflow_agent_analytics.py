# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`_run_agent_tracked` analytics record: one well-formed JSONL line per
agent exit carrying spec/role/session/duration/usage context (and never
prompts, raw streams, or secrets). Includes the spec-fallback model path
for codex stdout that omits the model field, and the disabled-sink knob."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from orchestrator import analytics, config, usage, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    EVENT_AGENT_EXIT,
    EVENT_AGENT_SPAWN,
    EVENT_AGENT_TRAJECTORY,
    EVENT_SKILL_TRIGGERED,
    LABEL_IMPLEMENTING,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    ROLE_DEVELOPER,
    ROLE_REVIEWER,
    TEST_BASE_BRANCH,
    TEST_REPO_SLUG,
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _analytics_records,
)


_TYPE_KEY = "type"
_USAGE_KEY = "usage"
_INPUT_TOKENS_KEY = "input_tokens"
_OUTPUT_TOKENS_KEY = "output_tokens"
_MESSAGE_KEY = "message"
_ID_KEY = "id"
_RESULT_KEY = "result"
_CONTENT_KEY = "content"
_SKILL_KEY = "skill"
_EVENT_KEY = "event"
_STAGE_KEY = "stage"
_AGENT_ROLE_KEY = "agent_role"
_COST_USD_KEY = "cost_usd"
_ANALYTICS_FILENAME = "analytics.jsonl"
_ANALYTICS_PATH_ATTR = "ANALYTICS_LOG_PATH"
_TRAJECTORY_PATH_ATTR = "TRAJECTORY_LOG_PATH"
_TRACK_SKILLS_ATTR = "TRACK_SKILL_TRIGGERS"
_RUN_AGENT_ATTR = "run_agent"
_CLAUDE_MODEL = "claude-sonnet-4-6"
_CODEX_MODEL = "gpt-5-codex"
_IGNORED_PROMPT = "ignored"
_DEVELOP_SKILL = "develop"
_REVIEW_SKILL = "review"
_TRAJECTORY_PROMPT = "implement the widget"
_REPORTED_COST_USD = 0.0123
_CLAUDE_INPUT_TOKENS = 1234
_CLAUDE_OUTPUT_TOKENS = 567
_CLAUDE_CACHE_WRITE_TOKENS = 80
_CODEX_INPUT_TOKENS = 2000
_CODEX_CACHED_TOKENS = 500
_CODEX_OUTPUT_TOKENS = 800
_SKILL_OUTPUT_TOKENS = 500
_IMPLEMENTING_ANALYTICS_ISSUE_NUMBER = 101
_REDACTION_ISSUE_NUMBER = 102
_REVIEW_ISSUE_NUMBER = 103
_REVIEW_PR_NUMBER = 44
_TIMEOUT_ISSUE_NUMBER = 104
_AUDIT_ISSUE_NUMBER = 105
_DISABLED_SINK_ISSUE_NUMBER = 106
_CODEX_FALLBACK_ISSUE_NUMBER = 107
_CLAUDE_FALLBACK_ISSUE_NUMBER = 108
_USAGE_HELPER_ISSUE_NUMBER = 401
_TRAJECTORY_ISSUE_NUMBER = 301
_PROMPT_FORWARDING_ISSUE_NUMBER = 302
_TRAJECTORY_FAILURE_ISSUE_NUMBER = 303
_TRAJECTORY_SINK_ISSUE_NUMBER = 562
_SKILL_AGENT_ISSUE_NUMBER = 201
_SKILL_REUSE_ISSUE_NUMBER = 202


def _codex_stdout_no_model(
    *,
    input_tokens: int = 2000,
    cached: int = 500,
    output_tokens: int = 800,
) -> str:
    """Build a codex --json stdout with usage frames but NO model field.

    Reproduces the case the reviewer flagged: codex sometimes emits a
    usage frame on resume / minimal completions whose `model` is
    missing. Without `fallback_model` the parser tags the run
    `unknown-price` with `models=[]`; with the fallback it should
    populate `models` with the configured model and -- when priced --
    produce an `estimated` cost.
    """
    return json.dumps({
        _TYPE_KEY: "turn_complete",
        _USAGE_KEY: {
            _INPUT_TOKENS_KEY: input_tokens,
            "cached_input_tokens": cached,
            _OUTPUT_TOKENS_KEY: output_tokens,
        },
    })


def _claude_stdout(
    *,
    msg_id: str = "msg-1",
    model: str = _CLAUDE_MODEL,
    total_cost_usd: Optional[float] = None,
    num_turns: int = 2,
) -> str:
    """Build a minimal claude stream-json stdout the usage parser understands.

    Mirrors the shape `parse_claude_usage` reads: one assistant frame with
    `message.usage` and one terminal `result` frame carrying `num_turns`
    (and `total_cost_usd` when the agent self-reports it).
    """
    assistant = {
        _TYPE_KEY: "assistant",
        _MESSAGE_KEY: {
            _ID_KEY: msg_id,
            "model": model,
            _USAGE_KEY: {
                _INPUT_TOKENS_KEY: _CLAUDE_INPUT_TOKENS,
                _OUTPUT_TOKENS_KEY: _CLAUDE_OUTPUT_TOKENS,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": _CLAUDE_CACHE_WRITE_TOKENS,
            },
        },
    }
    result_frame = {_TYPE_KEY: _RESULT_KEY, "num_turns": num_turns}
    if total_cost_usd is not None:
        result_frame["total_cost_usd"] = total_cost_usd
    return "\n".join([json.dumps(assistant), json.dumps(result_frame)])


def _claude_stdout_with_skills(
    *,
    skills: tuple[str, ...],
    args_marker: str = "skill-args-must-never-be-stored",
) -> str:
    """A claude stream-json stdout that reports usage AND triggers `Skill`
    blocks -- each name in `skills` becomes one `tool_use` block named
    `"Skill"`. The `args` string is asserted never to reach an emitted event
    (Privacy: only the skill name is read).
    """
    content_blocks = [
        {
            _TYPE_KEY: "tool_use",
            "name": "Skill",
            "input": {_SKILL_KEY: name, "args": args_marker},
        }
        for name in skills
    ]
    assistant = {
        _TYPE_KEY: "assistant",
        _MESSAGE_KEY: {
            _ID_KEY: "msg-skill",
            "model": _CLAUDE_MODEL,
            _CONTENT_KEY: content_blocks,
            _USAGE_KEY: {
                _INPUT_TOKENS_KEY: 1000,
                _OUTPUT_TOKENS_KEY: _SKILL_OUTPUT_TOKENS,
            },
        },
    }
    result_frame = {_TYPE_KEY: _RESULT_KEY, "num_turns": 1}
    return "\n".join([json.dumps(assistant), json.dumps(result_frame)])


def _skill_events(gh: FakeGitHubClient) -> list[dict]:
    return [
        event for event in gh.recorded_events
        if event[_EVENT_KEY] == EVENT_SKILL_TRIGGERED
    ]


class _RaisingOnSkillGitHubClient(FakeGitHubClient):
    def emit_event(self, event, **kwargs):
        if event == EVENT_SKILL_TRIGGERED:
            raise RuntimeError("emit boom")
        return super().emit_event(event, **kwargs)


class AgentAnalyticsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_run_agent_tracked` appends a single analytics record per agent
    exit, carrying the configured spec, resume/session context, retry
    budget, reviewer round, duration, exit metadata, parsed token
    counts, model list, cost, and cost_source -- and never the prompt,
    raw stdout, stderr, or any auth header. The existing audit
    `agent_spawn` / `agent_exit` events must continue to fire unchanged.
    """

    def test_implementing_spawn_appends_record(self) -> None:
        # End-to-end: an implementing tick spawns the dev agent, the
        # wrapper parses usage from a realistic claude stream-json stdout
        # and appends one well-formed JSONL line to the configured sink.
        with tempfile.TemporaryDirectory(prefix="analytics-impl-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            stdout = _claude_stdout(total_cost_usd=_REPORTED_COST_USD)
            gh = FakeGitHubClient()
            issue = make_issue(
                _IMPLEMENTING_ANALYTICS_ISSUE_NUMBER,
                label=LABEL_IMPLEMENTING,
            )
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-impl",
                    last_message="open question?",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            records = _analytics_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            # Audit context — same shape `agent_exit` uses, so an
            # operator can correlate sinks one-to-one.
            self.assertEqual(rec[_EVENT_KEY], EVENT_AGENT_EXIT)
            self.assertEqual(rec["repo"], TEST_REPO_SLUG)
            self.assertEqual(
                rec["issue"],
                _IMPLEMENTING_ANALYTICS_ISSUE_NUMBER,
            )
            self.assertEqual(rec[_STAGE_KEY], LABEL_IMPLEMENTING)
            self.assertEqual(rec[_AGENT_ROLE_KEY], ROLE_DEVELOPER)
            self.assertEqual(rec["backend"], config.DEV_AGENT)
            # Configured spec: implementing's fresh-spawn branch persists
            # DEV_AGENT_SPEC in pinned state before invoking the wrapper.
            self.assertEqual(rec["agent_spec"], config.DEV_AGENT_SPEC)
            self.assertEqual(rec["session_id"], "sess-impl")
            self.assertNotIn("resume_session_id", rec)  # fresh spawn
            self.assertEqual(rec["review_round"], 0)
            self.assertEqual(rec["exit_code"], 0)
            self.assertFalse(rec["timed_out"])
            self.assertGreaterEqual(rec["duration_s"], 0)
            # Parsed usage from the synthetic claude stream-json stdout.
            self.assertEqual(rec[_INPUT_TOKENS_KEY], _CLAUDE_INPUT_TOKENS)
            self.assertEqual(rec[_OUTPUT_TOKENS_KEY], _CLAUDE_OUTPUT_TOKENS)
            self.assertEqual(rec["cache_read_tokens"], 100)
            self.assertEqual(
                rec["cache_write_tokens"],
                _CLAUDE_CACHE_WRITE_TOKENS,
            )
            self.assertEqual(rec["models"], [_CLAUDE_MODEL])
            self.assertEqual(rec["turns"], 2)
            # Reported cost wins over the price-table estimate.
            self.assertEqual(rec["cost_source"], "reported")
            self.assertAlmostEqual(rec[_COST_USD_KEY], _REPORTED_COST_USD)
            # retry_count was incremented to 1 by the budget check
            # before the spawn (the spawn ran under retry budget #1).
            self.assertEqual(rec["retry_count"], 1)

    def test_excludes_prompt_stdout_stderr_secrets(self) -> None:
        # The sink is a usage/cost surface, not a debugging mirror.
        # `result.stdout` may contain user-issue text and we must never
        # store it (nor the prompt the agent was sent, nor stderr which
        # can leak token-shaped strings from CLI banners).
        with tempfile.TemporaryDirectory(prefix="analytics-redaction-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            stdout = _claude_stdout()
            secret_marker = "ghp_DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEAD"
            stderr_marker = f"WARN missing scope for {secret_marker}"
            gh = FakeGitHubClient()
            issue = make_issue(
                _REDACTION_ISSUE_NUMBER,
                label=LABEL_IMPLEMENTING,
                body=f"please use token {secret_marker}",
            )
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-redact",
                    last_message="q?",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr=stderr_marker,
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            records = _analytics_records(path)
            self.assertEqual(len(records), 1)
            blob = json.dumps(records[0])
            # The configured token, the prompt body, the stderr tail, and
            # the raw stdout must all stay out of the record.
            self.assertNotIn(secret_marker, blob)
            self.assertNotIn("please use token", blob)
            self.assertNotIn("missing scope", blob)
            self.assertNotIn(stdout, blob)
            # Prompt-shaped fields must be absent.
            for forbidden in (
                "prompt", "stdout", "stderr", "last_message", "cwd",
            ):
                self.assertNotIn(forbidden, records[0])

    def test_reviewer_record_carries_round_and_resume(
        self,
    ) -> None:
        # Reviewer spawn carries `agent_spec=REVIEW_AGENT_SPEC` and the
        # current review_round / retry_count; the wrapper records both
        # `resume_session_id` (None for the fresh reviewer) and the
        # `session_id` the AgentResult surfaced.
        with tempfile.TemporaryDirectory(prefix="analytics-review-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            stdout = _claude_stdout(msg_id="msg-review")
            gh = FakeGitHubClient()
            issue = make_issue(_REVIEW_ISSUE_NUMBER, label=LABEL_VALIDATING)
            gh.add_issue(issue)
            pr = FakePR(
                number=_REVIEW_PR_NUMBER,
                head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-103",
                base_branch=TEST_BASE_BRANCH,
                mergeable=True,
                check_state="success",
                approved=False,
            )
            gh.add_pr(pr)
            gh.seed_state(
                _REVIEW_ISSUE_NUMBER,
                pr_number=_REVIEW_PR_NUMBER,
                review_round=2,
                retry_count=3,
            )
            with patch.object(
                workflow, "_latest_pr_comment_ids",
                return_value=(None, None),
            ):
                self._run(
                    lambda: workflow._handle_validating(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=AgentResult(
                        session_id="sess-review",
                        last_message=REVIEW_APPROVED_MESSAGE,
                        exit_code=0,
                        timed_out=False,
                        stdout=stdout,
                        stderr="",
                    ),
                    head_shas=[pr.head.sha, pr.head.sha],
                    analytics_log_path=path,
                )

            records = _analytics_records(path)
            reviewer = [
                record for record in records
                if record.get(_AGENT_ROLE_KEY) == ROLE_REVIEWER
            ]
            self.assertEqual(len(reviewer), 1)
            reviewer_record = reviewer[0]
            self.assertEqual(reviewer_record[_STAGE_KEY], LABEL_VALIDATING)
            self.assertEqual(reviewer_record["backend"], config.REVIEW_AGENT)
            self.assertEqual(reviewer_record["agent_spec"], config.REVIEW_AGENT_SPEC)
            self.assertEqual(reviewer_record["review_round"], 2)
            self.assertEqual(reviewer_record["retry_count"], 3)
            self.assertEqual(reviewer_record["session_id"], "sess-review")
            # Reviewer always spawns fresh; the wrapper drops None-valued
            # extras so `resume_session_id` is absent (not stored as null).
            self.assertNotIn("resume_session_id", reviewer_record)

    def test_timeout_records_exit_metadata_no_cost(self) -> None:
        # A timed-out agent has empty stdout; the parser yields the
        # `no-usage` sentinel and `cost_usd` stays unset rather than
        # being stored as null. The exit metadata still rides along.
        with tempfile.TemporaryDirectory(prefix="analytics-timeout-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            gh = FakeGitHubClient()
            issue = make_issue(_TIMEOUT_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id=None,
                    last_message="",
                    exit_code=-1,
                    timed_out=True,
                    stdout="",
                    stderr="",
                ),
                has_new_commits=False,
                # before_sha == after_sha: the timeout produced no new commit,
                # so the issue parks (the disposition reads HEAD twice now).
                head_shas=("sha-pre", "sha-pre"),
                analytics_log_path=path,
            )

            records = _analytics_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["exit_code"], -1)
            self.assertTrue(rec["timed_out"])
            self.assertEqual(rec["cost_source"], "no-usage")
            self.assertNotIn(_COST_USD_KEY, rec)
            self.assertEqual(rec[_INPUT_TOKENS_KEY], 0)
            self.assertEqual(rec[_OUTPUT_TOKENS_KEY], 0)

    def test_audit_events_unchanged_with_record(self) -> None:
        # Preserving the existing audit schema is a hard requirement:
        # one `agent_spawn` + one `agent_exit` per invocation, both
        # appearing in the in-memory capture even though the analytics
        # sink also writes a single record to disk.
        with tempfile.TemporaryDirectory(prefix="analytics-audit-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            stdout = _claude_stdout()
            gh = FakeGitHubClient()
            issue = make_issue(_AUDIT_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-x",
                    last_message="q?",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            spawns = [
                event for event in gh.recorded_events
                if event[_EVENT_KEY] == EVENT_AGENT_SPAWN
            ]
            exits = [
                event for event in gh.recorded_events
                if event[_EVENT_KEY] == EVENT_AGENT_EXIT
            ]
            self.assertEqual(len(spawns), 1)
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["session_id"], "sess-x")
            self.assertEqual(exits[0]["exit_code"], 0)
            # And exactly one analytics record for the same invocation.
            self.assertEqual(len(_analytics_records(path)), 1)

    def test_disabled_sink_writes_no_analytics_file(self) -> None:
        # `ANALYTICS_LOG_PATH=None` is the documented disable knob;
        # `_run_agent_tracked` must still fire the audit events but the
        # sink path must not be created. The `_run` default already
        # patches `ANALYTICS_LOG_PATH=None`, so the sentinel must stay
        # absent without any opt-in from this test.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-exist.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(
                _DISABLED_SINK_ISSUE_NUMBER,
                label=LABEL_IMPLEMENTING,
            )
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-off",
                    last_message="q?",
                    exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout(),
                    stderr="",
                ),
                has_new_commits=False,
            )
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])
            # Audit events are still captured in memory.
            self.assertIn(
                EVENT_AGENT_EXIT,
                {event[_EVENT_KEY] for event in gh.recorded_events},
            )


class AgentAnalyticsModelFallbackTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """Configured models fill only streams that omit their model."""

    def test_codex_no_model_uses_spec_fallback(self) -> None:
        # Reviewer-flagged regression: a codex run whose stdout includes
        # usage frames but omits the `model` field used to record
        # `models=[]` and `cost_source="unknown-price"` even when the
        # configured spec named a priced model. `_run_agent_tracked`
        # must pull the model out of `extra_args` (`-m gpt-5-codex`)
        # and pass it to `usage.parse_agent_usage` as `fallback_model`
        # so the spec-known model both labels the record and enables
        # the price-table estimate.
        with tempfile.TemporaryDirectory(prefix="analytics-codex-fallback-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, path), \
                 patch.object(analytics, _TRAJECTORY_PATH_ATTR, None), \
                 patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
                run_mock.return_value = AgentResult(
                    session_id="sess-codex",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_codex_stdout_no_model(),
                    stderr="",
                )
                gh = FakeGitHubClient()
                workflow._run_agent_tracked(
                    gh, _CODEX_FALLBACK_ISSUE_NUMBER,
                    agent_role=ROLE_DEVELOPER,
                    stage=LABEL_IMPLEMENTING,
                    backend=BACKEND_CODEX,
                    prompt=_IGNORED_PROMPT,
                    cwd=_FAKE_WT,
                    agent_spec=f"codex -m {_CODEX_MODEL}",
                    extra_args=("-m", _CODEX_MODEL),
                    retry_count=1,
                )

            records = _analytics_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["backend"], BACKEND_CODEX)
            self.assertEqual(rec["agent_spec"], f"codex -m {_CODEX_MODEL}")
            # Fallback wired the configured model into both the model
            # list and the cost estimate.
            self.assertEqual(rec["models"], [_CODEX_MODEL])
            self.assertEqual(rec["cost_source"], "estimated")
            self.assertIn(_COST_USD_KEY, rec)
            self.assertGreater(rec[_COST_USD_KEY], 0)
            # Parsed counts come from the codex usage frame verbatim.
            self.assertEqual(rec[_INPUT_TOKENS_KEY], _CODEX_INPUT_TOKENS)
            self.assertEqual(rec["cached_tokens"], _CODEX_CACHED_TOKENS)
            self.assertEqual(rec[_OUTPUT_TOKENS_KEY], _CODEX_OUTPUT_TOKENS)

    def test_claude_model_ignores_spec_fallback(self) -> None:
        # Companion guard: when the stream itself carries a model
        # (claude always does, codex usually does), the spec fallback
        # must not override it. The configured spec names a different
        # model than the stream's `message.model`; the record should
        # reflect the stream-reported model, not the fallback.
        with tempfile.TemporaryDirectory(prefix="analytics-claude-fallback-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            with patch.object(analytics, _ANALYTICS_PATH_ATTR, path), \
                 patch.object(analytics, _TRAJECTORY_PATH_ATTR, None), \
                 patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
                run_mock.return_value = AgentResult(
                    session_id="sess-claude",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout(model=_CLAUDE_MODEL),
                    stderr="",
                )
                gh = FakeGitHubClient()
                workflow._run_agent_tracked(
                    gh, _CLAUDE_FALLBACK_ISSUE_NUMBER,
                    agent_role=ROLE_DEVELOPER,
                    stage=LABEL_IMPLEMENTING,
                    backend=BACKEND_CLAUDE,
                    prompt=_IGNORED_PROMPT,
                    cwd=_FAKE_WT,
                    agent_spec="claude --model claude-opus-4-7",
                    extra_args=("--model", "claude-opus-4-7"),
                    retry_count=1,
                )

            records = _analytics_records(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["models"], [_CLAUDE_MODEL])


def _run_usage(
    *,
    stdout: str,
    backend: str = BACKEND_CLAUDE,
    track: bool = False,
    analytics_path: Optional[Path] = None,
    extra_args: tuple[str, ...] = (),
) -> tuple[FakeGitHubClient, AgentResult]:
    gh = FakeGitHubClient()
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, analytics_path), \
            patch.object(analytics, _TRAJECTORY_PATH_ATTR, None), \
            patch.object(analytics, _TRACK_SKILLS_ATTR, track), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-usage",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        )
        tracked_result = workflow._run_agent_tracked(
            gh, _USAGE_HELPER_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=backend,
            prompt=_IGNORED_PROMPT,
            cwd=_FAKE_WT,
            agent_spec=backend,
            extra_args=extra_args,
            review_round=2,
            retry_count=1,
        )
    return gh, tracked_result


class RunUsageSurfacedTest(unittest.TestCase):
    """Per-issue usage plumbing: `_run_agent_tracked` returns an `AgentResult`
    whose `usage` field carries the same `UsageMetrics` `record_agent_exit`
    parsed for the analytics record -- surfaced even when the sink is off,
    left `None` when the usage parse fails (fail-open), and never disturbing
    the analytics record or the `skill_triggered` audit events."""

    def test_agent_result_usage_defaults_to_none(self) -> None:
        # The new field is defaulted so every existing construction stays
        # valid without passing it; an untracked result carries no usage.
        agent_result = AgentResult(
            session_id="s", last_message="", exit_code=0,
            timed_out=False, stdout="", stderr="",
        )
        self.assertIsNone(agent_result.usage)

    def test_result_carries_usage_without_sink(self) -> None:
        # Sink OFF: the parsed metrics still reach the caller off `.usage`,
        # proving the plumbing is independent of the observability sink.
        gh, agent_result = _run_usage(
            stdout=_claude_stdout(total_cost_usd=_REPORTED_COST_USD),
            analytics_path=None,
        )
        self.assertIsInstance(agent_result.usage, usage.UsageMetrics)
        self.assertEqual(agent_result.usage.backend, BACKEND_CLAUDE)
        self.assertEqual(agent_result.usage.input_tokens, _CLAUDE_INPUT_TOKENS)
        self.assertEqual(agent_result.usage.output_tokens, _CLAUDE_OUTPUT_TOKENS)
        self.assertEqual(agent_result.usage.cache_read_tokens, 100)
        self.assertEqual(
            agent_result.usage.cache_write_tokens,
            _CLAUDE_CACHE_WRITE_TOKENS,
        )
        self.assertEqual(list(agent_result.usage.models), [_CLAUDE_MODEL])
        self.assertEqual(agent_result.usage.turns, 2)
        self.assertEqual(agent_result.usage.cost_source, "reported")
        self.assertAlmostEqual(agent_result.usage.cost_usd, _REPORTED_COST_USD)
        # The lifecycle audit still fired even with the sink disabled.
        self.assertIn(
            EVENT_AGENT_EXIT, {event[_EVENT_KEY] for event in gh.recorded_events},
        )

    def test_usage_reflects_spec_fallback_model(self) -> None:
        # The surfaced metrics are the SAME object the record used, so the
        # codex spec-fallback model path (extra_args -> `_configured_model`
        # -> `fallback_model`) is visible on `.usage` too.
        _, agent_result = _run_usage(
            stdout=_codex_stdout_no_model(),
            backend=BACKEND_CODEX,
            extra_args=("-m", _CODEX_MODEL),
        )
        self.assertIsNotNone(agent_result.usage)
        self.assertEqual(list(agent_result.usage.models), [_CODEX_MODEL])
        self.assertEqual(agent_result.usage.cost_source, "estimated")

    def test_parse_failure_leaves_none_and_fails_open(self) -> None:
        # A raising usage parser must NOT propagate: `record_agent_exit`
        # returns early, no analytics record is written, `.usage` stays None,
        # and the wrapper still returns the AgentResult with its lifecycle
        # audit events intact.
        with tempfile.TemporaryDirectory(prefix="usage-failopen-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            with patch.object(
                analytics.usage, "parse_agent_usage",
                side_effect=RuntimeError("boom"),
            ), self.assertLogs(analytics.log, level="ERROR"):
                gh, agent_result = _run_usage(
                    stdout=_claude_stdout(),
                    analytics_path=path,
                )
            self.assertEqual(agent_result.session_id, "sess-usage")
            self.assertIsNone(agent_result.usage)
            # Parse failure drops the whole record, so nothing is written.
            self.assertEqual(_analytics_records(path), [])
            # Lifecycle audit events fired before the analytics parse ran.
            self.assertIn(
                EVENT_AGENT_EXIT,
                {event[_EVENT_KEY] for event in gh.recorded_events},
            )

    def test_analytics_and_skill_events_unchanged(
        self,
    ) -> None:
        # Surfacing usage must not perturb the analytics record or the
        # skill-trigger audit events: with both enabled, exactly one
        # agent_exit record lands (carrying the usual token fields and no
        # extra `usage` key), the skill events fire, AND the returned result
        # carries the same metrics.
        with tempfile.TemporaryDirectory(prefix="usage-unchanged-") as td:
            path = Path(td) / _ANALYTICS_FILENAME
            gh, agent_result = _run_usage(
                stdout=_claude_stdout_with_skills(
                    skills=(_DEVELOP_SKILL, _REVIEW_SKILL),
                ),
                track=True,
                analytics_path=path,
            )
            records = _analytics_records(path)
            self.assertEqual(len(records), 1)
            exit_record = records[0]
            self.assertEqual(exit_record[_EVENT_KEY], EVENT_AGENT_EXIT)
            self.assertEqual(exit_record[_INPUT_TOKENS_KEY], 1000)
            self.assertEqual(
                exit_record[_OUTPUT_TOKENS_KEY],
                _SKILL_OUTPUT_TOKENS,
            )
            # The record shape is unchanged -- the surfaced field name must
            # not leak into the JSONL record.
            self.assertNotIn(_USAGE_KEY, exit_record)
            # The same numbers are visible on the surfaced metrics object.
            self.assertEqual(agent_result.usage.input_tokens, 1000)
            self.assertEqual(
                agent_result.usage.output_tokens,
                _SKILL_OUTPUT_TOKENS,
            )
            # Skill-trigger audit events are unaffected.
            skill_events = [
                event for event in gh.recorded_events
                if event[_EVENT_KEY] == EVENT_SKILL_TRIGGERED
            ]
            self.assertEqual(
                [event[_SKILL_KEY] for event in skill_events],
                [_DEVELOP_SKILL, _REVIEW_SKILL],
            )


def _claude_trajectory_stdout(
    *,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    tool_result: str = "result text",
    final_output: str = "final answer",
) -> str:
    """A claude stream-json stdout with one tool_use / tool_result step, a
    usage block, and a terminal `result` answer -- the surface the
    trajectory classifier reconstructs."""
    frames = [
        {_TYPE_KEY: "system", "subtype": "init", "tools": ["Read", "Bash"]},
        {
            _TYPE_KEY: "assistant",
            _MESSAGE_KEY: {
                _ID_KEY: "m1", "model": _CLAUDE_MODEL,
                _CONTENT_KEY: [{
                    _TYPE_KEY: "tool_use", "name": tool_name, _ID_KEY: "tu1",
                    "input": tool_input or {"command": "ls"},
                }],
                _USAGE_KEY: {_INPUT_TOKENS_KEY: 100, _OUTPUT_TOKENS_KEY: 50},
            },
        },
        {
            _TYPE_KEY: "user",
            _MESSAGE_KEY: {_CONTENT_KEY: [{
                _TYPE_KEY: "tool_result", "tool_use_id": "tu1",
                _CONTENT_KEY: tool_result,
            }]},
        },
        {_TYPE_KEY: _RESULT_KEY, "num_turns": 1, _RESULT_KEY: final_output},
    ]
    return "\n".join(json.dumps(frame) for frame in frames)


def _run_trajectory(
    *,
    stdout: str,
    prompt: str,
    trajectory_path: Optional[Path],
    analytics_path: Optional[Path] = None,
) -> AgentResult:
    gh = FakeGitHubClient()
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, analytics_path), \
            patch.object(analytics, _TRAJECTORY_PATH_ATTR, trajectory_path), \
            patch.object(analytics, _TRACK_SKILLS_ATTR, False), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-traj",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        )
        return workflow._run_agent_tracked(
            gh, _TRAJECTORY_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=BACKEND_CLAUDE,
            prompt=prompt,
            cwd=_FAKE_WT,
            agent_spec=BACKEND_CLAUDE,
            review_round=2,
            retry_count=1,
        )


class TrajectoryRecordingTest(unittest.TestCase):
    """`_run_agent_tracked` forwards its prompt to `record_agent_exit`, which
    writes one redacted `agent_trajectory` record only when
    `TRAJECTORY_LOG_PATH` is enabled -- never disturbing the baseline
    `agent_exit` analytics record or the `skill_triggered` audit events."""

    def test_prompt_is_forwarded_to_record_agent_exit(self) -> None:
        # The orchestrator-built prompt reaches `record_agent_exit` as the
        # `prompt` kwarg -- the seam that lets it become `user_input`.
        gh = FakeGitHubClient()
        record_mock = MagicMock(return_value=None)
        with patch.object(
            analytics, "record_agent_exit", record_mock,
        ), patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
            run_mock.return_value = AgentResult(
                session_id="s", last_message="", exit_code=0,
                timed_out=False, stdout="", stderr="",
            )
            workflow._run_agent_tracked(
                gh, _PROMPT_FORWARDING_ISSUE_NUMBER,
                agent_role=ROLE_DEVELOPER,
                stage=LABEL_IMPLEMENTING,
                backend=BACKEND_CLAUDE,
                prompt="PROMPT-MARKER-XYZ",
                cwd=_FAKE_WT,
            )
        self.assertEqual(record_mock.call_count, 1)
        self.assertEqual(
            record_mock.call_args.kwargs["prompt"],
            "PROMPT-MARKER-XYZ",
        )

    def test_redacts_user_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="traj-on-") as td:
            t_path = Path(td) / "trajectory.jsonl"
            a_path = Path(td) / _ANALYTICS_FILENAME
            _run_trajectory(
                stdout=_claude_trajectory_stdout(
                    tool_result="hi", final_output="implemented",
                ),
                prompt=_TRAJECTORY_PROMPT,
                trajectory_path=t_path,
                analytics_path=a_path,
            )
            traj = _analytics_records(t_path)
            self.assertEqual(len(traj), 1)
            rec = traj[0]
            self.assertEqual(rec[_EVENT_KEY], EVENT_AGENT_TRAJECTORY)
            self.assertEqual(rec["issue"], _TRAJECTORY_ISSUE_NUMBER)
            self.assertEqual(rec[_STAGE_KEY], LABEL_IMPLEMENTING)
            self.assertEqual(rec[_AGENT_ROLE_KEY], ROLE_DEVELOPER)
            self.assertEqual(rec["user_input"], _TRAJECTORY_PROMPT)
            self.assertEqual(rec["output"], "implemented")
            self.assertEqual(
                [step["kind"] for step in rec["steps"]],
                ["tool_call", "tool_result"],
            )
            # Baseline agent_exit analytics record still written, sans prompt.
            base = _analytics_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertEqual(base[0][_EVENT_KEY], EVENT_AGENT_EXIT)
            self.assertNotIn("user_input", base[0])

    def test_no_trajectory_record_when_sink_off(self) -> None:
        # Default off: a prompt is passed but no trajectory file is created;
        # the baseline agent_exit record is still written.
        with tempfile.TemporaryDirectory(prefix="traj-off-") as td:
            a_path = Path(td) / _ANALYTICS_FILENAME
            _run_trajectory(
                stdout=_claude_trajectory_stdout(),
                prompt=_TRAJECTORY_PROMPT,
                trajectory_path=None,
                analytics_path=a_path,
            )
            self.assertEqual(
                sorted(path.name for path in Path(td).iterdir()),
                [_ANALYTICS_FILENAME],
            )
            base = _analytics_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertNotIn("user_input", base[0])

    def test_failure_keeps_skill_events(self) -> None:
        # A trajectory-parse failure must not cost the `skill_triggered`
        # audit events: they are driven by the value `record_agent_exit`
        # returns before the trajectory block runs.
        gh = FakeGitHubClient()
        with tempfile.TemporaryDirectory(prefix="traj-failopen-") as td:
            t_path = Path(td) / "trajectory.jsonl"
            with (
                patch.object(analytics, _ANALYTICS_PATH_ATTR, None),
                patch.object(analytics, _TRAJECTORY_PATH_ATTR, t_path),
                patch.object(analytics, _TRACK_SKILLS_ATTR, True),
                patch.object(
                    analytics.usage,
                    "parse_agent_trajectory",
                    side_effect=RuntimeError("boom"),
                ),
                patch.object(workflow, _RUN_AGENT_ATTR) as run_mock,
                self.assertLogs(analytics.log, level="ERROR"),
            ):
                run_mock.return_value = AgentResult(
                    session_id="sess-skill", last_message="", exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout_with_skills(skills=(_DEVELOP_SKILL,)),
                    stderr="",
                )
                workflow._run_agent_tracked(
                    gh, _TRAJECTORY_FAILURE_ISSUE_NUMBER,
                    agent_role=ROLE_DEVELOPER,
                    stage=LABEL_IMPLEMENTING,
                    backend=BACKEND_CLAUDE,
                    prompt="p",
                    cwd=_FAKE_WT,
                    agent_spec=BACKEND_CLAUDE,
                )
            skill_events = [
                event for event in gh.recorded_events
                if event[_EVENT_KEY] == EVENT_SKILL_TRIGGERED
            ]
            self.assertEqual(
                [event[_SKILL_KEY] for event in skill_events],
                [_DEVELOP_SKILL],
            )
            self.assertFalse(t_path.exists())


def _drive_trajectory_sink(
    gh: FakeGitHubClient,
    *,
    analytics_path: Path,
) -> None:
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, analytics_path), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-traj-guard",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=_claude_trajectory_stdout(
                tool_result="hi", final_output="done",
            ),
            stderr="",
        )
        workflow._run_agent_tracked(
            gh, _TRAJECTORY_SINK_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=BACKEND_CLAUDE,
            prompt=_TRAJECTORY_PROMPT,
            cwd=_FAKE_WT,
        )


class TrajectorySinkHermeticityTest(unittest.TestCase):
    """Regression guard for the hermetic test default: the global conftest
    fixture pins `TRAJECTORY_LOG_PATH` to None so a workflow analytics path
    can never write to an operator-configured trajectory sink -- even though
    the same synthetic run writes one record when the sink is left on."""

    def test_global_fixture_pins_trajectory_sink_off(self) -> None:
        # The autouse conftest fixture neutralizes any operator-exported
        # TRAJECTORY_LOG_PATH for the duration of every test.
        self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_pinned_off_sink_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory(prefix="traj-guard-") as td:
            configured = Path(td) / "operator-trajectories.jsonl"
            a_path = Path(td) / _ANALYTICS_FILENAME

            # Stand up an operator-configured sink, live on the analytics
            # module exactly as an exported TRAJECTORY_LOG_PATH resolves at
            # import. It stays configured for the whole test so the suppressed
            # run below is gated by the off-pin, not by an ambient None that a
            # bare runner (no env var) would also exhibit.
            with patch.object(analytics, _TRAJECTORY_PATH_ATTR, configured):
                # Control: the configured sink genuinely captures the synthetic
                # run, so the suppression asserted below is a real effect.
                _drive_trajectory_sink(
                    FakeGitHubClient(),
                    analytics_path=a_path,
                )
                self.assertTrue(configured.exists())
                configured.unlink()

                # The documented "off" knob (None) -- the value the conftest
                # autouse fixture installs for every test -- must suppress the
                # write to that still-configured path. Cover both halves of
                # "not created or appended", while the baseline agent_exit
                # record is still produced.
                with patch.object(analytics, _TRAJECTORY_PATH_ATTR, None):
                    # (a) not created: an absent sink stays absent.
                    _drive_trajectory_sink(
                        FakeGitHubClient(),
                        analytics_path=a_path,
                    )
                    self.assertFalse(
                        configured.exists(),
                        "trajectory sink created the operator-configured "
                        "path while pinned off",
                    )
                    # (b) not appended: a pre-existing sink is left byte-for-
                    # byte unchanged.
                    sentinel = '{"event": "pre-existing"}\n'
                    configured.write_text(sentinel, encoding="utf-8")
                    _drive_trajectory_sink(
                        FakeGitHubClient(),
                        analytics_path=a_path,
                    )
                    self.assertEqual(
                        configured.read_text(encoding="utf-8"), sentinel,
                        "trajectory sink appended to the operator-configured "
                        "path while pinned off",
                    )
                self.assertTrue(a_path.exists())


def _run_skill_agent(
    gh: FakeGitHubClient,
    *,
    stdout: str,
    track: bool,
    backend: str = BACKEND_CLAUDE,
) -> AgentResult:
    with patch.object(analytics, _ANALYTICS_PATH_ATTR, None), \
            patch.object(analytics, _TRAJECTORY_PATH_ATTR, None), \
            patch.object(analytics, _TRACK_SKILLS_ATTR, track), \
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock:
        run_mock.return_value = AgentResult(
            session_id="sess-skill",
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout=stdout,
            stderr="",
        )
        return workflow._run_agent_tracked(
            gh, _SKILL_AGENT_ISSUE_NUMBER,
            agent_role=ROLE_DEVELOPER,
            stage=LABEL_IMPLEMENTING,
            backend=backend,
            prompt=_IGNORED_PROMPT,
            cwd=_FAKE_WT,
            agent_spec=backend,
            review_round=2,
            retry_count=1,
        )


class SkillTriggeredEventTest(unittest.TestCase):
    """`_run_agent_tracked` emits one `skill_triggered` audit event per
    distinct triggered skill, gated on `TRACK_SKILL_TRIGGERS` and reusing the
    list `record_agent_exit` already parsed -- never re-reading stdout, never
    leaking the `Skill` args, and never breaking a run if the emit raises."""

    def test_emits_once_per_distinct_skill(self) -> None:
        # develop fires twice, review once: two events in first-seen order,
        # one per DISTINCT skill (the repeat does not double-emit).
        gh = FakeGitHubClient()
        _run_skill_agent(
            gh,
            stdout=_claude_stdout_with_skills(
                skills=(_DEVELOP_SKILL, _DEVELOP_SKILL, _REVIEW_SKILL),
            ),
            track=True,
        )
        events = _skill_events(gh)
        self.assertEqual(
            [event[_SKILL_KEY] for event in events],
            [_DEVELOP_SKILL, _REVIEW_SKILL],
        )
        for event in events:
            self.assertEqual(event["agent"], BACKEND_CLAUDE)
            self.assertEqual(event[_AGENT_ROLE_KEY], ROLE_DEVELOPER)
            self.assertEqual(event[_STAGE_KEY], LABEL_IMPLEMENTING)
            self.assertEqual(event["review_round"], 2)
            self.assertEqual(event["retry_count"], 1)
        # The baseline audit lifecycle events still fire alongside.
        kinds = {
            recorded_event[_EVENT_KEY]
            for recorded_event in gh.recorded_events
        }
        self.assertIn(EVENT_AGENT_SPAWN, kinds)
        self.assertIn(EVENT_AGENT_EXIT, kinds)

    def test_switch_off_emits_no_skill_events(self) -> None:
        # Default-off: a skill-bearing stream produces the lifecycle events
        # but no `skill_triggered` at all -- gating is inherited from the
        # analytics layer returning an empty list.
        gh = FakeGitHubClient()
        _run_skill_agent(
            gh,
            stdout=_claude_stdout_with_skills(
                skills=(_DEVELOP_SKILL, _REVIEW_SKILL),
            ),
            track=False,
        )
        self.assertEqual(_skill_events(gh), [])
        self.assertIn(
            EVENT_AGENT_EXIT, {event[_EVENT_KEY] for event in gh.recorded_events},
        )

    def test_no_triggers_emits_no_skill_events(self) -> None:
        # Switch on but the stream triggered nothing: no events emitted.
        gh = FakeGitHubClient()
        _run_skill_agent(gh, stdout=_claude_stdout(), track=True)
        self.assertEqual(_skill_events(gh), [])

    def test_skill_args_never_reach_the_event(self) -> None:
        # Privacy: the `Skill` args payload must never land in an event.
        gh = FakeGitHubClient()
        marker = "ghp_LEAKED_SKILL_ARG_DO_NOT_EMIT"
        _run_skill_agent(
            gh,
            stdout=_claude_stdout_with_skills(
                skills=(_DEVELOP_SKILL,), args_marker=marker,
            ),
            track=True,
        )
        events = _skill_events(gh)
        self.assertEqual(
            [event[_SKILL_KEY] for event in events],
            [_DEVELOP_SKILL],
        )
        blob = json.dumps(events)
        self.assertNotIn(marker, blob)
        self.assertNotIn("args", blob)

    def test_emission_reuses_record_agent_exit_return(self) -> None:
        # The events are driven by `record_agent_exit`'s return value, not a
        # second parse of stdout: a stubbed return emits exactly its names.
        gh = FakeGitHubClient()
        with (
            patch.object(analytics, _ANALYTICS_PATH_ATTR, None),
            patch.object(
                analytics,
                "record_agent_exit",
                return_value=["alpha", "beta"],
            ),
            patch.object(workflow, _RUN_AGENT_ATTR) as run_mock,
        ):
            run_mock.return_value = AgentResult(
                session_id="s", last_message="", exit_code=0,
                timed_out=False, stdout="ignored-not-reparsed", stderr="",
            )
            workflow._run_agent_tracked(
                gh, _SKILL_REUSE_ISSUE_NUMBER,
                agent_role=ROLE_REVIEWER,
                stage=LABEL_VALIDATING,
                backend=BACKEND_CODEX,
                prompt=_IGNORED_PROMPT,
                cwd=_FAKE_WT,
            )
        self.assertEqual(
            [event[_SKILL_KEY] for event in _skill_events(gh)],
            ["alpha", "beta"],
        )

    def test_emission_is_fail_open(self) -> None:
        # A bug in the skill emit must NOT break a run whose baseline audit
        # events already fired: the loop's own guard logs and falls through,
        # and `_run_agent_tracked` still returns the AgentResult.
        gh = _RaisingOnSkillGitHubClient()
        with self.assertLogs(workflow.log, level="ERROR"):
            agent_result = _run_skill_agent(
                gh,
                stdout=_claude_stdout_with_skills(skills=(_DEVELOP_SKILL,)),
                track=True,
            )
        self.assertEqual(agent_result.session_id, "sess-skill")
        # The raising path emitted no skill event, but the lifecycle events
        # (which do not raise) still landed.
        self.assertEqual(_skill_events(gh), [])
        self.assertIn(
            EVENT_AGENT_EXIT,
            {event[_EVENT_KEY] for event in gh.recorded_events},
        )
