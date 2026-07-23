# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests.fakes import make_issue
from tests.workflow_helpers import _TEST_SPEC, _manifest

KEY_DECISION = "decision"
KEY_RATIONALE = "rationale"
DECISION_SINGLE = "single"
EXCESSIVE_CHILD_COUNT = 11


class ParseManifestDecisionTest(unittest.TestCase):
    def test_single_decision(self) -> None:
        manifest_block = _manifest(
            '{"decision": "single", "rationale": "small change"}',
        )
        msg = f"I think this fits.\n\n{manifest_block}"
        decision_manifest, decision_error = workflow._parse_manifest(msg)
        self.assertIsNone(decision_error)
        self.assertIsNotNone(decision_manifest)
        self.assertEqual(decision_manifest[KEY_DECISION], DECISION_SINGLE)

    def test_split_decision_two_children(self) -> None:
        payload = (
            '{"decision": "split", "rationale": "too many surfaces", '
            '"children": ['
            '{"title": "A", "body": "do A", "depends_on": []},'
            '{"title": "B", "body": "do B", "depends_on": [0]}'
            "]}"
        )
        decision_manifest, decision_error = workflow._parse_manifest(_manifest(payload))
        self.assertIsNone(decision_error)
        self.assertEqual(len(decision_manifest["children"]), 2)
        self.assertEqual(decision_manifest["children"][1]["depends_on"], [0])

    def test_no_fenced_block_returns_none_none(self) -> None:
        decision_manifest, decision_error = workflow._parse_manifest("just a question, no fence")
        self.assertIsNone(decision_manifest)
        self.assertIsNone(decision_error)

    def test_invalid_json_returns_error(self) -> None:
        decision_manifest, decision_error = workflow._parse_manifest(_manifest("{not json"))
        self.assertIsNone(decision_manifest)
        self.assertIn("invalid JSON", decision_error)

    def test_unknown_decision_rejected(self) -> None:
        decision_manifest, decision_error = workflow._parse_manifest(_manifest('{"decision": "maybe"}'))
        self.assertIsNone(decision_manifest)
        self.assertIn(KEY_DECISION, decision_error)

    def test_split_with_empty_children_rejected(self) -> None:
        decision_manifest, decision_error = workflow._parse_manifest(_manifest('{"decision": "split", "children": []}'))
        self.assertIsNone(decision_manifest)
        self.assertIn("non-empty", decision_error)


class ParseManifestChildValidationTest(unittest.TestCase):
    def test_child_missing_title_rejected(self) -> None:
        child_manifest, child_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": [{"body": "no title here"}]}')
        )
        self.assertIsNone(child_manifest)
        self.assertIn("title or body", child_error)

    def test_self_dependency_rejected(self) -> None:
        child_manifest, child_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": [{"title": "X", "body": "x", "depends_on": [0]}]}')
        )
        self.assertIsNone(child_manifest)
        self.assertIn("invalid dependency", child_error)

    def test_dep_cycle_rejected(self) -> None:
        # 0 -> 1 -> 0
        child_manifest, child_error = workflow._parse_manifest(
            _manifest(
                '{"decision": "split", "children": ['
                '{"title": "A", "body": "a", "depends_on": [1]},'
                '{"title": "B", "body": "b", "depends_on": [0]}'
                "]}"
            )
        )
        self.assertIsNone(child_manifest)
        self.assertIn("cycle", child_error)

    def test_too_many_children_rejected(self) -> None:
        children = ",".join(
            f'{{"title": "T{child_index}", "body": "b{child_index}"}}' for child_index in range(EXCESSIVE_CHILD_COUNT)
        )
        child_manifest, child_error = workflow._parse_manifest(
            _manifest(f'{{"decision": "split", "children": [{children}]}}')
        )
        self.assertIsNone(child_manifest)
        self.assertIn("too many", child_error)

    def test_non_string_title_rejected(self) -> None:
        # JSON-valid child_manifest with a non-string title (here a number)
        # must be rejected before any side effects. Truthiness alone
        # would let `42` pass, but `gh.create_child_issue` (`body.rstrip()`
        # plus the PyGithub call) blows up only AFTER
        # `expected_children_count` has been persisted, forcing the
        # half-finished-recovery path instead of the clean
        # invalid-child_manifest HITL/resume loop.
        child_manifest, child_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": [{"title": 42, "body": "x"}]}')
        )
        self.assertIsNone(child_manifest)
        self.assertIn("title or body", child_error)

    def test_non_string_body_rejected(self) -> None:
        child_manifest, child_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": [{"title": "x", "body": ["a", "b"]}]}')
        )
        self.assertIsNone(child_manifest)
        self.assertIn("title or body", child_error)


class ParseManifestEnvelopeTest(unittest.TestCase):
    def test_multiple_manifest_blocks_rejected(self) -> None:
        # The decompose prompt requires exactly one envelope_manifest. If the
        # decomposer quotes a sample/template envelope_manifest and then emits its
        # real one, `re.search` would silently take the first (sample)
        # block and the orchestrator would act on the wrong decision --
        # creating wrong child issues or marking a split parent as
        # `single`. Reject the message before any side effects.
        sample = _manifest('{"decision": "single", "rationale": "sample"}')
        real = _manifest(
            '{"decision": "split", "rationale": "real", "children": [{"title": "A", "body": "do A", "depends_on": []}]}'
        )
        msg = f"Here is the schema:\n\n{sample}\n\nMy answer:\n\n{real}"
        envelope_manifest, envelope_error = workflow._parse_manifest(msg)
        self.assertIsNone(envelope_manifest)
        self.assertIn("exactly one", envelope_error)
        self.assertIn("found 2", envelope_error)

    def test_content_after_manifest_rejected(self) -> None:
        # The prompt says "nothing else after" the envelope_manifest. Trailing
        # prose suggests the agent did not finish its final answer or
        # appended commentary that the orchestrator would ignore --
        # either way, surface to the human rather than silently act.
        manifest_block = _manifest('{"decision": "single"}')
        msg = f"{manifest_block}\n\nP.S. hope this works"
        envelope_manifest, envelope_error = workflow._parse_manifest(msg)
        self.assertIsNone(envelope_manifest)
        self.assertIn("final block", envelope_error)

    def test_accepts_trailing_manifest_whitespace(self) -> None:
        # Pure whitespace (newlines/spaces) after the closing fence is a
        # benign formatting artifact and must NOT trip the "trailing
        # content" guard.
        manifest_block = _manifest('{"decision": "single"}')
        msg = f"{manifest_block}\n\n   \n"
        envelope_manifest, envelope_error = workflow._parse_manifest(msg)
        self.assertIsNone(envelope_error)
        self.assertEqual(envelope_manifest[KEY_DECISION], DECISION_SINGLE)

    def test_scalar_falsy_depends_on_rejected(self) -> None:
        # `child.get("depends_on") or []` previously collapsed every
        # falsy scalar (0, False, "") to [] before the list-type check.
        # A envelope_manifest like `"depends_on": 0` -- a clear malformed list,
        # not "no deps" -- would be silently accepted and child 1
        # activated before child 0 instead of waiting on it. Reject
        # any non-list, non-null value so the standard invalid-envelope_manifest
        # HITL/resume loop catches the typo.
        for raw in ("0", "false", '""', "0.0"):
            with self.subTest(raw=raw):
                envelope_manifest, envelope_error = workflow._parse_manifest(
                    _manifest(
                        '{"decision": "split", "children": ['
                        '{"title": "A", "body": "a"},'
                        f'{{"title": "B", "body": "b", "depends_on": {raw}}}'
                        "]}"
                    )
                )
                self.assertIsNone(envelope_manifest)
                self.assertIn("must be a list", envelope_error)


class ParseManifestOptionsTest(unittest.TestCase):
    def test_null_depends_on_treated_as_empty(self) -> None:
        # Explicit JSON null is treated the same as a missing key:
        # both signal "no dependencies". Only a non-list, non-null
        # value is a contract violation. This locks in the forgiving
        # behavior so a future tighten-up doesn't accidentally start
        # rejecting `"depends_on": null`.
        options_manifest, options_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": [{"title": "A", "body": "a", "depends_on": null}]}')
        )
        self.assertIsNone(options_error)
        self.assertIsNotNone(options_manifest)

    def test_umbrella_flag_accepted(self) -> None:
        options_manifest, options_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "umbrella": true, "children": [{"title": "A", "body": "a"}]}')
        )
        self.assertIsNone(options_error)
        self.assertTrue(options_manifest.get("umbrella"))

    def test_umbrella_default_missing(self) -> None:
        options_manifest, options_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": [{"title": "A", "body": "a"}]}')
        )
        self.assertIsNone(options_error)
        self.assertIsNone(options_manifest.get("umbrella"))

    def test_umbrella_non_bool_rejected(self) -> None:
        # A typo like `"umbrella": "yes"` would be silently treated as
        # truthy if we coerced; reject so the standard invalid-options_manifest
        # HITL/resume loop catches it instead of mislabeling the parent.
        options_manifest, options_error = workflow._parse_manifest(
            _manifest('{"decision": "split", "umbrella": "yes", "children": [{"title": "A", "body": "a"}]}')
        )
        self.assertIsNone(options_manifest)
        self.assertIn("umbrella", options_error)

    def test_displayed_schema_is_valid(self) -> None:
        # A literal-minded decomposer that copies the schema verbatim
        # must produce a options_manifest that survives _parse_manifest. If the
        # displayed example uses union notation or any other
        # non-JSON sugar, prompt-compliant runs would park awaiting
        # human for a self-inflicted reason. Round-trip the example
        # through the same parser the orchestrator runs on agent
        # output to keep the prompt and parser in lockstep.
        prompt = workflow._build_decompose_prompt(
            _TEST_SPEC,
            make_issue(1, title="example", body="some body"),
            "",
            [_TEST_SPEC],
        )
        manifest_match = workflow._MANIFEST_RE.search(prompt)
        self.assertIsNotNone(
            manifest_match,
            "prompt must contain a fenced example",
        )
        options_manifest, options_error = workflow._parse_manifest(manifest_match.group(0))
        self.assertIsNone(options_error, f"displayed example failed to parse: {options_error}")
        self.assertIsNotNone(options_manifest)


class BuildSingleDecisionCommentTest(unittest.TestCase):
    """The `single`-decision comment carries the context the decomposer
    gathered (affected files + notes) into the implementer, and tolerates
    any missing / malformed optional field without dropping the rationale.
    """

    def test_renders_rationale_files_and_notes(self) -> None:
        comment = workflow._build_single_decision_comment(
            {
                KEY_DECISION: DECISION_SINGLE,
                KEY_RATIONALE: "one small change",
                "affected_files": ["orchestrator/config.py", "tests/fakes.py"],
                "notes": "Bump the default and cover it in fakes.",
            }
        )
        self.assertIn(
            ":mag: decomposer says this fits one context: one small change",
            comment,
        )
        self.assertIn("**Affected files:**", comment)
        self.assertIn("- `orchestrator/config.py`", comment)
        self.assertIn("- `tests/fakes.py`", comment)
        self.assertIn("**Implementation notes:**", comment)
        self.assertIn("Bump the default and cover it in fakes.", comment)

    def test_omits_absent_optional_sections(self) -> None:
        comment = workflow._build_single_decision_comment(
            {
                KEY_DECISION: DECISION_SINGLE,
                KEY_RATIONALE: "trivial",
            }
        )
        self.assertEqual(
            comment,
            ":mag: decomposer says this fits one context: trivial",
        )

    def test_missing_rationale_uses_placeholder(self) -> None:
        # `_parse_manifest` does not validate single-branch fields, so a
        # non-string / absent rationale must not crash rendering.
        comment = workflow._build_single_decision_comment({KEY_DECISION: DECISION_SINGLE, KEY_RATIONALE: [1, 2, 3]})
        self.assertIn("(no rationale provided)", comment)

    def test_drops_malformed_files_and_notes(self) -> None:
        # Non-list files, non-string entries, and non-string notes are
        # sanitized away rather than rendered or raised on.
        comment = workflow._build_single_decision_comment(
            {
                KEY_DECISION: DECISION_SINGLE,
                KEY_RATIONALE: "ok",
                "affected_files": ["good.py", "", 42, "  spaced.py  "],
                "notes": {"not": "a string"},
            }
        )
        self.assertIn("- `good.py`", comment)
        self.assertIn("- `spaced.py`", comment)
        self.assertNotIn("- `42`", comment)
        self.assertNotIn("- ``", comment)
        self.assertNotIn("**Implementation notes:**", comment)

    def test_non_list_affected_files_omits_section(self) -> None:
        comment = workflow._build_single_decision_comment(
            {
                KEY_DECISION: DECISION_SINGLE,
                KEY_RATIONALE: "ok",
                "affected_files": "orchestrator/config.py",
            }
        )
        self.assertNotIn("**Affected files:**", comment)
