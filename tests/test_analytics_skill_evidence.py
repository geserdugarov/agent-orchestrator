# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics agent-exit skill-evidence tests."""

import contextlib


import tempfile


import unittest


from pathlib import Path


from unittest.mock import patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_records as _read_records,
)


from tests.analytics_recording_cases import (
    codex_stdout_with_skills as _codex_stdout_with_skills,
)


_ANALYTICS_FILENAME = 'a.jsonl'


AGENT_EXIT_ISSUE_NUMBER = 7


_REPO = "owner/repo"


_CLAUDE = "claude"


_CODEX = "codex"


_DEVELOP = "develop"


_REVIEW = "review"


_STAGE_IMPLEMENTING = "implementing"


_DEVELOPER = "developer"


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_TRACK_SKILL_TRIGGERS = "TRACK_SKILL_TRIGGERS"


_SKILLS_TRIGGERED = "skills_triggered"


_SKILLS_TRIGGERED_COUNT = "skills_triggered_count"


_SKILLS_EVIDENCE = "skills_evidence"


_SKILLS_INCIDENTAL = "skills_incidental"


_SKILLS_INCIDENTAL_COUNT = "skills_incidental_count"


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


class RecordAgentExitSkillEvidenceTest(_RecordAgentExitSkillSupport):
    def test_codex_records_inferred_and_incidental(self) -> None:
        # A codex run that directly reads review/SKILL.md (an inferred load)
        # and runs `git diff` over a changed develop/SKILL.md (an incidental
        # reference) records the load in `skills_triggered` with `inferred`
        # evidence and the reference in the separate incidental bucket -- never
        # in the triggered set or its count.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics,
                Path(td) / _ANALYTICS_FILENAME,
                stdout=_codex_stdout_with_skills(
                    read=_REVIEW,
                    incidental=_DEVELOP,
                ),
                backend=_CODEX,
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_REVIEW])
        self.assertEqual(rec[_SKILLS_TRIGGERED_COUNT], 1)
        self.assertEqual(rec[_SKILLS_EVIDENCE], {_REVIEW: "inferred"})
        self.assertEqual(rec[_SKILLS_INCIDENTAL], [_DEVELOP])
        self.assertEqual(rec[_SKILLS_INCIDENTAL_COUNT], 1)

    def test_codex_loaded_and_inspected_evidence(self) -> None:
        # A skill a codex run both reads and inspects persists in BOTH the
        # triggered / evidence fields and the incidental fields: the buckets
        # are independent, so a loaded skill keeps its incidental count while
        # the trigger set still excludes the inspection.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics,
                Path(td) / _ANALYTICS_FILENAME,
                stdout=_codex_stdout_with_skills(
                    read=_REVIEW,
                    incidental=_REVIEW,
                ),
                backend=_CODEX,
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_REVIEW])
        self.assertEqual(rec[_SKILLS_TRIGGERED_COUNT], 1)
        self.assertEqual(rec[_SKILLS_EVIDENCE], {_REVIEW: "inferred"})
        self.assertEqual(rec[_SKILLS_INCIDENTAL], [_REVIEW])
        self.assertEqual(rec[_SKILLS_INCIDENTAL_COUNT], 1)

    def test_incidental_run_omits_triggered_keys(self) -> None:
        # A run whose only SKILL.md reference is a `git diff` inspection records
        # the incidental bucket but leaves every triggered / evidence key
        # dropped, so the record cannot masquerade as a load.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics,
                Path(td) / _ANALYTICS_FILENAME,
                stdout=_codex_stdout_with_skills(incidental=_DEVELOP),
                backend=_CODEX,
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_INCIDENTAL], [_DEVELOP])
        self.assertEqual(rec[_SKILLS_INCIDENTAL_COUNT], 1)
        for key in (_SKILLS_TRIGGERED, _SKILLS_TRIGGERED_COUNT, _SKILLS_EVIDENCE):
            self.assertNotIn(key, rec)

    def test_returns_loaded_skills_not_incidental(self) -> None:
        # The value `record_agent_exit` returns -- the list the `skill_triggered`
        # audit emitter iterates -- carries only loaded skills, so an incidental
        # `git diff` reference never produces an audit event.
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_codex_stdout_with_skills(read=_REVIEW, incidental=_DEVELOP),
            track=True,
            backend=_CODEX,
        )
        self.assertEqual(triggered, [_REVIEW])
