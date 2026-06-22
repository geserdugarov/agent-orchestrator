# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import make_issue
from tests.workflow_helpers import _TEST_SPEC, _manifest


class ParseManifestTest(unittest.TestCase):
    def test_single_decision(self) -> None:
        msg = "I think this fits.\n\n" + _manifest(
            '{"decision": "single", "rationale": "small change"}'
        )
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(error)
        self.assertIsNotNone(data)
        self.assertEqual(data["decision"], "single")

    def test_split_decision_two_children(self) -> None:
        payload = (
            '{"decision": "split", "rationale": "too many surfaces", '
            '"children": ['
            '{"title": "A", "body": "do A", "depends_on": []},'
            '{"title": "B", "body": "do B", "depends_on": [0]}'
            ']}'
        )
        data, error = workflow._parse_manifest(_manifest(payload))
        self.assertIsNone(error)
        self.assertEqual(len(data["children"]), 2)
        self.assertEqual(data["children"][1]["depends_on"], [0])

    def test_no_fenced_block_returns_none_none(self) -> None:
        data, error = workflow._parse_manifest("just a question, no fence")
        self.assertIsNone(data)
        self.assertIsNone(error)

    def test_invalid_json_returns_error(self) -> None:
        data, error = workflow._parse_manifest(_manifest("{not json"))
        self.assertIsNone(data)
        self.assertIn("invalid JSON", error)

    def test_unknown_decision_rejected(self) -> None:
        data, error = workflow._parse_manifest(
            _manifest('{"decision": "maybe"}')
        )
        self.assertIsNone(data)
        self.assertIn("decision", error)

    def test_split_with_empty_children_rejected(self) -> None:
        data, error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": []}')
        )
        self.assertIsNone(data)
        self.assertIn("non-empty", error)

    def test_child_missing_title_rejected(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"body": "no title here"}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("title or body", error)

    def test_self_dependency_rejected(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "X", "body": "x", "depends_on": [0]}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("invalid dependency", error)

    def test_dep_cycle_rejected(self) -> None:
        # 0 -> 1 -> 0
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a", "depends_on": [1]},'
            '{"title": "B", "body": "b", "depends_on": [0]}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("cycle", error)

    def test_too_many_children_rejected(self) -> None:
        children = ",".join(
            f'{{"title": "T{i}", "body": "b{i}"}}' for i in range(11)
        )
        data, error = workflow._parse_manifest(_manifest(
            f'{{"decision": "split", "children": [{children}]}}'
        ))
        self.assertIsNone(data)
        self.assertIn("too many", error)

    def test_non_string_title_rejected(self) -> None:
        # JSON-valid manifest with a non-string title (here a number)
        # must be rejected before any side effects. Truthiness alone
        # would let `42` pass, but `gh.create_child_issue` (`body.rstrip()`
        # plus the PyGithub call) blows up only AFTER
        # `expected_children_count` has been persisted, forcing the
        # half-finished-recovery path instead of the clean
        # invalid-manifest HITL/resume loop.
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": 42, "body": "x"}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("title or body", error)

    def test_non_string_body_rejected(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "x", "body": ["a", "b"]}'
            ']}'
        ))
        self.assertIsNone(data)
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
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(data)
        self.assertIn("exactly one", error)
        self.assertIn("found 2", error)

    def test_content_after_manifest_rejected(self) -> None:
        # The prompt says "nothing else after" the manifest. Trailing
        # prose suggests the agent did not finish its final answer or
        # appended commentary that the orchestrator would ignore --
        # either way, surface to the human rather than silently act.
        msg = _manifest('{"decision": "single"}') + "\n\nP.S. hope this works"
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(data)
        self.assertIn("final block", error)

    def test_trailing_whitespace_after_manifest_accepted(self) -> None:
        # Pure whitespace (newlines/spaces) after the closing fence is a
        # benign formatting artifact and must NOT trip the "trailing
        # content" guard.
        msg = _manifest('{"decision": "single"}') + "\n\n   \n"
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(error)
        self.assertEqual(data["decision"], "single")

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
                data, error = workflow._parse_manifest(_manifest(
                    '{"decision": "split", "children": ['
                    '{"title": "A", "body": "a"},'
                    f'{{"title": "B", "body": "b", "depends_on": {raw}}}'
                    ']}'
                ))
                self.assertIsNone(data)
                self.assertIn("must be a list", error)

    def test_null_depends_on_treated_as_empty(self) -> None:
        # Explicit JSON null is treated the same as a missing key:
        # both signal "no dependencies". Only a non-list, non-null
        # value is a contract violation. This locks in the forgiving
        # behavior so a future tighten-up doesn't accidentally start
        # rejecting `"depends_on": null`.
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a", "depends_on": null}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertIsNotNone(data)

    def test_umbrella_flag_accepted(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "umbrella": true, "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertTrue(data.get("umbrella"))

    def test_umbrella_default_missing(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertIsNone(data.get("umbrella"))

    def test_umbrella_non_bool_rejected(self) -> None:
        # A typo like `"umbrella": "yes"` would be silently treated as
        # truthy if we coerced; reject so the standard invalid-manifest
        # HITL/resume loop catches it instead of mislabeling the parent.
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "umbrella": "yes", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("umbrella", error)

    def test_displayed_schema_example_is_valid_manifest(self) -> None:
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
        data, error = workflow._parse_manifest(m.group(0))
        self.assertIsNone(
            error, f"displayed example failed to parse: {error}"
        )
        self.assertIsNotNone(data)
