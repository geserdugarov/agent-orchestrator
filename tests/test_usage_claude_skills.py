# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude skill event parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_claude_events as _claude


class ClaudeSkillEventTest(unittest.TestCase):
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

    def test_order_dedup_and_counts(self) -> None:
        stdout = _jsonl.jsonl(
            _claude.system_init(),
            _claude.assistant(
                content_blocks=[
                    _jsonl.text("reading the guide"),
                    _claude.skill_use(_usage_cases.DEVELOP),
                ]
            ),
            _claude.assistant(
                id="msg_2",
                content_blocks=[
                    _jsonl.tool_use(
                        _usage_cases.READ_TOOL, {_usage_cases.FILE_PATH_FIELD: _usage_cases.READ_FIXTURE_PATH}
                    ),
                    _claude.skill_use(_usage_cases.REVIEW),
                    _claude.skill_use(_usage_cases.DEVELOP),
                ],
            ),
            _claude.terminal_result(num_turns=2),
        )
        skills = _usage.parse_claude_skills(stdout)
        # First-seen order, de-duplicated.
        self.assertEqual(skills.triggered, (_usage_cases.DEVELOP, _usage_cases.REVIEW))
        # `develop` fired twice (across two messages), `review` once.
        self.assertEqual(skills.trigger_counts, {_usage_cases.DEVELOP: 2, _usage_cases.REVIEW: 1})
        # Every `Skill` call is a confirmed load, so the triggered set carries
        # `confirmed` evidence and claude never produces incidental references
        # (its loads go through the tool, not the shell).
        self.assertEqual(
            skills.evidence,
            {_usage_cases.DEVELOP: "confirmed", _usage_cases.REVIEW: "confirmed"},
        )
        self.assertEqual(skills.incidental, ())
        self.assertEqual(skills.incidental_counts, {})
        # This `init` frame carries no `skills` array, so the offered set is
        # empty (the `available` source is read from `system/init.skills`
        # when present -- see `test_available_from_init_skills`).
        self.assertEqual(skills.available, ())

    def test_partitioned_content_frames_keep_skill(self) -> None:
        # The real capture: `--include-partial-messages` emits one `assistant`
        # frame per completed content block, all sharing the message id. The
        # content array is partitioned across them -- a text block in its own
        # frame, then the `Skill` block in the next -- NOT a cumulative
        # snapshot. Walking every frame retains the trigger even though the
        # trailing frame's content has no skill.
        stdout = _jsonl.jsonl(
            _claude.assistant(content_blocks=[_claude.skill_use(_usage_cases.DEVELOP, id=_usage_cases.TOOL_USE_A_ID)]),
            _claude.assistant(content_blocks=[_jsonl.text("now I'll start")]),
            _claude.terminal_result(num_turns=1),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_repeated_tool_use_id_counted_once(self) -> None:
        # Defensive: should a future stream repeat one block across frames
        # (the way the `usage` sub-object repeats), the shared `tool_use` id
        # de-dups it so a single invocation still counts once.
        stdout = _jsonl.jsonl(
            _claude.assistant(content_blocks=[_claude.skill_use(_usage_cases.DEVELOP, id=_usage_cases.TOOL_USE_A_ID)]),
            _claude.assistant(
                content_blocks=[
                    _claude.skill_use(_usage_cases.DEVELOP, id=_usage_cases.TOOL_USE_A_ID),
                    _claude.skill_use(_usage_cases.REVIEW, id=_usage_cases.TOOL_USE_B_ID),
                ]
            ),
            _claude.terminal_result(num_turns=1),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (_usage_cases.DEVELOP, _usage_cases.REVIEW))
        self.assertEqual(skills.trigger_counts, {_usage_cases.DEVELOP: 1, _usage_cases.REVIEW: 1})

    def test_distinct_ids_count_repeats(self) -> None:
        # Two genuine `develop` invocations carry distinct ids -> count 2.
        stdout = _jsonl.jsonl(
            _claude.assistant(content_blocks=[_claude.skill_use(_usage_cases.DEVELOP, id=_usage_cases.TOOL_USE_A_ID)]),
            _claude.assistant(
                id="msg_2", content_blocks=[_claude.skill_use(_usage_cases.DEVELOP, id=_usage_cases.TOOL_USE_B_ID)]
            ),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, {_usage_cases.DEVELOP: 2})

    def test_available_from_init_skills(self) -> None:
        # The offered set is read from the `system`/`init` frame's dedicated
        # `skills` array (confirmed against a real claude 2.1.x capture), and
        # is independent of what the run triggered: here `review` is offered
        # but never fired, while `develop` is both offered and triggered.
        stdout = _jsonl.jsonl(
            _claude.system_init(skills=[_usage_cases.DEVELOP, _usage_cases.REVIEW, _usage_cases.VERIFY]),
            _claude.assistant(content_blocks=[_claude.skill_use(_usage_cases.DEVELOP, id=_usage_cases.TOOL_USE_A_ID)]),
            _claude.terminal_result(num_turns=1),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.available, (_usage_cases.DEVELOP, _usage_cases.REVIEW, _usage_cases.VERIFY))
        self.assertEqual(skills.triggered, _usage_cases.DEVELOP_ONLY)
        self.assertEqual(skills.trigger_counts, _usage_cases.DEVELOP_TRIGGER_COUNTS)

    def test_available_present_without_any_trigger(self) -> None:
        # Offered-but-not-triggered: `available` populated, `triggered` empty.
        stdout = _jsonl.jsonl(
            _claude.system_init(skills=[_usage_cases.DEVELOP, _usage_cases.REVIEW]),
            _claude.assistant(content_blocks=[_jsonl.text("no skill used")]),
            _claude.terminal_result(num_turns=1),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.available, (_usage_cases.DEVELOP, _usage_cases.REVIEW))
        self.assertEqual(skills.triggered, ())
        self.assertEqual(skills.trigger_counts, {})

    def test_available_dedups_and_filters_non_strings(self) -> None:
        # Non-string entries filter out; duplicates collapse, first-seen order.
        stdout = _jsonl.jsonl(
            _claude.system_init(
                skills=[
                    _usage_cases.DEVELOP,
                    _usage_cases.REVIEW,
                    _usage_cases.DEVELOP,
                    42,
                    None,
                    "",
                    _usage_cases.VERIFY,
                ]
            ),
        )
        skills = _usage.parse_claude_skills(stdout)
        self.assertEqual(skills.available, (_usage_cases.DEVELOP, _usage_cases.REVIEW, _usage_cases.VERIFY))
