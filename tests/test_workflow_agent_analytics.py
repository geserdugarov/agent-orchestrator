# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`_run_agent_tracked` analytics record: one well-formed JSONL line per
agent exit carrying spec/role/session/duration/usage context (and never
prompts, raw streams, or secrets). Includes the spec-fallback model path
for codex stdout that omits the model field, and the disabled-sink knob."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import analytics, config, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import _FAKE_WT, _PatchedWorkflowMixin, _TEST_SPEC


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
        "type": "turn_complete",
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
        },
    })


def _claude_stdout(
    *,
    msg_id: str = "msg-1",
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1234,
    output_tokens: int = 567,
    cache_read: int = 100,
    cache_write_5m: int = 80,
    total_cost_usd: Optional[float] = None,
    num_turns: int = 2,
) -> str:
    """Build a minimal claude stream-json stdout the usage parser understands.

    Mirrors the shape `parse_claude_usage` reads: one assistant frame with
    `message.usage` and one terminal `result` frame carrying `num_turns`
    (and `total_cost_usd` when the agent self-reports it).
    """
    assistant = {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write_5m,
            },
        },
    }
    result_frame = {"type": "result", "num_turns": num_turns}
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
    content = [
        {
            "type": "tool_use",
            "name": "Skill",
            "input": {"skill": name, "args": args_marker},
        }
        for name in skills
    ]
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-skill",
            "model": "claude-sonnet-4-6",
            "content": content,
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        },
    }
    result_frame = {"type": "result", "num_turns": 1}
    return "\n".join([json.dumps(assistant), json.dumps(result_frame)])


class AgentAnalyticsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_run_agent_tracked` appends a single analytics record per agent
    exit, carrying the configured spec, resume/session context, retry
    budget, reviewer round, duration, exit metadata, parsed token
    counts, model list, cost, and cost_source -- and never the prompt,
    raw stdout, stderr, or any auth header. The existing audit
    `agent_spawn` / `agent_exit` events must continue to fire unchanged.
    """

    @staticmethod
    def _exit_records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_implementing_spawn_appends_analytics_record(self) -> None:
        # End-to-end: an implementing tick spawns the dev agent, the
        # wrapper parses usage from a realistic claude stream-json stdout
        # and appends one well-formed JSONL line to the configured sink.
        with tempfile.TemporaryDirectory(prefix="analytics-impl-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout(total_cost_usd=0.0123)
            gh = FakeGitHubClient()
            issue = make_issue(101, label="implementing")
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

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            # Audit context — same shape `agent_exit` uses, so an
            # operator can correlate sinks one-to-one.
            self.assertEqual(rec["event"], "agent_exit")
            self.assertEqual(rec["repo"], "geserdugarov/agent-orchestrator")
            self.assertEqual(rec["issue"], 101)
            self.assertEqual(rec["stage"], "implementing")
            self.assertEqual(rec["agent_role"], "developer")
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
            self.assertEqual(rec["input_tokens"], 1234)
            self.assertEqual(rec["output_tokens"], 567)
            self.assertEqual(rec["cache_read_tokens"], 100)
            self.assertEqual(rec["cache_write_tokens"], 80)
            self.assertEqual(rec["models"], ["claude-sonnet-4-6"])
            self.assertEqual(rec["turns"], 2)
            # Reported cost wins over the price-table estimate.
            self.assertEqual(rec["cost_source"], "reported")
            self.assertAlmostEqual(rec["cost_usd"], 0.0123)
            # retry_count was incremented to 1 by the budget check
            # before the spawn (the spawn ran under retry budget #1).
            self.assertEqual(rec["retry_count"], 1)

    def test_record_excludes_prompt_stdout_stderr_and_secrets(self) -> None:
        # The sink is a usage/cost surface, not a debugging mirror.
        # `result.stdout` may contain user-issue text and we must never
        # store it (nor the prompt the agent was sent, nor stderr which
        # can leak token-shaped strings from CLI banners).
        with tempfile.TemporaryDirectory(prefix="analytics-redaction-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout()
            secret_marker = "ghp_DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEAD"
            stderr_marker = f"WARN missing scope for {secret_marker}"
            gh = FakeGitHubClient()
            issue = make_issue(
                102,
                label="implementing",
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

            records = self._exit_records(path)
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

    def test_reviewer_record_carries_review_round_and_resume_context(
        self,
    ) -> None:
        # Reviewer spawn carries `agent_spec=REVIEW_AGENT_SPEC` and the
        # current review_round / retry_count; the wrapper records both
        # `resume_session_id` (None for the fresh reviewer) and the
        # `session_id` the AgentResult surfaced.
        with tempfile.TemporaryDirectory(prefix="analytics-review-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout(msg_id="msg-review")
            gh = FakeGitHubClient()
            issue = make_issue(103, label="validating")
            gh.add_issue(issue)
            pr = FakePR(
                number=44,
                head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-103",
                base_branch="main",
                mergeable=True,
                check_state="success",
                approved=False,
            )
            gh.add_pr(pr)
            gh.seed_state(103, pr_number=44, review_round=2, retry_count=3)
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
                        last_message="VERDICT: APPROVED",
                        exit_code=0,
                        timed_out=False,
                        stdout=stdout,
                        stderr="",
                    ),
                    head_shas=[pr.head.sha, pr.head.sha],
                    analytics_log_path=path,
                )

            records = self._exit_records(path)
            reviewer = [
                r for r in records if r.get("agent_role") == "reviewer"
            ]
            self.assertEqual(len(reviewer), 1)
            rec = reviewer[0]
            self.assertEqual(rec["stage"], "validating")
            self.assertEqual(rec["backend"], config.REVIEW_AGENT)
            self.assertEqual(rec["agent_spec"], config.REVIEW_AGENT_SPEC)
            self.assertEqual(rec["review_round"], 2)
            self.assertEqual(rec["retry_count"], 3)
            self.assertEqual(rec["session_id"], "sess-review")
            # Reviewer always spawns fresh; the wrapper drops None-valued
            # extras so `resume_session_id` is absent (not stored as null).
            self.assertNotIn("resume_session_id", rec)

    def test_timeout_records_exit_metadata_and_no_cost(self) -> None:
        # A timed-out agent has empty stdout; the parser yields the
        # `no-usage` sentinel and `cost_usd` stays unset rather than
        # being stored as null. The exit metadata still rides along.
        with tempfile.TemporaryDirectory(prefix="analytics-timeout-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(104, label="implementing")
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

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["exit_code"], -1)
            self.assertTrue(rec["timed_out"])
            self.assertEqual(rec["cost_source"], "no-usage")
            self.assertNotIn("cost_usd", rec)
            self.assertEqual(rec["input_tokens"], 0)
            self.assertEqual(rec["output_tokens"], 0)

    def test_audit_events_unchanged_alongside_analytics_record(self) -> None:
        # Preserving the existing audit schema is a hard requirement:
        # one `agent_spawn` + one `agent_exit` per invocation, both
        # appearing in the in-memory capture even though the analytics
        # sink also writes a single record to disk.
        with tempfile.TemporaryDirectory(prefix="analytics-audit-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout()
            gh = FakeGitHubClient()
            issue = make_issue(105, label="implementing")
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
                e for e in gh.recorded_events if e["event"] == "agent_spawn"
            ]
            exits = [
                e for e in gh.recorded_events if e["event"] == "agent_exit"
            ]
            self.assertEqual(len(spawns), 1)
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["session_id"], "sess-x")
            self.assertEqual(exits[0]["exit_code"], 0)
            # And exactly one analytics record for the same invocation.
            self.assertEqual(len(self._exit_records(path)), 1)

    def test_disabled_sink_writes_no_analytics_file(self) -> None:
        # `ANALYTICS_LOG_PATH=None` is the documented disable knob;
        # `_run_agent_tracked` must still fire the audit events but the
        # sink path must not be created. The `_run` default already
        # patches `ANALYTICS_LOG_PATH=None`, so the sentinel must stay
        # absent without any opt-in from this test.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-exist.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(106, label="implementing")
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
                "agent_exit",
                {e["event"] for e in gh.recorded_events},
            )

    def test_codex_stream_without_model_uses_spec_fallback(self) -> None:
        # Reviewer-flagged regression: a codex run whose stdout includes
        # usage frames but omits the `model` field used to record
        # `models=[]` and `cost_source="unknown-price"` even when the
        # configured spec named a priced model. `_run_agent_tracked`
        # must pull the model out of `extra_args` (`-m gpt-5-codex`)
        # and pass it to `usage.parse_agent_usage` as `fallback_model`
        # so the spec-known model both labels the record and enables
        # the price-table estimate.
        with tempfile.TemporaryDirectory(prefix="analytics-codex-fallback-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "run_agent") as run_mock:
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
                    gh, 107,
                    agent_role="developer",
                    stage="implementing",
                    backend="codex",
                    prompt="ignored",
                    cwd=_FAKE_WT,
                    agent_spec="codex -m gpt-5-codex",
                    extra_args=("-m", "gpt-5-codex"),
                    retry_count=1,
                )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["backend"], "codex")
            self.assertEqual(rec["agent_spec"], "codex -m gpt-5-codex")
            # Fallback wired the configured model into both the model
            # list and the cost estimate.
            self.assertEqual(rec["models"], ["gpt-5-codex"])
            self.assertEqual(rec["cost_source"], "estimated")
            self.assertIn("cost_usd", rec)
            self.assertGreater(rec["cost_usd"], 0)
            # Parsed counts come from the codex usage frame verbatim.
            self.assertEqual(rec["input_tokens"], 2000)
            self.assertEqual(rec["cached_tokens"], 500)
            self.assertEqual(rec["output_tokens"], 800)

    def test_claude_stream_with_model_ignores_spec_fallback(self) -> None:
        # Companion guard: when the stream itself carries a model
        # (claude always does, codex usually does), the spec fallback
        # must not override it. The configured spec names a different
        # model than the stream's `message.model`; the record should
        # reflect the stream-reported model, not the fallback.
        with tempfile.TemporaryDirectory(prefix="analytics-claude-fallback-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "run_agent") as run_mock:
                run_mock.return_value = AgentResult(
                    session_id="sess-claude",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout(model="claude-sonnet-4-6"),
                    stderr="",
                )
                gh = FakeGitHubClient()
                workflow._run_agent_tracked(
                    gh, 108,
                    agent_role="developer",
                    stage="implementing",
                    backend="claude",
                    prompt="ignored",
                    cwd=_FAKE_WT,
                    agent_spec="claude --model claude-opus-4-7",
                    extra_args=("--model", "claude-opus-4-7"),
                    retry_count=1,
                )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["models"], ["claude-sonnet-4-6"])


class SkillTriggeredEventTest(unittest.TestCase):
    """`_run_agent_tracked` emits one `skill_triggered` audit event per
    distinct triggered skill, gated on `TRACK_SKILL_TRIGGERS` and reusing the
    list `record_agent_exit` already parsed -- never re-reading stdout, never
    leaking the `Skill` args, and never breaking a run if the emit raises."""

    @staticmethod
    def _skill_events(gh: FakeGitHubClient) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == "skill_triggered"]

    def _run(
        self,
        gh: FakeGitHubClient,
        *,
        stdout: str,
        track: bool,
        backend: str = "claude",
        review_round: Optional[int] = 2,
        retry_count: Optional[int] = 1,
    ) -> AgentResult:
        # Sink path None: the analytics record is a no-op, but the skill
        # parse + return (which drives the audit emission) still runs.
        with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
                patch.object(analytics, "TRACK_SKILL_TRIGGERS", track), \
                patch.object(workflow, "run_agent") as run_mock:
            run_mock.return_value = AgentResult(
                session_id="sess-skill",
                last_message="",
                exit_code=0,
                timed_out=False,
                stdout=stdout,
                stderr="",
            )
            return workflow._run_agent_tracked(
                gh, 201,
                agent_role="developer",
                stage="implementing",
                backend=backend,
                prompt="ignored",
                cwd=_FAKE_WT,
                agent_spec=backend,
                review_round=review_round,
                retry_count=retry_count,
            )

    def test_switch_on_emits_one_event_per_distinct_skill(self) -> None:
        # develop fires twice, review once: two events in first-seen order,
        # one per DISTINCT skill (the repeat does not double-emit).
        gh = FakeGitHubClient()
        self._run(
            gh,
            stdout=_claude_stdout_with_skills(
                skills=("develop", "develop", "review"),
            ),
            track=True,
        )
        events = self._skill_events(gh)
        self.assertEqual([e["skill"] for e in events], ["develop", "review"])
        for ev in events:
            self.assertEqual(ev["agent"], "claude")
            self.assertEqual(ev["agent_role"], "developer")
            self.assertEqual(ev["stage"], "implementing")
            self.assertEqual(ev["review_round"], 2)
            self.assertEqual(ev["retry_count"], 1)
        # The baseline audit lifecycle events still fire alongside.
        kinds = {e["event"] for e in gh.recorded_events}
        self.assertIn("agent_spawn", kinds)
        self.assertIn("agent_exit", kinds)

    def test_switch_off_emits_no_skill_events(self) -> None:
        # Default-off: a skill-bearing stream produces the lifecycle events
        # but no `skill_triggered` at all -- gating is inherited from the
        # analytics layer returning an empty list.
        gh = FakeGitHubClient()
        self._run(
            gh,
            stdout=_claude_stdout_with_skills(skills=("develop", "review")),
            track=False,
        )
        self.assertEqual(self._skill_events(gh), [])
        self.assertIn(
            "agent_exit", {e["event"] for e in gh.recorded_events},
        )

    def test_no_triggers_emits_no_skill_events(self) -> None:
        # Switch on but the stream triggered nothing: no events emitted.
        gh = FakeGitHubClient()
        self._run(gh, stdout=_claude_stdout(), track=True)
        self.assertEqual(self._skill_events(gh), [])

    def test_skill_args_never_reach_the_event(self) -> None:
        # Privacy: the `Skill` args payload must never land in an event.
        gh = FakeGitHubClient()
        marker = "ghp_LEAKED_SKILL_ARG_DO_NOT_EMIT"
        self._run(
            gh,
            stdout=_claude_stdout_with_skills(
                skills=("develop",), args_marker=marker,
            ),
            track=True,
        )
        events = self._skill_events(gh)
        self.assertEqual([e["skill"] for e in events], ["develop"])
        blob = json.dumps(events)
        self.assertNotIn(marker, blob)
        self.assertNotIn("args", blob)

    def test_emission_reuses_record_agent_exit_return(self) -> None:
        # The events are driven by `record_agent_exit`'s return value, not a
        # second parse of stdout: a stubbed return emits exactly its names.
        gh = FakeGitHubClient()
        with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
                patch.object(
                    analytics, "record_agent_exit",
                    return_value=["alpha", "beta"],
                ), \
                patch.object(workflow, "run_agent") as run_mock:
            run_mock.return_value = AgentResult(
                session_id="s", last_message="", exit_code=0,
                timed_out=False, stdout="ignored-not-reparsed", stderr="",
            )
            workflow._run_agent_tracked(
                gh, 202,
                agent_role="reviewer",
                stage="validating",
                backend="codex",
                prompt="ignored",
                cwd=_FAKE_WT,
            )
        self.assertEqual(
            [e["skill"] for e in self._skill_events(gh)], ["alpha", "beta"],
        )

    def test_emission_is_fail_open(self) -> None:
        # A bug in the skill emit must NOT break a run whose baseline audit
        # events already fired: the loop's own guard logs and falls through,
        # and `_run_agent_tracked` still returns the AgentResult.
        class _RaisingOnSkillGH(FakeGitHubClient):
            def emit_event(self, event, **kwargs):
                if event == "skill_triggered":
                    raise RuntimeError("emit boom")
                return super().emit_event(event, **kwargs)

        gh = _RaisingOnSkillGH()
        with self.assertLogs(workflow.log, level="ERROR"):
            result = self._run(
                gh,
                stdout=_claude_stdout_with_skills(skills=("develop",)),
                track=True,
            )
        self.assertEqual(result.session_id, "sess-skill")
        # The raising path emitted no skill event, but the lifecycle events
        # (which do not raise) still landed.
        self.assertEqual(self._skill_events(gh), [])
        self.assertIn("agent_exit", {e["event"] for e in gh.recorded_events})
