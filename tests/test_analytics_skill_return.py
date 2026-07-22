# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics agent-exit skill-return tests."""

import contextlib


import unittest


from unittest.mock import MagicMock, patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_records as _read_records,
)


from tests.analytics_recording_cases import (
    claude_stdout_with_skills as _claude_stdout_with_skills,
)


AGENT_EXIT_ISSUE_NUMBER = 7


_REPO = "owner/repo"


_CLAUDE = "claude"


_DEVELOP = "develop"


_REVIEW = "review"


_STAGE_IMPLEMENTING = "implementing"


_DEVELOPER = "developer"


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_TRACK_SKILL_TRIGGERS = "TRACK_SKILL_TRIGGERS"


class _RecordAgentExitSkillSupport(unittest.TestCase):
    """`record_agent_exit` folds skill triggers into the `agent_exit`
    record only when `TRACK_SKILL_TRIGGERS` is on, never leaks the `Skill`
    args or raw stdout, and keeps emitting the baseline usage/cost record
    even when the skill parse raises (its own fail-open guard)."""

    def _emit(
        self,
        analytics,
        path,
        *,
        stdout,
        backend=_CLAUDE,
        track=True,
    ) -> list[dict]:
        with patch.object(analytics, _ANALYTICS_LOG_PATH, path), patch.object(analytics, _TRACK_SKILL_TRIGGERS, track):
            analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage=_STAGE_IMPLEMENTING,
                agent_role=_DEVELOPER,
                backend=backend,
                agent_spec=_CLAUDE,
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                duration_s=float(),
                review_round=0,
                retry_count=1,
            )
        return _read_records(path)

    def _record(
        self,
        analytics,
        *,
        stdout,
        track=True,
        parse=None,
        backend=_CLAUDE,
    ):
        """Call `record_agent_exit` with the sink disabled and return its
        value -- the de-duplicated triggered list the caller emits events
        from. `parse` optionally stubs the skill extractor.
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(analytics, _ANALYTICS_LOG_PATH, None))
            stack.enter_context(patch.object(analytics, _TRACK_SKILL_TRIGGERS, track))
            if parse is not None:
                stack.enter_context(patch.object(analytics.usage, "parse_agent_skills", parse))
            return analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage=_STAGE_IMPLEMENTING,
                agent_role=_DEVELOPER,
                backend=backend,
                agent_spec=backend,
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                duration_s=float(),
                review_round=0,
                retry_count=1,
            )


class RecordAgentExitSkillReturnTest(_RecordAgentExitSkillSupport):
    def test_returns_triggered_list_when_switch_on(self) -> None:
        # The return value is the de-duplicated first-seen list the audit
        # emitter consumes -- here develop fires twice, review once.
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(
                skills=(_DEVELOP, _DEVELOP, _REVIEW),
            ),
            track=True,
        )
        self.assertEqual(triggered, [_DEVELOP, _REVIEW])

    def test_returns_none_when_switch_off(self) -> None:
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
            track=False,
        )
        self.assertIsNone(triggered)

    def test_returns_none_when_nothing_triggered(self) -> None:
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(skills=()),
            track=True,
        )
        self.assertIsNone(triggered)

    def test_returns_none_on_skill_parse_failure(self) -> None:
        # A skill-parse bug returns None (no events) but still emits baseline.
        _, analytics = _reload()
        with self.assertLogs(analytics.log, level="ERROR"):
            triggered = self._record(
                analytics,
                stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                track=True,
                parse=MagicMock(side_effect=RuntimeError("boom")),
            )
        self.assertIsNone(triggered)
