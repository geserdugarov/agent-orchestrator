# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests.fakes import make_issue
from tests.workflow_helpers import _TEST_SPEC, _manifest


class ParseManifestTest(unittest.TestCase):
    def test_single_decision(self) -> None:
        msg = "I think this fits.\n\n" + _manifest(
            '{"decision": "single", "rationale": "small change"}'
        )
        manifest, error = workflow._parse_manifest(msg)
        self.assertIsNone(error)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["decision"], "single")

    def test_split_decision_two_children(self) -> None:
        payload = (
            '{"decision": "split", "rationale": "too many surfaces", '
            '"children": ['
            '{"title": "A", "body": "do A", "depends_on": []},'
            '{"title": "B", "body": "do B", "depends_on": [0]}'
            ']}'
        )
        manifest, error = workflow._parse_manifest(_manifest(payload))
        self.assertIsNone(error)
        self.assertEqual(len(manifest["children"]), 2)
        self.assertEqual(manifest["children"][1]["depends_on"], [0])

    def test_no_fenced_block_returns_none_none(self) -> None:
        manifest, error = workflow._parse_manifest("just a question, no fence")
        self.assertIsNone(manifest)
        self.assertIsNone(error)

    def test_invalid_json_returns_error(self) -> None:
        manifest, error = workflow._parse_manifest(_manifest("{not json"))
        self.assertIsNone(manifest)
        self.assertIn("invalid JSON", error)

    def test_unknown_decision_rejected(self) -> None:
        manifest, error = workflow._parse_manifest(
            _manifest('{"decision": "maybe"}')
        )
        self.assertIsNone(manifest)
        self.assertIn("decision", error)

    def test_split_with_empty_children_rejected(self) -> None:
        manifest, error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": []}')
        )
        self.assertIsNone(manifest)
        self.assertIn("non-empty", error)

    def test_child_missing_title_rejected(self) -> None:
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"body": "no title here"}'
            ']}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("title or body", error)

    def test_self_dependency_rejected(self) -> None:
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "X", "body": "x", "depends_on": [0]}'
            ']}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("invalid dependency", error)

    def test_dep_cycle_rejected(self) -> None:
        # 0 -> 1 -> 0
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a", "depends_on": [1]},'
            '{"title": "B", "body": "b", "depends_on": [0]}'
            ']}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("cycle", error)

    def test_too_many_children_rejected(self) -> None:
        children = ",".join(
            f'{{"title": "T{i}", "body": "b{i}"}}' for i in range(11)
        )
        manifest, error = workflow._parse_manifest(_manifest(
            f'{{"decision": "split", "children": [{children}]}}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("too many", error)

    def test_non_string_title_rejected(self) -> None:
        # JSON-valid manifest with a non-string title (here a number)
        # must be rejected before any side effects. Truthiness alone
        # would let `42` pass, but `gh.create_child_issue` (`body.rstrip()`
        # plus the PyGithub call) blows up only AFTER
        # `expected_children_count` has been persisted, forcing the
        # half-finished-recovery path instead of the clean
        # invalid-manifest HITL/resume loop.
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": 42, "body": "x"}'
            ']}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("title or body", error)

    def test_non_string_body_rejected(self) -> None:
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "x", "body": ["a", "b"]}'
            ']}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("title or body", error)

    def test_multiple_manifest_blocks_rejected(self) -> None:
        # The decompose prompt requires exactly one manifest. If the
        # decomposer quotes a sample/template manifest and then emits its
        # real one, `re.search` would silently take the first (sample)
        # block and the orchestrator would act on the wrong decision --
        # creating wrong child issues or marking a split parent as
        # `single`. Reject the message before any side effects.
        sample = _manifest('{"decision": "single", "rationale": "sample"}')
        real = _manifest(
            '{"decision": "split", "rationale": "real", "children": ['
            '{"title": "A", "body": "do A", "depends_on": []}'
            ']}'
        )
        msg = f"Here is the schema:\n\n{sample}\n\nMy answer:\n\n{real}"
        manifest, error = workflow._parse_manifest(msg)
        self.assertIsNone(manifest)
        self.assertIn("exactly one", error)
        self.assertIn("found 2", error)

    def test_content_after_manifest_rejected(self) -> None:
        # The prompt says "nothing else after" the manifest. Trailing
        # prose suggests the agent did not finish its final answer or
        # appended commentary that the orchestrator would ignore --
        # either way, surface to the human rather than silently act.
        msg = _manifest('{"decision": "single"}') + "\n\nP.S. hope this works"
        manifest, error = workflow._parse_manifest(msg)
        self.assertIsNone(manifest)
        self.assertIn("final block", error)

    def test_accepts_trailing_manifest_whitespace(self) -> None:
        # Pure whitespace (newlines/spaces) after the closing fence is a
        # benign formatting artifact and must NOT trip the "trailing
        # content" guard.
        msg = _manifest('{"decision": "single"}') + "\n\n   \n"
        manifest, error = workflow._parse_manifest(msg)
        self.assertIsNone(error)
        self.assertEqual(manifest["decision"], "single")

    def test_scalar_falsy_depends_on_rejected(self) -> None:
        # `child.get("depends_on") or []` previously collapsed every
        # falsy scalar (0, False, "") to [] before the list-type check.
        # A manifest like `"depends_on": 0` -- a clear malformed list,
        # not "no deps" -- would be silently accepted and child 1
        # activated before child 0 instead of waiting on it. Reject
        # any non-list, non-null value so the standard invalid-manifest
        # HITL/resume loop catches the typo.
        for raw in ("0", "false", '""', "0.0"):
            with self.subTest(raw=raw):
                manifest, error = workflow._parse_manifest(_manifest(
                    '{"decision": "split", "children": ['
                    '{"title": "A", "body": "a"},'
                    f'{{"title": "B", "body": "b", "depends_on": {raw}}}'
                    ']}'
                ))
                self.assertIsNone(manifest)
                self.assertIn("must be a list", error)

    def test_null_depends_on_treated_as_empty(self) -> None:
        # Explicit JSON null is treated the same as a missing key:
        # both signal "no dependencies". Only a non-list, non-null
        # value is a contract violation. This locks in the forgiving
        # behavior so a future tighten-up doesn't accidentally start
        # rejecting `"depends_on": null`.
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a", "depends_on": null}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertIsNotNone(manifest)

    def test_umbrella_flag_accepted(self) -> None:
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "umbrella": true, "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertTrue(manifest.get("umbrella"))

    def test_umbrella_default_missing(self) -> None:
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertIsNone(manifest.get("umbrella"))

    def test_umbrella_non_bool_rejected(self) -> None:
        # A typo like `"umbrella": "yes"` would be silently treated as
        # truthy if we coerced; reject so the standard invalid-manifest
        # HITL/resume loop catches it instead of mislabeling the parent.
        manifest, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "umbrella": "yes", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        ))
        self.assertIsNone(manifest)
        self.assertIn("umbrella", error)

    def test_displayed_schema_is_valid(self) -> None:
        # A literal-minded decomposer that copies the schema verbatim
        # must produce a manifest that survives _parse_manifest. If the
        # displayed example uses union notation or any other
        # non-JSON sugar, prompt-compliant runs would park awaiting
        # human for a self-inflicted reason. Round-trip the example
        # through the same parser the orchestrator runs on agent
        # output to keep the prompt and parser in lockstep.
        prompt = workflow._build_decompose_prompt(
            _TEST_SPEC, make_issue(1, title="example", body="some body"), "",
            [_TEST_SPEC],
        )
        m = workflow._MANIFEST_RE.search(prompt)
        self.assertIsNotNone(m, "prompt must contain a fenced example")
        manifest, error = workflow._parse_manifest(m.group(0))
        self.assertIsNone(
            error, f"displayed example failed to parse: {error}"
        )
        self.assertIsNotNone(manifest)


class BuildSingleDecisionCommentTest(unittest.TestCase):
    """The `single`-decision comment carries the context the decomposer
    gathered (affected files + notes) into the implementer, and tolerates
    any missing / malformed optional field without dropping the rationale.
    """

    def test_renders_rationale_files_and_notes(self) -> None:
        comment = workflow._build_single_decision_comment({
            "decision": "single",
            "rationale": "one small change",
            "affected_files": ["orchestrator/config.py", "tests/fakes.py"],
            "notes": "Bump the default and cover it in fakes.",
        })
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
        comment = workflow._build_single_decision_comment({
            "decision": "single",
            "rationale": "trivial",
        })
        self.assertEqual(
            comment,
            ":mag: decomposer says this fits one context: trivial",
        )

    def test_missing_rationale_uses_placeholder(self) -> None:
        # `_parse_manifest` does not validate single-branch fields, so a
        # non-string / absent rationale must not crash rendering.
        comment = workflow._build_single_decision_comment(
            {"decision": "single", "rationale": [1, 2, 3]}
        )
        self.assertIn("(no rationale provided)", comment)

    def test_drops_malformed_files_and_notes(self) -> None:
        # Non-list files, non-string entries, and non-string notes are
        # sanitized away rather than rendered or raised on.
        comment = workflow._build_single_decision_comment({
            "decision": "single",
            "rationale": "ok",
            "affected_files": ["good.py", "", 42, "  spaced.py  "],
            "notes": {"not": "a string"},
        })
        self.assertIn("- `good.py`", comment)
        self.assertIn("- `spaced.py`", comment)
        self.assertNotIn("- `42`", comment)
        self.assertNotIn("- ``", comment)
        self.assertNotIn("**Implementation notes:**", comment)

    def test_non_list_affected_files_omits_section(self) -> None:
        comment = workflow._build_single_decision_comment({
            "decision": "single",
            "rationale": "ok",
            "affected_files": "orchestrator/config.py",
        })
        self.assertNotIn("**Affected files:**", comment)
