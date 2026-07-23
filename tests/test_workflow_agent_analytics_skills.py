# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Skill-trigger event emission for tracked agent runs."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from orchestrator import analytics, workflow
from orchestrator.agents import AgentResult

from tests.fakes import FakeGitHubClient

from tests import workflow_agent_analytics_test_support as support

BACKEND_CLAUDE = support.BACKEND_CLAUDE
BACKEND_CODEX = support.BACKEND_CODEX
EVENT_AGENT_EXIT = support.EVENT_AGENT_EXIT
EVENT_AGENT_SPAWN = support.EVENT_AGENT_SPAWN
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_VALIDATING = support.LABEL_VALIDATING
ROLE_DEVELOPER = support.ROLE_DEVELOPER
ROLE_REVIEWER = support.ROLE_REVIEWER
_AGENT_ROLE_KEY = support._AGENT_ROLE_KEY
_ANALYTICS_PATH_ATTR = support._ANALYTICS_PATH_ATTR
_DEVELOP_SKILL = support._DEVELOP_SKILL
_EVENT_KEY = support._EVENT_KEY
_FAKE_WT = support._FAKE_WT
_IGNORED_PROMPT = support._IGNORED_PROMPT
_REVIEW_SKILL = support._REVIEW_SKILL
_RUN_AGENT_ATTR = support._RUN_AGENT_ATTR
_RaisingOnSkillGitHubClient = support._RaisingOnSkillGitHubClient
_SKILL_AGENT_ISSUE_NUMBER = support._SKILL_AGENT_ISSUE_NUMBER
_SKILL_KEY = support._SKILL_KEY
_SKILL_REUSE_ISSUE_NUMBER = support._SKILL_REUSE_ISSUE_NUMBER
_STAGE_KEY = support._STAGE_KEY
_TRACK_SKILLS_ATTR = support._TRACK_SKILLS_ATTR
_TRAJECTORY_PATH_ATTR = support._TRAJECTORY_PATH_ATTR
_claude_stdout = support._claude_stdout
_claude_stdout_with_skills = support._claude_stdout_with_skills
_skill_events = support._skill_events


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
