# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex skill discovery recording tests."""

import os


import tempfile


import unittest


from dataclasses import dataclass


from pathlib import Path


from unittest.mock import patch

from tests.analytics_reload_helpers import reload_analytics as _reload

from tests.analytics_jsonl_helpers import (
    read_records as _read_records,
)

from tests.analytics_recording_cases import (
    claude_stdout_with_skills as _claude_stdout_with_skills,
)

from tests.analytics_trajectory_cases import (
    codex_trajectory_stdout as _codex_trajectory_stdout,
)

_NONE_SENTINEL = 'none'


AGENT_EXIT_ISSUE_NUMBER = 7


SKILL_STREAM_INPUT_TOKENS = 1_000


SKILL_STREAM_OUTPUT_TOKENS = 500


CODEX_TRAJECTORY_INPUT_TOKENS = 200


CODEX_TRAJECTORY_OUTPUT_TOKENS = 80


_REPO = "owner/repo"


_CLAUDE = "claude"


_CODEX = "codex"


_DEVELOP = "develop"


_REVIEW = "review"


_STAGE_IMPLEMENTING = "implementing"


_DEVELOPER = "developer"


_AGENT_EXIT = "agent_exit"


_AGENT_TRAJECTORY = "agent_trajectory"


_CLAUDE_MODEL = "claude-sonnet-4-6"


_ENCODING = "utf-8"


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_TRACK_SKILL_TRIGGERS = "TRACK_SKILL_TRIGGERS"


_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"


_CODEX_HOME = "CODEX_HOME"


_INPUT_TOKENS = "input_tokens"


_OUTPUT_TOKENS = "output_tokens"


_BACKEND = "backend"


_SKILLS_TRIGGERED = "skills_triggered"


_SKILLS_AVAILABLE = "skills_available"


def _mk_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# skill\n", encoding=_ENCODING)


@dataclass(frozen=True)
class _SkillDiscoveryCase:
    backend: str
    cwd: Path | None
    temp_dir: str
    track: bool = False
    trajectory: bool = False


class RecordAgentExitCodexSkillDiscoveryTest(unittest.TestCase):
    """Codex has no offered-skills stream frame, so `record_agent_exit`
    backfills `skills_available` out-of-band from the worktree / `$CODEX_HOME`
    skill roots (via `skill_catalog.discover_local_skills`) -- into both the
    `agent_exit` record (behind `TRACK_SKILL_TRIGGERS`) and the trajectory
    record (behind `TRAJECTORY_LOG_PATH`). Claude is untouched (its offered
    set rides the stream), and a run with no worktree stays empty."""

    def test_agent_exit_records_discovered_skills(self) -> None:
        _, analytics = _reload()
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {_CODEX_HOME: str(Path(td) / _NONE_SENTINEL)},
            ),
        ):
            cwd = Path(td, "wt")
            _mk_skill(cwd / ".agents/skills", _DEVELOP)
            _mk_skill(cwd / ".agents/skills", _REVIEW)
            base, _ = self._emit(
                analytics,
                backend=_CODEX,
                cwd=cwd,
                td=td,
                track=True,
            )
        rec = base[0]
        self.assertEqual(rec["event"], _AGENT_EXIT)
        # No SKILL.md read in the stream -> nothing triggered, but the offered
        # set is filled from the filesystem scan.
        self.assertEqual(rec[_SKILLS_AVAILABLE], [_DEVELOP, _REVIEW])
        self.assertNotIn(_SKILLS_TRIGGERED, rec)

    def test_trajectory_records_discovered_skills(self) -> None:
        _, analytics = _reload()
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {_CODEX_HOME: str(Path(td) / _NONE_SENTINEL)},
            ),
        ):
            cwd = Path(td) / "wt"
            _mk_skill(cwd / ".claude/skills", _REVIEW)
            _, traj = self._emit(
                analytics,
                backend=_CODEX,
                cwd=cwd,
                td=td,
                traj=True,
            )
        rec = traj[0]
        self.assertEqual(rec["event"], _AGENT_TRAJECTORY)
        self.assertEqual(rec[_BACKEND], _CODEX)
        self.assertEqual(rec[_SKILLS_AVAILABLE], [_REVIEW])
        # The offered-tools baseline is backfilled onto the same record.
        from orchestrator import skill_catalog

        self.assertEqual(rec["tools"], list(skill_catalog.discover_codex_tools()))

    def test_no_worktree_leaves_codex_available_empty(self) -> None:
        # No worktree -> no skill discovery; the offered-tools baseline needs
        # no worktree, so the trajectory record still carries `tools`.
        _, analytics = _reload()
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {_CODEX_HOME: str(Path(td) / _NONE_SENTINEL)},
            ),
        ):
            base, traj = self._emit(
                analytics,
                backend=_CODEX,
                cwd=None,
                td=td,
                track=True,
                traj=True,
            )
        self.assertNotIn(_SKILLS_AVAILABLE, base[0])
        self.assertNotIn(_SKILLS_AVAILABLE, traj[0])
        from orchestrator import skill_catalog

        self.assertEqual(traj[0]["tools"], list(skill_catalog.discover_codex_tools()))

    def test_claude_offered_set_not_from_discovery(self) -> None:
        # Discovery is codex-only: a claude run in a worktree full of skill
        # dirs still takes its offered set from the stream (here: none), never
        # from the filesystem, so a stray scan can't invent a claude field.
        _, analytics = _reload()
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {_CODEX_HOME: str(Path(td) / _NONE_SENTINEL)},
            ),
        ):
            cwd = Path(td) / "wt"
            _mk_skill(cwd / ".agents/skills", _DEVELOP)
            a_path = Path(td) / "a.jsonl"
            with (
                patch.object(analytics, _ANALYTICS_LOG_PATH, a_path),
                patch.object(analytics, _TRAJECTORY_LOG_PATH, None),
                patch.object(analytics, _TRACK_SKILL_TRIGGERS, True),
            ):
                analytics.record_agent_exit(
                    repo=_REPO,
                    issue=AGENT_EXIT_ISSUE_NUMBER,
                    stage=_STAGE_IMPLEMENTING,
                    agent_role=_DEVELOPER,
                    backend=_CLAUDE,
                    agent_spec=_CLAUDE,
                    resume_session_id=None,
                    result=analytics.AgentResult(
                        session_id="s",
                        last_message="",
                        exit_code=0,
                        timed_out=False,
                        stdout=_claude_stdout_with_skills(skills=()),
                        stderr="",
                    ),
                    duration_s=float(),
                    review_round=0,
                    retry_count=0,
                    cwd=cwd,
                )
            self.assertNotIn(_SKILLS_AVAILABLE, _read_records(a_path)[0])

    def _emit(self, analytics, **options) -> tuple[list[dict], list[dict]]:
        case = _SkillDiscoveryCase(
            temp_dir=options.pop("td"),
            trajectory=options.pop("traj", False),
            **options,
        )
        a_path = Path(case.temp_dir) / "a.jsonl"
        t_path = Path(case.temp_dir) / "t.jsonl" if case.trajectory else None
        with (
            patch.object(analytics, _ANALYTICS_LOG_PATH, a_path),
            patch.object(analytics, _TRAJECTORY_LOG_PATH, t_path),
            patch.object(analytics, _TRACK_SKILL_TRIGGERS, case.track),
        ):
            analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage="validating",
                agent_role="reviewer",
                backend=case.backend,
                agent_spec=case.backend,
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_codex_trajectory_stdout(),
                    stderr="",
                ),
                duration_s=float(),
                review_round=1,
                retry_count=0,
                prompt="review this",
                cwd=case.cwd,
            )
        traj_recs = _read_records(t_path) if t_path else []
        return _read_records(a_path), traj_recs
