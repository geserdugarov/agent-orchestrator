# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude malformed and skill-free event tests."""

import json
import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_claude_events as _claude


class ClaudeSkillErrorTest(unittest.TestCase):
    """``_usage.parse_claude_skills`` over synthetic ``stream-json`` runs.

    Skill invocations surface as ``Skill`` ``tool_use`` blocks inside
    ``assistant`` messages; the parser reads only ``input.skill``, keeps
    first-seen order, de-duplicates per-invocation by the block ``id``, and
    counts repeats. The offered set comes from the ``system``/``init``
    frame's ``skills`` array. Fixtures mirror the real captured shape: under
    ``--include-partial-messages`` the content array is partitioned one
    completed block per ``assistant`` frame (not a cumulative snapshot), so a
    ``tool_use`` block appears in exactly one frame and carries a unique id.
    """

    def test_available_empty_without_init_skills(self) -> None:
        # An init frame with no `skills` key, a non-list `skills`, and a
        # stream with no init frame at all all yield an empty offered set,
        # never an exception.
        for frame in (
            _claude.system_init(),
            _claude.system_init(skills=_usage_cases.DEVELOP),
            {_usage_cases.TYPE_FIELD: "system", "subtype": "status"},
        ):
            with self.subTest(frame=frame):
                skills = _usage.parse_claude_skills(_jsonl.jsonl(frame))
                self.assertEqual(skills.available, ())

    def test_malformed_lines_are_skipped(self) -> None:
        good_event = _claude.assistant(
            content_blocks=[_claude.skill_use(_usage_cases.DEVELOP)],
        )
        good = json.dumps(good_event)
        stdout = "\n".join(
            [
                "starting claude...",
                '{"type":"assistant","message"',
                good,
                "",
                "not json either",
            ]
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_skill_free_stream_is_empty(self) -> None:
        # Text and non-Skill tool_use blocks must not register as triggers.
        stdout = _jsonl.jsonl(
            _claude.system_init(),
            _claude.assistant(
                content_blocks=[
                    _jsonl.text("no skills here"),
                    _jsonl.tool_use(
                        _usage_cases.READ_TOOL, {_usage_cases.FILE_PATH_FIELD: _usage_cases.READ_FIXTURE_PATH}
                    ),
                ],
                usage=_claude.usage(input=5, output=3),
            ),
            _claude.terminal_result(num_turns=1),
        )
        self.assertEqual(_usage.parse_claude_skills(stdout), _usage.SkillTriggers())

    def test_malformed_skill_blocks_are_ignored(self) -> None:
        # Missing ``input``, missing/empty ``skill``, and non-dict content
        # entries all skip silently rather than raise.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                content_blocks=[
                    {
                        _usage_cases.TYPE_FIELD: _usage_cases.TOOL_USE_EVENT,
                        _usage_cases.NAME_FIELD: _usage_cases.SKILL_TOOL,
                    },
                    {
                        _usage_cases.TYPE_FIELD: _usage_cases.TOOL_USE_EVENT,
                        _usage_cases.NAME_FIELD: _usage_cases.SKILL_TOOL,
                        _usage_cases.INPUT_FIELD: {},
                    },
                    {
                        _usage_cases.TYPE_FIELD: _usage_cases.TOOL_USE_EVENT,
                        _usage_cases.NAME_FIELD: _usage_cases.SKILL_TOOL,
                        _usage_cases.INPUT_FIELD: {"skill": ""},
                    },
                    "not-a-block",
                    _claude.skill_use(_usage_cases.DEVELOP),
                ]
            ),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_ignores_skill_args_for_privacy(self) -> None:
        # `input.args` can echo issue / user content; only the name is read.
        secret = "user secret: api_key=sk-deadbeef"
        stdout = _jsonl.jsonl(
            _claude.assistant(content_blocks=[_claude.skill_use(_usage_cases.DEVELOP, args=secret)]),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)
        self.assertNotIn(secret, repr(skills))

    def test_empty_stdout(self) -> None:
        self.assertEqual(_usage.parse_claude_skills(""), _usage.SkillTriggers())
