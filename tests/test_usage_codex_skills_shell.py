# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexSkillShellParsingTest(unittest.TestCase):
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

    def test_system_skill_subdir_is_not_matched(self) -> None:
        # Built-in skills nest under `skills/.system/<name>/SKILL.md`; their
        # SKILL.md is not directly under `skills/`, so the anchor skips them.
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'cat skills/.system/imagegen/SKILL.md'"),
        )
        self.assertEqual(_usage.parse_codex_skills(stdout), _usage.SkillTriggers())

    def test_aggregated_output_is_never_scanned(self) -> None:
        # The command's ``aggregated_output`` carries the file's contents and
        # other command output -- it can echo issue / user text and even a
        # SKILL.md path. The parser reads only ``command``; a command that
        # opens no SKILL.md records nothing even when its output mentions one.
        leaked = "secret: sk-deadbeef and skills/leaked/SKILL.md"
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc 'git diff'", aggregated_output=leaked),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills, _usage.SkillTriggers())
        self.assertNotIn(leaked, repr(skills))
        self.assertNotIn("leaked", repr(skills))

    def test_only_name_segment_is_captured(self) -> None:
        # The command around the SKILL.md read can carry issue / user content;
        # only the `<name>` path segment is ever extracted, never the rest.
        secret = "user secret: api_key=sk-deadbeef"
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, f"/bin/bash -lc \"cat skills/review/SKILL.md; echo '{secret}'\""),
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.REVIEW,))
        self.assertNotIn(secret, repr(skills))
        self.assertNotIn("sk-deadbeef", repr(skills))

    def test_non_reader_commands_are_incidental(self) -> None:
        # A command that names a `SKILL.md` path without being a direct read is
        # an incidental reference, kept out of `triggered` (and the audit
        # events) and folded into the separate incidental bucket. This covers
        # inspection / search (`git diff` / `git status` / `rg`), a search whose
        # quoted argument holds a shell metacharacter (must not split into a
        # bogus reader segment), an env-prefixed inspection (the `GIT_PAGER=cat`
        # prefix must not read as the verb), and a generic path-only command
        # (`echo`) — none of which establish a read of the file's contents.
        for command in (
            "/bin/bash -lc 'git diff -- .agents/skills/develop/SKILL.md'",
            "/bin/bash -lc 'git status skills/develop/SKILL.md'",
            "/bin/bash -lc 'rg TODO skills/develop/SKILL.md'",
            "/bin/bash -lc \"rg 'foo|bar' .agents/skills/develop/SKILL.md\"",
            "/bin/bash -lc 'GIT_PAGER=cat git diff -- .agents/skills/develop/SKILL.md'",
            "/bin/bash -lc 'echo .agents/skills/develop/SKILL.md'",
        ):
            with self.subTest(command=command):
                skills = _usage.parse_codex_skills(_jsonl.jsonl(_codex.command(_usage_cases.ITEM_ONE_ID, command)))
                self.assertEqual(skills.triggered, ())
                self.assertEqual(skills.trigger_counts, {})
                self.assertEqual(skills.evidence, {})
                self.assertEqual(skills.incidental, _usage_cases.DEVELOP_ONLY)
                self.assertEqual(skills.incidental_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_quoted_metacharacters_stay_in_reader(self) -> None:
        # A reader whose quoted argument carries `|` / `;` must stay one
        # sub-command: quote-aware segmentation keeps the `cat` verb attached to
        # the SKILL.md read, so it registers as an inferred load rather than
        # splitting into a spurious non-reader segment.
        stdout = _jsonl.jsonl(
            _codex.command(_usage_cases.ITEM_ONE_ID, "/bin/bash -lc \"cat 'a|b;c' skills/review/SKILL.md\"")
        )
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.REVIEW,))
        self.assertEqual(skills.evidence, {_usage_cases.REVIEW: _usage_cases.INFERRED_EVIDENCE})
        self.assertEqual(skills.incidental, ())

    def test_escaped_operator_does_not_split(self) -> None:
        # A backslash-escaped operator is a literal argument char, not a
        # sub-command boundary: `rg foo\|cat ... SKILL.md` is one `rg` search,
        # so `develop` stays an incidental reference -- the `\|` must not split
        # off a bogus `cat` reader segment that would fabricate an inferred load
        # and a spurious `skill_triggered` audit event.
        stdout = _jsonl.jsonl(_codex.command(_usage_cases.ITEM_ONE_ID, r"rg foo\|cat .agents/skills/develop/SKILL.md"))
        skills = _usage.parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ())
        self.assertEqual(skills.trigger_counts, {})
        self.assertEqual(skills.evidence, {})
        self.assertEqual(skills.incidental, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.incidental_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_wrapper_decodes_inner_escaped_quotes(self) -> None:
        # The `bash -lc "…"` wrapper is decoded, not blindly stripped: an inner
        # escaped double-quote (`\"`) re-opens a real quote in the script bash
        # runs, so the `|` inside `rg "foo|cat …/SKILL.md"` stays quoted and the
        # search stays one `rg` command -- `develop` is an incidental reference,
        # not a bogus `cat` reader load and audit event.
        cmd = r'/bin/bash -lc "rg \"foo|cat skills/develop/SKILL.md\" README.md"'
        skills = _usage.parse_codex_skills(_jsonl.jsonl(_codex.command(_usage_cases.ITEM_ONE_ID, cmd)))
        self.assertEqual(skills.triggered, ())
        self.assertEqual(skills.evidence, {})
        self.assertEqual(skills.incidental, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.incidental_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)
