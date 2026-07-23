# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_serialization_cases as _serialization
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_claude_events as _claude
from tests import usage_codex_events as _codex
from orchestrator import _usage_metrics, _usage_skills, _usage_trajectory


class DispatcherTest(unittest.TestCase):
    """``_usage.parse_agent_usage`` is a thin dispatcher over the per-backend parsers."""

    def test_routes_claude(self) -> None:
        metrics = _usage.parse_agent_usage(_usage_cases.CLAUDE, "")
        self.assertEqual(metrics.backend, _usage_cases.CLAUDE)

    def test_routes_codex(self) -> None:
        metrics = _usage.parse_agent_usage(_usage_cases.CODEX, "")
        self.assertEqual(metrics.backend, _usage_cases.CODEX)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            _usage.parse_agent_usage("gemini", "")


class UsageMetricsTest(unittest.TestCase):
    def test_to_dict_round_trips_via_json(self) -> None:
        decoded = _serialization.serialize(_serialization.build_usage_metrics())
        self.assertEqual(decoded["backend"], _usage_cases.CODEX)
        self.assertEqual(decoded["models"], [_usage_cases.GPT_FIVE_CODEX])
        self.assertEqual(decoded[_usage_cases.TURNS_FIELD], 3)
        self.assertEqual(decoded["cost_source"], _usage_cases.ESTIMATED_COST_SOURCE)


class SkillDispatcherTest(unittest.TestCase):
    """``_usage.parse_agent_skills`` routes by backend, mirroring ``_usage.parse_agent_usage``."""

    def test_routes_claude(self) -> None:
        # An assistant/tool_use stream is recognized only by the claude path.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.MESSAGE_FIXTURE_ID, content_blocks=[_claude.skill_use(_usage_cases.DEVELOP)]
            )
        )
        self.assertEqual(_usage.parse_agent_skills(_usage_cases.CLAUDE, stdout).triggered, _usage_cases.DEVELOP_ONLY)

    def test_routes_codex(self) -> None:
        # A codex SKILL.md-read command_execution is recognized only by the
        # codex path; the claude parser returns empty on it, so a non-empty
        # result here proves the codex parser ran.
        stdout = _jsonl.jsonl(_codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'cat skills/review/SKILL.md'"))
        self.assertEqual(_usage.parse_agent_skills(_usage_cases.CODEX, stdout).triggered, (_usage_cases.REVIEW,))
        self.assertEqual(_usage.parse_claude_skills(stdout), _usage.SkillTriggers())

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            _usage.parse_agent_skills("gemini", "")


class TrajectoryDispatcherTest(unittest.TestCase):
    """``_usage.parse_agent_trajectory`` routes by backend, mirroring the siblings."""

    def test_routes_claude(self) -> None:
        self.assertEqual(_usage.parse_agent_trajectory(_usage_cases.CLAUDE, "").backend, _usage_cases.CLAUDE)

    def test_routes_codex(self) -> None:
        self.assertEqual(_usage.parse_agent_trajectory(_usage_cases.CODEX, "").backend, _usage_cases.CODEX)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            _usage.parse_agent_trajectory("gemini", "")


class AgentTrajectoryTest(unittest.TestCase):
    def test_to_dict_round_trips_via_json(self) -> None:
        decoded = _serialization.serialize(
            _serialization.build_agent_trajectory(),
        )
        self.assertEqual(
            _serialization.trajectory_summary(decoded),
            (
                _usage_cases.CLAUDE,
                [_usage_cases.BASH_TOOL, _usage_cases.READ_TOOL],
                (None, _usage_cases.FINAL_OUTPUT),
                ([_usage_cases.DEVELOP], [_usage_cases.DEVELOP, _usage_cases.REVIEW]),
            ),
        )
        self.assertEqual(
            _serialization.trajectory_steps(decoded),
            (
                2,
                (_usage_cases.BASH_TOOL, 0),
                (_usage_cases.TOOL_RESULT_STEP, None),
            ),
        )
        self.assertEqual(
            _serialization.trajectory_turns(decoded),
            (
                1,
                (
                    _usage_cases.OPUS_FOUR_EIGHT,
                    _usage_cases.CLAUDE_TURN_CACHE_READ_TOKENS,
                    _usage_cases.ESTIMATED_COST_SOURCE,
                ),
            ),
        )


class CompatibilityReexportTest(unittest.TestCase):
    """Pin the usage-metric, skill-trigger, and trajectory parsing to their
    private ``_usage_metrics`` / ``_usage_skills`` / ``_usage_trajectory`` homes
    and the public ``orchestrator.usage`` re-export site existing callers
    import."""

    def test_usage_metric_surface_is_reexported(self):
        for name in (
            "UsageMetrics",
            "parse_agent_usage",
            "parse_claude_usage",
            "parse_codex_usage",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(_usage, name),
                    getattr(_usage_metrics, name),
                )
                self.assertEqual(
                    getattr(_usage, name).__module__,
                    "orchestrator._usage_metrics",
                )

    def test_skill_surface_is_reexported(self):
        for name in (
            "SkillTriggers",
            "parse_agent_skills",
            "parse_claude_skills",
            "parse_codex_skills",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(_usage, name),
                    getattr(_usage_skills, name),
                )
                self.assertEqual(
                    getattr(_usage, name).__module__,
                    "orchestrator._usage_skills",
                )

    def test_trajectory_surface_is_reexported(self):
        for name in (
            "TrajectoryStep",
            "TurnUsage",
            "AgentTrajectory",
            "parse_agent_trajectory",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(_usage, name),
                    getattr(_usage_trajectory, name),
                )
                self.assertEqual(
                    getattr(_usage, name).__module__,
                    "orchestrator._usage_trajectory",
                )
