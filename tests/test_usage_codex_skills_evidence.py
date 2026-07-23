# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import json
import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexSkillEvidenceTest(unittest.TestCase):
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

    def test_reader_writing_the_skill_is_incidental(self) -> None:
        # A reader verb whose SKILL.md is a *write* target, not an argument it
        # dumps, is an incidental reference -- an output redirection
        # (`cat t > …/SKILL.md`) or a non-reading mode (`sed -i` edits in place)
        # must not fabricate an inferred load.
        for command in (
            "/bin/bash -lc 'cat /tmp/template > .agents/skills/develop/SKILL.md'",
            "/bin/bash -lc \"sed -i 's/a/b/' .agents/skills/develop/SKILL.md\"",
        ):
            with self.subTest(command=command):
                skills = _usage.parse_codex_skills(_jsonl.jsonl(_codex.command(_usage_cases.ITEM_ONE_ID, command)))
                self.assertEqual(skills.triggered, ())
                self.assertEqual(skills.evidence, {})
                self.assertEqual(skills.incidental, _usage_cases.DEVELOP_ONLY)
                self.assertEqual(skills.incidental_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_inferred_and_incidental_reads_coexist(self) -> None:
        # The issue's combined shape: one run that both directly reads
        # review/SKILL.md (an inferred load) and runs `git diff` over a changed
        # develop/SKILL.md (an incidental reference). The two land in separate
        # buckets, and the git-diff item's started/completed pair dedups to one
        # incidental reference by its shared item.id.
        diff = "/bin/bash -lc 'git diff -- .agents/skills/develop/SKILL.md'"
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'cat skills/review/SKILL.md'"),
            _codex.command(_usage_cases.ITEM_TWO_ID, diff, started=True, status=_usage_cases.IN_PROGRESS_STATUS),
            _codex.command(_usage_cases.ITEM_TWO_ID, diff, status=_usage_cases.COMPLETED_STATUS, exit_code=0),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.REVIEW,))
        self.assertEqual(skills.trigger_counts, {_usage_cases.REVIEW: 1})
        self.assertEqual(skills.evidence, {_usage_cases.REVIEW: _usage_cases.INFERRED_EVIDENCE})
        self.assertEqual(skills.incidental, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.incidental_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_incidental_survives_confirmed_load(self) -> None:
        # A skill that is both directly read and separately inspected keeps
        # both records: it stays a trigger (evidence `inferred`, trigger count
        # from the read alone) AND retains its incidental count from the two
        # inspections — the buckets are independent, only the structural
        # exclusion (incidental never enters the trigger set) applies.
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, _usage_cases.DEVELOP_SKILL_READ_COMMAND),
            _codex.command(_usage_cases.ITEM_TWO_ID, "/bin/bash -lc 'git diff -- skills/develop/SKILL.md'"),
            _codex.command(_usage_cases.ITEM_THREE_ID, "/bin/bash -lc 'git status skills/develop/SKILL.md'"),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)
        self.assertEqual(skills.evidence, {_usage_cases.DEVELOP: _usage_cases.INFERRED_EVIDENCE})
        self.assertEqual(skills.incidental, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.incidental_counts, {_usage_cases.DEVELOP: 2})

    def test_read_after_inspection_is_inferred(self) -> None:
        # A single command can chain an inspection and a read; each sub-command
        # is classified on its own leading verb, so the `sed` read is an
        # inferred load even though a `git diff` precedes it in the same command.
        cmd = "/bin/bash -lc \"git diff -- calc.py && sed -n '1,80p' skills/review/SKILL.md\""
        skills = _usage.parse_codex_skills(_jsonl.jsonl(_codex.command(_usage_cases.ITEM_ONE_ID, cmd)))
        self.assertEqual(skills.triggered, (_usage_cases.REVIEW,))
        self.assertEqual(skills.evidence, {_usage_cases.REVIEW: _usage_cases.INFERRED_EVIDENCE})
        self.assertEqual(skills.incidental, ())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(_codex.command(_usage_cases.ITEM_ONE_ID, _usage_cases.DEVELOP_SKILL_READ_COMMAND))
        stdout = "\n".join(
            [
                "codex starting...",
                '{"truncated":',
                good,
                "trailing-noise",
            ]
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_empty_stdout(self) -> None:
        self.assertEqual(_usage.parse_codex_skills(""), _usage.SkillTriggers())
