# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Wire payloads, constants, and fixtures for agent analytics tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests import workflow_event_values as _events
from tests import workflow_repo_values as _repo
from tests import workflow_stage_labels as _labels
from tests import workflow_state_values as _roles
from tests import workflow_value_helpers as _value_helpers
from tests import workflow_verdict_values as _verdicts
from tests import workflow_patch_runner as _runner
from tests.fakes import FakeGitHubClient


EVENT_AGENT_EXIT = _events.EVENT_AGENT_EXIT
EVENT_AGENT_SPAWN = _events.EVENT_AGENT_SPAWN
EVENT_AGENT_TRAJECTORY = _events.EVENT_AGENT_TRAJECTORY
EVENT_SKILL_TRIGGERED = _events.EVENT_SKILL_TRIGGERED
BACKEND_CLAUDE = _repo.BACKEND_CLAUDE
BACKEND_CODEX = _repo.BACKEND_CODEX
TEST_BASE_BRANCH = _repo.TEST_BASE_BRANCH
TEST_REPO_SLUG = _repo.TEST_REPO_SLUG
_FAKE_WT = _repo._FAKE_WT
_TEST_SPEC = _repo._TEST_SPEC
LABEL_IMPLEMENTING = _labels.LABEL_IMPLEMENTING
LABEL_VALIDATING = _labels.LABEL_VALIDATING
ROLE_DEVELOPER = _roles.ROLE_DEVELOPER
ROLE_REVIEWER = _roles.ROLE_REVIEWER
_analytics_records = _value_helpers._analytics_records
REVIEW_APPROVED_MESSAGE = _verdicts.REVIEW_APPROVED_MESSAGE
_PatchedWorkflowMixin = _runner._PatchedWorkflowMixin


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
    total_cost_usd: float | None = None,
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


def _analytics_path(
    case,
    prefix: str,
    filename: str = _ANALYTICS_FILENAME,
) -> Path:
    temp_dir = tempfile.TemporaryDirectory(prefix=prefix)
    case.addCleanup(temp_dir.cleanup)
    return Path(temp_dir.name) / filename


def _assert_redacted_record(
    case,
    record: dict,
    raw_stdout: str,
    secret_marker: str,
) -> None:
    serialized_record = json.dumps(record)
    case.assertNotIn(secret_marker, serialized_record)
    case.assertNotIn("please use token", serialized_record)
    case.assertNotIn("missing scope", serialized_record)
    case.assertNotIn(raw_stdout, serialized_record)
    for forbidden in (
        "prompt",
        "stdout",
        "stderr",
        "last_message",
        "cwd",
    ):
        case.assertNotIn(forbidden, record)
