# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexSkillReadTest(unittest.TestCase):
    """``_usage.parse_codex_skills`` over the confirmed ``codex exec --json`` shape.

    Codex has no dedicated ``Skill`` tool: a captured reviewer run pinned the
    only observable load as a ``command_execution`` whose ``command`` opens a
    ``skills/<name>/SKILL.md`` file. The parser reads only the ``<name>`` path
    segment, dedups the started/completed pair codex emits per command by its
    shared ``item.id``, keeps first-seen order, and returns empty -- never an
    exception -- on a stream that opens no SKILL.md. A command that only
    *inspects* a SKILL.md path (``git diff`` / ``rg``) is classified as an
    incidental reference rather than an inferred load, kept out of the trigger
    set.
    """

    def test_extracts_skill_from_skill_md_read(self) -> None:
        # The confirmed shape: the reviewer opens the review skill's SKILL.md
        # via a shell command. Codex registers the skill under
        # ``$CODEX_HOME/skills/<name>/SKILL.md``; the read carries an absolute
        # path plus unrelated commands chained after it.
        cmd = "/bin/bash -lc \"sed -n '1,220p' /home/u/.codex/skills/review/SKILL.md && git diff -- calc.py\""
        stdout = _jsonl.jsonl(
            {_usage_cases.TYPE_FIELD: _usage_cases.THREAD_STARTED_EVENT, "thread_id": _usage_cases.TASK_ONE_ID},
            {_usage_cases.TYPE_FIELD: "turn.started"},
            _codex.command(_usage_cases.ITEM_ONE_ID, cmd, started=True, status=_usage_cases.IN_PROGRESS_STATUS),
            _codex.command(_usage_cases.ITEM_ONE_ID, cmd, status=_usage_cases.COMPLETED_STATUS, exit_code=0),
            {
                _usage_cases.TYPE_FIELD: _usage_cases.TURN_COMPLETED_EVENT,
                _usage_cases.USAGE_FIELD: {
                    _usage_cases.INPUT_TOKENS_FIELD: 10,
                    _usage_cases.OUTPUT_TOKENS_FIELD: 5,
                },
            },
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.REVIEW,))
        # started + completed echo the same command; the shared id counts once.
        self.assertEqual(skills.trigger_counts, {_usage_cases.REVIEW: 1})
        # A direct `sed`/`cat` read is an inferred load and makes no incidental
        # reference; the chained `git diff` names no SKILL.md path.
        self.assertEqual(skills.evidence, {_usage_cases.REVIEW: _usage_cases.INFERRED_EVIDENCE})
        self.assertEqual(skills.incidental, ())
        self.assertEqual(skills.incidental_counts, {})
        self.assertEqual(skills.available, ())

    def test_started_and_completed_not_double_counted(self) -> None:
        # Explicit dedup guard: a single SKILL.md read emits two frames sharing
        # one ``item.id`` -- they must collapse to one trigger.
        cmd = _usage_cases.DEVELOP_SKILL_READ_COMMAND
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_TWO_ID, cmd, started=True, status=_usage_cases.IN_PROGRESS_STATUS),
            _codex.command(_usage_cases.ITEM_TWO_ID, cmd, status=_usage_cases.COMPLETED_STATUS, exit_code=0),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_project_local_skill_paths(self) -> None:
        # Codex discovers project-local skills too: a captured clean-CODEX_HOME
        # run read ``.agents/skills/review/SKILL.md`` directly. Both the
        # ``.agents/`` source and the ``.claude/`` symlink path resolve.
        stdout = _jsonl.jsonl(
            _codex.command(
                _usage_cases.ITEM_ONE_ID, "/bin/bash -lc \"sed -n '1,200p' .agents/skills/develop/SKILL.md\""
            ),
            _codex.command(_usage_cases.ITEM_TWO_ID, "/bin/bash -lc 'cat .claude/skills/review/SKILL.md'"),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.DEVELOP, _usage_cases.REVIEW))
        self.assertEqual(skills.trigger_counts, {_usage_cases.DEVELOP: 1, _usage_cases.REVIEW: 1})

    def test_order_dedup_counts_across_reads(self) -> None:
        # Distinct ``item.id``s are separate reads: a skill opened in two
        # separate commands counts twice, mirroring the claude path, while the
        # ``triggered`` tuple keeps it once in first-seen order.
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, _usage_cases.DEVELOP_SKILL_READ_COMMAND),
            _codex.command(_usage_cases.ITEM_TWO_ID, "/bin/bash -lc 'cat skills/review/SKILL.md'"),
            _codex.command(_usage_cases.ITEM_THREE_ID, _usage_cases.DEVELOP_SKILL_READ_COMMAND),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.DEVELOP, _usage_cases.REVIEW))
        self.assertEqual(skills.trigger_counts, {_usage_cases.DEVELOP: 2, _usage_cases.REVIEW: 1})

    def test_multiple_skills_in_one_command(self) -> None:
        # One command that opens two SKILL.md files records both, in order.
        stdout = _jsonl.jsonl(
            _codex.command(
                _usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'cat skills/review/SKILL.md skills/develop/SKILL.md'"
            ),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.REVIEW, _usage_cases.DEVELOP))
        self.assertEqual(skills.trigger_counts, {_usage_cases.REVIEW: 1, _usage_cases.DEVELOP: 1})

    def test_skill_free_usage_stream_is_empty(self) -> None:
        # A normal run (thread/turn frames, an agent message, a usage-bearing
        # turn.completed, and ordinary command_execution items that touch no
        # SKILL.md) carries no skill trigger; the parser must not false-positive.
        stdout = _jsonl.jsonl(
            {_usage_cases.TYPE_FIELD: _usage_cases.THREAD_STARTED_EVENT, "thread_id": _usage_cases.TASK_ONE_ID},
            {_usage_cases.TYPE_FIELD: "turn.started"},
            _codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'git diff -- calc.py'"),
            _codex.agent_message(_usage_cases.ITEM_TWO_ID, _usage_cases.APPROVAL_MESSAGE),
            {
                _usage_cases.TYPE_FIELD: _usage_cases.TURN_COMPLETED_EVENT,
                _usage_cases.USAGE_FIELD: {
                    _usage_cases.INPUT_TOKENS_FIELD: 100,
                    "cached_input_tokens": 0,
                    _usage_cases.OUTPUT_TOKENS_FIELD: _usage_cases.TOKEN_COUNT_FIFTY,
                },
            },
        )
        self.assertEqual(_usage.parse_codex_skills(stdout), _usage.SkillTriggers())

    def test_non_skill_md_commands_are_ignored(self) -> None:
        # Touching the skills directory without opening a `<name>/SKILL.md`
        # file is not a trigger; nor is a path where `skills` is a substring of
        # a longer component (`myskills/`), which the boundary anchor rejects.
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'ls -la skills/'"),
            _codex.command(_usage_cases.ITEM_TWO_ID, "/bin/bash -lc 'grep -rn TODO skills/'"),
            _codex.command(_usage_cases.ITEM_THREE_ID, "/bin/bash -lc 'cat myskills/review/SKILL.md'"),
            _codex.command("item_4", "/bin/bash -lc 'cat skills/review/README.md'"),
        )
        self.assertEqual(_usage.parse_codex_skills(stdout), _usage.SkillTriggers())
