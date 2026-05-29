# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeIssue,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _iso_hours_ago,
    _manifest,
)


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
            make_issue(1, title="example", body="some body"), ""
        )
        m = workflow._MANIFEST_RE.search(prompt)
        self.assertIsNotNone(m, "prompt must contain a fenced example")
        data, error = workflow._parse_manifest(m.group(0))
        self.assertIsNone(
            error, f"displayed example failed to parse: {error}"
        )
        self.assertIsNotNone(data)


class HandleDecomposingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The decomposer drives the (no-label / `decomposing`) -> ready/blocked
    transitions. Single decision routes the parent to `ready`; split creates
    children with `ready`/`blocked` labels and parks the parent on `blocked`.
    Malformed or absent manifests park awaiting human.
    """

    def test_pickup_routes_to_decomposing(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(10)
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "trivial"}'
        )

        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess", last_message=manifest
                ),
            )

        # First label flip is to decomposing; the single-decision path then
        # flips it to ready on the same tick.
        self.assertEqual(gh.label_history[0], (10, "decomposing"))
        self.assertIn((10, "ready"), gh.label_history)
        self.assertTrue(any(
            "decomposing" in body
            for _, body in gh.posted_comments
        ))

    def test_decompose_decision_single_flips_to_ready(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(11, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "fits in one context"}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        self.assertIn((11, "ready"), gh.label_history)
        # No children created.
        self.assertEqual(gh.created_child_issues, [])
        data = gh.pinned_data(11)
        self.assertEqual(data.get("decomposer_agent"), config.DECOMPOSE_AGENT)
        self.assertEqual(data.get("decomposer_session_id"), "dec-sess")
        self.assertIn("decomposed_at", data)
        # Rationale surfaced in a comment.
        self.assertTrue(any(
            "fits in one context" in body for _, body in gh.posted_comments
        ))

    def test_decompose_decision_split_creates_children(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(12, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "rationale": "two pieces", "children": ['
            '{"title": "Add status subcommand", "body": "implement status", '
            '"depends_on": []},'
            '{"title": "Add pause subcommand", "body": "implement pause", '
            '"depends_on": []}'
            ']}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        # Parent is now blocked; both children created with `ready`.
        self.assertIn((12, "blocked"), gh.label_history)
        self.assertEqual(len(gh.created_child_issues), 2)
        for child in gh.created_child_issues:
            self.assertEqual(
                [l.name for l in child.labels], ["ready"],
            )
            self.assertIn(f"Parent: #{12}", child.body)

        data = gh.pinned_data(12)
        self.assertEqual(
            data.get("children"),
            [c.number for c in gh.created_child_issues],
        )
        # No deps -> dep_graph not persisted.
        self.assertNotIn("dep_graph", data)
        # Summary comment lists both child numbers.
        last_comment = next(
            body for n, body in gh.posted_comments if n == 12
            and ":bookmark_tabs:" in body
        )
        for child in gh.created_child_issues:
            self.assertIn(f"#{child.number}", last_comment)

    def test_decompose_split_umbrella_marks_parent_umbrella(self) -> None:
        # `umbrella: true` on a split decision means the parent has no
        # implementation work of its own; instead of `blocked` (which
        # would re-enter implementation after children resolve), it gets
        # the `umbrella` label and `_handle_umbrella` will close it once
        # every child reaches `done`.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "umbrella": true, '
            '"rationale": "parent is just a tracker", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"}'
            ']}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        # Parent reached `umbrella`, NOT `blocked`.
        labels = [lbl for n, lbl in gh.label_history if n == 50]
        self.assertIn("umbrella", labels)
        self.assertNotIn("blocked", labels)
        # Children created normally, with no-dep activation -> `ready`.
        self.assertEqual(len(gh.created_child_issues), 2)
        for child in gh.created_child_issues:
            self.assertEqual([l.name for l in child.labels], ["ready"])
        # `umbrella` flag persisted on parent state so the
        # half-finished recovery path can read it back after a SIGKILL.
        self.assertTrue(gh.pinned_data(50).get("umbrella"))
        # Summary comment mentions umbrella so a human glancing at the
        # thread sees what label the parent landed on.
        last_comment = next(
            body for n, body in gh.posted_comments if n == 50
            and ":bookmark_tabs:" in body
        )
        self.assertIn("umbrella", last_comment)

    def test_decompose_split_non_umbrella_default_marks_blocked(
        self,
    ) -> None:
        # Default for the umbrella flag is False -- a split manifest
        # without `umbrella` must still go through `blocked` so the
        # parent re-enters implementation after children resolve, the
        # legacy behavior.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        labels = [lbl for n, lbl in gh.label_history if n == 51]
        self.assertIn("blocked", labels)
        self.assertNotIn("umbrella", labels)
        # State records umbrella=False explicitly so a stale True from a
        # prior aborted decomposition cannot survive into recovery.
        self.assertEqual(gh.pinned_data(51).get("umbrella"), False)

    def test_decompose_split_with_deps_persists_dep_graph(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(13, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "First", "body": "do first", "depends_on": []},'
            '{"title": "Second", "body": "needs first", "depends_on": [0]}'
            ']}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        children = gh.created_child_issues
        self.assertEqual(len(children), 2)
        # child[0] has no deps -> ready; child[1] depends on [0] -> blocked.
        self.assertEqual([l.name for l in children[0].labels], ["ready"])
        self.assertEqual([l.name for l in children[1].labels], ["blocked"])

        data = gh.pinned_data(13)
        self.assertEqual(data.get("dep_graph"), {"1": [0]})
        # Each child's pinned state records the parent so the polling
        # loop's blocked-issue dispatch can recognize it as a child
        # rather than as an unattributed `blocked` parent.
        for child in children:
            self.assertEqual(
                gh.pinned_data(child.number).get("parent_number"), 13,
            )

    def test_decompose_parks_if_decomposer_left_commits(self) -> None:
        # The decomposer is supposed to be read-only. If it commits in the
        # parent's worktree, the implementer recovery path in
        # `_handle_implementing` would later see `_has_new_commits` -> True
        # and push decomposer-authored work as if it were implementation.
        # Defensive park is the surface that catches this.
        gh = FakeGitHubClient()
        issue = make_issue(40, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits"}')

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
            has_new_commits=True,
        )

        data = gh.pinned_data(40)
        self.assertTrue(data.get("awaiting_human"))
        # Did NOT advance to ready -- the operator must clean up first.
        self.assertNotIn((40, "ready"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("read-only", last_comment)

    def test_decompose_parks_if_decomposer_left_dirty_files(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(41, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits"}')

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
            dirty_files=("foo.py",),
        )

        data = gh.pinned_data(41)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((41, "ready"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("read-only", last_comment)

    def test_decompose_malformed_manifest_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(14, label="decomposing")
        gh.add_issue(issue)
        bad = _manifest("{not really json")

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=bad),
        )

        data = gh.pinned_data(14)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("manifest invalid", last_comment)
        # Last decomposer message quoted into the HITL ping so the human
        # can see what the agent actually emitted.
        self.assertIn("not really json", last_comment)
        # Decomposer session recorded so the resume on human reply uses
        # the right backend even if DECOMPOSE_AGENT flips between ticks.
        self.assertEqual(data.get("decomposer_session_id"), "dec-sess")

    def test_decompose_no_manifest_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(15, label="decomposing")
        gh.add_issue(issue)

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess",
                last_message="Should the new commands accept a --json flag?",
                stderr="benign warning",
            ),
        )

        data = gh.pinned_data(15)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("needs your input", last_comment)
        self.assertIn("--json flag", last_comment)
        # Real decomposer text -> no stderr block (would be noise).
        self.assertNotIn("Decomposer stderr", last_comment)

    def test_decompose_silent_failure_surfaces_stderr(self) -> None:
        # No manifest AND no final message: the decomposer subprocess
        # produced literally nothing. Surface its stderr/exit_code in
        # the park so the operator can tell a CF / quota / auth failure
        # apart from a model that just had no opinion.
        gh = FakeGitHubClient()
        issue = make_issue(115, label="decomposing")
        gh.add_issue(issue)

        with self.assertLogs("orchestrator.workflow", level="WARNING") as logs:
            self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess",
                    last_message="",
                    stderr="rate limit exceeded; retry after 60s",
                    exit_code=3,
                ),
            )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("(decomposer produced no final message)", last_comment)
        self.assertIn("_Decomposer stderr (last 1KB):_", last_comment)
        self.assertIn("rate limit exceeded", last_comment)
        self.assertIn("_Decomposer exit code:_ 3", last_comment)
        self.assertTrue(any(
            "decomposer produced no final message" in r.getMessage()
            and "exit_code=3" in r.getMessage()
            for r in logs.records
        ))

    def test_decompose_resume_on_human_reply(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(16, label="decomposing")
        issue.comments.append(FakeComment(
            id=1100, body="please split into 2", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            16,
            awaiting_human=True,
            last_action_comment_id=900,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"}'
            ']}'
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        # Resume happened with the human comment quoted, on the locked
        # backend.
        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "claude")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dec-sess")
        self.assertIn("please split into 2", call.args[1])

        self.assertIn((16, "blocked"), gh.label_history)
        self.assertEqual(len(gh.created_child_issues), 2)
        self.assertFalse(gh.pinned_data(16).get("awaiting_human"))

    def test_decompose_agent_locked_on_resume(self) -> None:
        # Pinned state recorded `decomposer_agent="claude"`. Even after
        # DECOMPOSE_AGENT flips to "codex", the resume must stick with
        # claude -- session ids do not bridge across backends.
        gh = FakeGitHubClient()
        issue = make_issue(17, label="decomposing")
        issue.comments.append(FakeComment(
            id=1100, body="any update?", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            17,
            awaiting_human=True,
            last_action_comment_id=900,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )
        manifest = _manifest(
            '{"decision": "single", "rationale": "trivial"}'
        )

        with patch.object(config, "DECOMPOSE_AGENT", "codex"):
            mocks = self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess", last_message=manifest
                ),
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "claude")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "dec-sess",
        )

    def test_decompose_retry_cap_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(18, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            18,
            retry_count=config.MAX_RETRIES_PER_DAY,
            retry_window_start=_iso_hours_ago(1),
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(18).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(
            f"hit retry cap ({config.MAX_RETRIES_PER_DAY}/day) for decomposing",
            last_comment,
        )

    def test_decompose_off_falls_back_to_legacy_pickup(self) -> None:
        # End-to-end: with DECOMPOSE=off, the unlabeled issue must skip
        # the decomposer entirely and route straight to implementing
        # exactly as the bootstrap-milestone path did. No `decomposing`
        # label and no decomposer pinned-state keys are written.
        gh = FakeGitHubClient()
        issue = make_issue(19)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="done"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        self.assertNotIn(
            "decomposing", [lbl for _, lbl in gh.label_history],
        )
        self.assertIn((19, "implementing"), gh.label_history)
        self.assertEqual(gh.created_child_issues, [])
        data = gh.pinned_data(19)
        self.assertNotIn("decomposer_agent", data)
        self.assertNotIn("decomposer_session_id", data)

    def test_decompose_off_routes_decomposing_label_to_implementing(
        self,
    ) -> None:
        # The DECOMPOSE kill switch must apply to issues that were
        # already labeled `decomposing` (or parked there awaiting a
        # human) when the operator restarts with the flag off.
        # Without this, `_process_issue` still calls `_handle_decomposing`
        # for that label and the disabled rollout keeps spawning the
        # decomposer, producing manifests and child issues that the
        # operator explicitly disabled.
        gh = FakeGitHubClient()
        issue = make_issue(20, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            20,
            awaiting_human=True,
            park_reason="(test) decomposer asked a question",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
            last_action_comment_id=900,
            pickup_comment_id=100,
        )

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="implemented"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        # The agent that did run was the dev agent (legacy implementing
        # took over), not the decomposer.
        mocks["run_agent"].assert_called_once()
        self.assertEqual(
            mocks["run_agent"].call_args.args[0], config.DEV_AGENT,
            "kill switch must route to the dev backend, not decomposer",
        )

        # Label transitioned to implementing. Must never have routed
        # through `blocked` (that would have implied children created).
        labels = [lbl for _, lbl in gh.label_history]
        self.assertIn("implementing", labels)
        self.assertNotIn("blocked", labels)

        # Decomposer-side park state cleared so `_handle_implementing`'s
        # awaiting_human resume branch doesn't fire on stale state.
        data = gh.pinned_data(20)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

        # Routing comment posted; no children created.
        self.assertTrue(any(
            "decomposition is disabled" in body
            for _, body in gh.posted_comments
        ))
        self.assertEqual(gh.created_child_issues, [])

    def test_decompose_off_ratchets_last_action_past_decomposing_comments(
        self,
    ) -> None:
        # When DECOMPOSE flips off mid-flight, decomposing-era human
        # comments newer than `last_action_comment_id` must be marked
        # consumed before falling into `_handle_implementing`. The
        # implementer reads the full thread via `_recent_comments_text`
        # at spawn, so the dev sees those comments at implementation
        # time. Without the ratchet, the validating->in_review
        # watermark seed later treats those same comments as fresh PR
        # feedback and bounces the dev unnecessarily -- exactly the
        # replay `_handle_ready` already prevents on the single-decision
        # happy path.
        gh = FakeGitHubClient()
        issue = make_issue(21, label="decomposing")
        # Decomposer-era HITL comments newer than the parked
        # last_action_comment_id (which is anchored on the original
        # pickup or an earlier decomposer round).
        issue.comments.append(FakeComment(
            id=950, body="please reconsider", user=FakeUser("alice"),
        ))
        issue.comments.append(FakeComment(
            id=960, body="the title is wrong", user=FakeUser("bob"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            21,
            awaiting_human=True,
            park_reason="(test) decomposer asked a question",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
            last_action_comment_id=900,
            pickup_comment_id=100,
        )

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="implemented"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        data = gh.pinned_data(21)
        last_action = data.get("last_action_comment_id")
        # Must be past the highest decomposing-era comment so the
        # in_review watermark seed treats them as already-consumed.
        self.assertIsInstance(last_action, int)
        self.assertGreaterEqual(last_action, 960)

    def test_decompose_off_does_not_lower_last_action_comment_id(self) -> None:
        # The ratchet is one-way. If `last_action_comment_id` is
        # already past the latest visible comment (e.g. a prior tick
        # consumed everything and a later high-id comment hasn't been
        # posted yet), the kill-switch path must NOT lower it.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="decomposing")
        # One older comment; latest visible id is 500.
        issue.comments.append(FakeComment(
            id=500, body="early note", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            22,
            awaiting_human=True,
            last_action_comment_id=10000,
            pickup_comment_id=100,
        )

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="implemented"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        # Must not regress below the previously persisted high water mark.
        self.assertGreaterEqual(
            gh.pinned_data(22).get("last_action_comment_id"), 10000,
        )

    def test_decompose_off_still_finalizes_half_finished_split(self) -> None:
        # If a SIGKILL crashed a split between the parent's last
        # incremental `children` write and the parent label flip,
        # turning the kill switch on must NOT abandon the orphan
        # children -- they already exist on GitHub. Half-finished
        # recovery sits ABOVE the kill-switch bailout precisely so a
        # disabled rollout can still finalize the in-flight state to
        # `blocked` without spawning the decomposer.
        gh = FakeGitHubClient()
        parent = make_issue(50, label="decomposing")
        gh.add_issue(parent)
        for child_number in (101, 102):
            child = make_issue(child_number, label="blocked")
            gh.add_issue(child)
            gh.seed_state(
                child_number, parent_number=50,
                created_at="2026-05-03T00:00:00+00:00",
            )
        gh.seed_state(
            50,
            children=[101, 102],
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, parent),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        labels = [lbl for _, lbl in gh.label_history]
        self.assertIn("blocked", labels)
        self.assertNotIn("implementing", labels)
        self.assertEqual(gh.created_child_issues, [])

    def test_decompose_persists_children_incrementally(self) -> None:
        # Each successful child creation must flush the parent's
        # `children` list before the next iteration starts. Without this,
        # a process kill (no exception) between iterations leaves the
        # parent without a `children` record, the next tick re-spawns the
        # decomposer, and duplicate child issues are created. We probe
        # the contract by snapshotting the parent's persisted `children`
        # list at the moment each child creation begins.
        gh = FakeGitHubClient()
        issue = make_issue(80, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"},'
            '{"title": "C", "body": "c"}'
            ']}'
        )

        snapshots: list[list] = []
        real_create = gh.create_child_issue

        def spy_create(**kwargs):
            snapshots.append(list(gh.pinned_data(80).get("children") or []))
            return real_create(**kwargs)

        gh.create_child_issue = spy_create

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        # iter 0: no children yet. iter 1: child[0] already persisted.
        # iter 2: child[0] + child[1] already persisted.
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(snapshots[0], [])
        self.assertEqual(len(snapshots[1]), 1)
        self.assertEqual(len(snapshots[2]), 2)
        self.assertEqual(
            len(gh.pinned_data(80).get("children") or []), 3,
        )

    def test_half_finished_recovery_flips_to_blocked(self) -> None:
        # Simulate: a prior tick created+persisted children but crashed
        # before flipping the parent label from `decomposing` to
        # `blocked`. The next tick must NOT re-spawn the decomposer
        # (would create duplicate children); it must finalize the parent
        # transition. The parent's `_handle_blocked` activates no-dep
        # children on a subsequent tick.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="decomposing")
        gh.add_issue(issue)
        # Children already exist on GitHub with `parent_number` seeded --
        # the crash happened AFTER both child seeds, between the parent's
        # last incremental write and the parent label flip.
        for child_number in (101, 102):
            child = make_issue(child_number, label="blocked")
            gh.add_issue(child)
            gh.seed_state(
                child_number, parent_number=50,
                created_at="2026-05-03T00:00:00+00:00",
            )
        gh.seed_state(
            50,
            children=[101, 102],
            decomposed_at="2026-05-03T00:00:00+00:00",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Decomposer was NOT respawned; no new children were created.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertIn((50, "blocked"), gh.label_history)
        # Children + decomposed_at preserved.
        data = gh.pinned_data(50)
        self.assertEqual(data.get("children"), [101, 102])

    def test_half_finished_recovery_with_awaiting_human_holds(self) -> None:
        # If the prior tick parked awaiting_human after partial child
        # creation, the recovery must NOT silently flip the parent to
        # `blocked`; the human's intervention is still required.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            51,
            children=[201],
            awaiting_human=True,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        # Label NOT flipped; human still owns it.
        self.assertNotIn((51, "blocked"), gh.label_history)
        self.assertTrue(gh.pinned_data(51).get("awaiting_human"))

    def test_partial_children_recovery_parks(self) -> None:
        # SIGKILL between iterations leaves a partial `children` list
        # that the half-finished recovery used to silently treat as
        # complete -- stranding any un-created dependents and never
        # creating the missing children. With `expected_children_count`
        # persisted up-front, the recovery distinguishes partial from
        # complete and parks awaiting human.
        gh = FakeGitHubClient()
        issue = make_issue(52, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            52,
            children=[101],
            expected_children_count=3,
            decomposed_at="2026-05-03T00:00:00+00:00",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        # Parked, not finalized to blocked.
        self.assertNotIn((52, "blocked"), gh.label_history)
        data = gh.pinned_data(52)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("crashed mid-way", last_comment)
        self.assertIn("1 of 3", last_comment)

    def test_orphan_child_recovery_parks_when_no_children_recorded(
        self,
    ) -> None:
        # SIGKILL between `create_child_issue` returning and the parent's
        # incremental `children` write leaves the parent with
        # `expected_children_count` set but zero recorded children, while
        # an orphan child issue exists on GitHub. The previous recovery
        # branch only fired when `state.get("children")` was truthy, so
        # this case fell through, the decomposer was respawned, and a
        # different manifest produced duplicate child issues alongside
        # the orphan.
        gh = FakeGitHubClient()
        issue = make_issue(53, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            53,
            expected_children_count=2,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertNotIn((53, "blocked"), gh.label_history)
        data = gh.pinned_data(53)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("crashed mid-way", last_comment)
        self.assertIn("0 of 2", last_comment)

    def test_recovery_seeds_missing_parent_number_on_orphan_child(self) -> None:
        # SIGKILL between the parent's child-record write and the child's
        # pinned-state seed for the LAST child satisfies
        # `len(children) == expected_children_count` but leaves that child
        # orphaned (label=blocked, no `parent_number`). A prior
        # `_handle_blocked` tick may have already parked the orphan as
        # "manual relabel suspected" with `awaiting_human=True`. Without
        # repair, recovery finalizes the parent to `blocked`, the parent's
        # walk later flips the orphan to `ready`, and
        # `_handle_implementing` reads the stale park and sits waiting on
        # a human reply that never comes.
        gh = FakeGitHubClient()
        parent = make_issue(60, label="decomposing")
        gh.add_issue(parent)
        # First child seeded normally; second is the orphan.
        child_a = make_issue(601, label="blocked")
        child_b = make_issue(602, label="blocked")
        gh.add_issue(child_a)
        gh.add_issue(child_b)
        gh.seed_state(
            601, parent_number=60, created_at="2026-05-03T00:00:00+00:00",
        )
        gh.seed_state(
            602,
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=999,
        )
        gh.seed_state(
            60,
            children=[601, 602],
            expected_children_count=2,
            decomposed_at="2026-05-03T00:00:00+00:00",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertIn((60, "blocked"), gh.label_history)
        # Orphan got parent_number seeded and stale park cleared.
        orphan_state = gh.pinned_data(602)
        self.assertEqual(orphan_state.get("parent_number"), 60)
        self.assertFalse(orphan_state.get("awaiting_human"))
        # Healthy child untouched.
        healthy_state = gh.pinned_data(601)
        self.assertEqual(healthy_state.get("parent_number"), 60)

    def test_decompose_split_persists_expected_count_first(self) -> None:
        # `expected_children_count` MUST be on the parent before any
        # child is created on GitHub. Otherwise a SIGKILL after the
        # first child creation leaves `children=[#x]` without an
        # `expected_children_count`, and the recovery (legacy branch)
        # incorrectly finalizes to `blocked`.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"}'
            ']}'
        )

        seen_expected: list[Optional[int]] = []
        real_create = gh.create_child_issue

        def spy_create(**kwargs):
            seen_expected.append(
                gh.pinned_data(82).get("expected_children_count")
            )
            return real_create(**kwargs)

        gh.create_child_issue = spy_create

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        self.assertEqual(seen_expected[0], 2)
        self.assertEqual(gh.pinned_data(82).get("expected_children_count"), 2)

    def test_parent_records_child_before_seeding_child_state(self) -> None:
        # Order matters: parent state records the new child BEFORE the
        # child's pinned state is seeded. Otherwise a SIGKILL between
        # `create_child_issue` returning and the parent write leaves
        # an orphan child (parent doesn't know about it), and the next
        # tick re-spawns the decomposer to create a duplicate.
        gh = FakeGitHubClient()
        issue = make_issue(83, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        )

        # Wrap write_pinned_state so we can observe the order of writes
        # against parent vs child.
        seen_children_before_child_seed: list[list] = []
        real_write = gh.write_pinned_state

        def spy_write(target_issue, state):
            if target_issue.number != 83:
                # Child write -- parent state should already have the
                # child number recorded by now.
                seen_children_before_child_seed.append(
                    list(gh.pinned_data(83).get("children") or [])
                )
            return real_write(target_issue, state)

        gh.write_pinned_state = spy_write

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        # Exactly one child was created and its pinned state was seeded
        # AFTER the parent recorded the child number.
        self.assertEqual(len(seen_children_before_child_seed), 1)
        self.assertEqual(
            len(seen_children_before_child_seed[0]), 1,
            "parent must record the child number before the child's "
            "pinned state is seeded",
        )

    def test_decompose_uses_separate_worktree_from_implementer(self) -> None:
        # The decomposer must NOT taint the implementer's per-issue branch.
        # If it shared `_ensure_worktree`, a `split` decision would leave
        # the local `orchestrator/issue-<n>` branch anchored at the
        # origin/main snapshot the decomposer saw, and the parent's
        # eventual implementer (after children merged to main) would
        # commit on a stale base.
        gh = FakeGitHubClient()
        issue = make_issue(70, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "fits"}'
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        mocks["_ensure_decompose_worktree"].assert_called_with(_TEST_SPEC, 70)
        mocks["_ensure_worktree"].assert_not_called()
        # Cleanup runs at function exit so the next consumer of issue 70
        # (here _handle_ready -> _handle_implementing on the next tick)
        # starts from a fresh checkout.
        mocks["_cleanup_decompose_worktree"].assert_called_with(_TEST_SPEC, 70)

    def test_decompose_skips_cleanup_on_dirty_park(self) -> None:
        # Operator inspection requires the decomposer's worktree to
        # outlive the dirty/commits park.
        gh = FakeGitHubClient()
        issue = make_issue(71, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits"}')

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
            has_new_commits=True,
        )

        self.assertTrue(gh.pinned_data(71).get("awaiting_human"))
        mocks["_cleanup_decompose_worktree"].assert_not_called()

    def test_decompose_skips_cleanup_while_awaiting_human(self) -> None:
        # On the tick AFTER a dirty/commits park, awaiting_human is True
        # and no human reply has arrived yet. The handler must not clean
        # up the decomposer worktree -- the HITL message asks the operator
        # to inspect and reset it, and a subsequent-tick cleanup would
        # silently delete that state out from under them.
        gh = FakeGitHubClient()
        issue = make_issue(73, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            73,
            awaiting_human=True,
            last_action_comment_id=999,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_decompose_worktree"].assert_not_called()

    def test_decompose_handles_non_string_rationale(self) -> None:
        # JSON-valid manifest with a non-string rationale (`[1,2,3]`,
        # `{}`, `42`) must not crash the handler at `.strip()` after
        # the agent already ran. Coerce to the placeholder.
        gh = FakeGitHubClient()
        issue = make_issue(72, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": [1, 2, 3]}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        self.assertIn((72, "ready"), gh.label_history)
        self.assertFalse(gh.pinned_data(72).get("awaiting_human"))
        rationale_comment = next(
            body for n, body in gh.posted_comments
            if n == 72 and ":mag:" in body
        )
        self.assertIn("(no rationale provided)", rationale_comment)


class HandleReadyTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_handle_ready_routes_to_implementing_same_tick(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label="ready")
        gh.add_issue(issue)

        mocks = self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="implemented"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # Label flips to implementing on the same tick; the dev agent ran
        # and a PR opened.
        self.assertEqual(gh.label_history[0], (20, "implementing"))
        mocks["run_agent"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # pickup_comment_id seeded so the validating handoff can anchor
        # the in_review watermark seed on it.
        data = gh.pinned_data(20)
        self.assertIn("pickup_comment_id", data)
        self.assertIn("created_at", data)

    def test_handle_ready_keeps_existing_pickup_state(self) -> None:
        # If pickup state was already seeded (e.g. by a re-tick after the
        # legacy pickup path), don't double-post the picking-this-up
        # comment.
        gh = FakeGitHubClient()
        issue = make_issue(21, label="ready")
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pickup_comment_id=500,
            created_at="2026-05-03T00:00:00+00:00",
        )

        before = len(gh.posted_comments)
        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # The "picking this up; starting implementation" comment was NOT
        # re-posted. (`_on_commits` still posts a `:sparkles:` comment.)
        new_comments = gh.posted_comments[before:]
        self.assertFalse(any(
            "picking this up" in body for _, body in new_comments
        ))

    def test_handle_ready_marks_pre_existing_comments_consumed(self) -> None:
        # A parent that came through `decomposing` -> `blocked` ->
        # all-children-done -> `ready` carries a `pickup_comment_id`
        # anchored on the original "decomposing" comment. Any human
        # feedback posted while children were resolving sits at a
        # comment id ABOVE pickup, so the in_review watermark seed
        # would classify it as post-pickup unconsumed PR feedback and
        # bounce the PR back to validating after the implementer has
        # already incorporated it. _handle_ready must bump
        # `last_action_comment_id` past the latest visible comment so
        # `_seed_watermark_past_self`'s `consumed_through` walk treats
        # those decomposing/blocked-era comments as already-fed-to-the-dev.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="ready")
        # Decomposing-era human comment -- id well above the original
        # pickup comment id.
        issue.comments.append(FakeComment(
            id=2050, body="please use snake_case",
            user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pickup_comment_id=500,
            created_at="2026-05-03T00:00:00+00:00",
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        data = gh.pinned_data(22)
        last_action = data.get("last_action_comment_id")
        self.assertIsNotNone(
            last_action,
            "last_action_comment_id must be set so the in_review "
            "handoff treats decomposing-era comments as consumed",
        )
        self.assertGreaterEqual(int(last_action), 2050)

    def test_handle_ready_does_not_lower_existing_last_action(self) -> None:
        # If a prior decomposing park already advanced
        # `last_action_comment_id` past everything, _handle_ready must
        # not regress it. Latest comment id might be smaller than the
        # park id when the latest is the orchestrator's own pinned-state
        # comment from a fresh seed (low id) and the prior park id was
        # higher.
        gh = FakeGitHubClient()
        issue = make_issue(23, label="ready")
        gh.add_issue(issue)
        gh.seed_state(
            23,
            pickup_comment_id=500,
            last_action_comment_id=9999,
            created_at="2026-05-03T00:00:00+00:00",
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        data = gh.pinned_data(23)
        self.assertGreaterEqual(int(data["last_action_comment_id"]), 9999)


class HandleBlockedTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seed_parent_with_children(
        self,
        *,
        parent_number: int,
        child_labels: list[Optional[str]],
        dep_graph: Optional[dict] = None,
    ) -> tuple[FakeGitHubClient, FakeIssue, list[FakeIssue]]:
        gh = FakeGitHubClient()
        parent = make_issue(parent_number, label="blocked")
        gh.add_issue(parent)
        children: list[FakeIssue] = []
        for i, lbl in enumerate(child_labels):
            child = make_issue(parent_number * 10 + i + 1, label=lbl)
            gh.add_issue(child)
            children.append(child)
        seed = {
            "children": [c.number for c in children],
            "decomposer_agent": "claude",
            "decomposer_session_id": "dec-sess",
        }
        if dep_graph is not None:
            seed["dep_graph"] = dep_graph
        gh.seed_state(parent_number, **seed)
        return gh, parent, children

    def test_all_children_done_flips_parent_to_ready(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=30, child_labels=["done", "done"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((30, "ready"), gh.label_history)
        self.assertTrue(any(
            "all children resolved" in body
            for _, body in gh.posted_comments
        ))

    def test_some_children_in_progress_no_op(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=31,
            child_labels=["done", "implementing"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # No label flip on parent and no comment posted on the parent.
        self.assertNotIn((31, "ready"), gh.label_history)
        self.assertEqual(
            [b for n, b in gh.posted_comments if n == 31], [],
        )

    def test_rejected_child_parks_parent(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=32,
            child_labels=["done", "rejected"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(32)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rejected", last_comment)
        self.assertIn(f"#{children[1].number}", last_comment)

    def test_manually_closed_child_parks_parent(self) -> None:
        # A child closed manually (e.g. via the GitHub UI) before
        # reaching `in_review` is invisible to `list_pollable_issues`
        # (which only sweeps closed issues for `in_review`). Its
        # workflow label stays frozen, so without this branch the
        # parent reads the stale label, neither the rejected nor the
        # all-done branch fires, and the parent waits forever for a
        # child that is gone. Park it for human adjudication, exactly
        # like a rejected child.
        gh = FakeGitHubClient()
        parent = make_issue(40, label="blocked")
        gh.add_issue(parent)
        # children[0]: properly done -- closed with label `done`.
        done_child = make_issue(401, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: manually closed mid-implementation. Label stays
        # `implementing` because no orchestrator transition closed it.
        closed_child = make_issue(402, label="implementing")
        closed_child.closed = True
        gh.add_issue(closed_child)
        gh.seed_state(40, children=[401, 402])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(40)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("closed without reaching", last_comment)
        self.assertIn("#402", last_comment)
        # Crucially: the parent must NOT have flipped to `ready`. With
        # only the all-done branch, the manually-closed child carrying
        # a non-"done" label correctly fails the `all(lbl == "done")`
        # check; but if a future change lowered that bar (e.g. "all
        # closed"), this assertion would catch the regression.
        self.assertNotIn((40, "ready"), gh.label_history)

    def test_closed_in_review_child_does_not_falsely_park_parent(
        self,
    ) -> None:
        # state=closed + label=in_review is the externally-merged
        # transient: the closed-in_review sweep in
        # `list_pollable_issues` picks the child up next tick and
        # `_handle_in_review` finalizes it to done/rejected. The
        # blocked parent must NOT pre-empt that finalization with a
        # manual-close park -- treating this as a manual override
        # would strand legitimately externally-merged children.
        gh = FakeGitHubClient()
        parent = make_issue(41, label="blocked")
        gh.add_issue(parent)
        in_review_child = make_issue(411, label="in_review")
        in_review_child.closed = True
        gh.add_issue(in_review_child)
        other_child = make_issue(412, label="implementing")
        gh.add_issue(other_child)
        gh.seed_state(41, children=[411, 412])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(41)
        self.assertFalse(data.get("awaiting_human"))
        # Parent stays `blocked`: no `ready` flip while other_child is
        # still implementing, and no manual-close park comment posted.
        self.assertNotIn((41, "ready"), gh.label_history)
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 41
        ))

    def test_manually_closed_child_with_no_label_parks_parent(self) -> None:
        # Defensive corner: a child with no workflow label at all
        # (e.g. a label was manually stripped before the issue was
        # closed) is also invisible to the closed-in_review sweep.
        # The "manually closed" branch must catch it -- otherwise the
        # parent would still wait forever.
        gh = FakeGitHubClient()
        parent = make_issue(42, label="blocked")
        gh.add_issue(parent)
        unlabeled_closed = make_issue(421, label=None)
        unlabeled_closed.closed = True
        gh.add_issue(unlabeled_closed)
        gh.seed_state(42, children=[421])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(42)
        self.assertTrue(data.get("awaiting_human"))
        self.assertTrue(any(
            "closed without reaching" in body and "#421" in body
            for _, body in gh.posted_comments
        ))

    def test_unblocks_middle_child_when_dep_done(self) -> None:
        # children[0] is done; children[1] depends on [0] and is currently
        # blocked. Next blocked tick must relabel children[1] to `ready`.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=33,
            child_labels=["done", "blocked"],
            dep_graph={"1": [0]},
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # children[1] flipped to ready by the dep-graph walk; parent
        # stays blocked because children[1] is not yet done.
        flipped = [
            new for issue_n, new in gh.label_history
            if issue_n == children[1].number
        ]
        self.assertEqual(flipped, ["ready"])
        self.assertNotIn((33, "ready"), gh.label_history)

    def test_blocked_with_no_recorded_children_parks(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(34, label="blocked")
        gh.add_issue(parent)
        # No children pinned.
        gh.seed_state(34, decomposer_agent="claude")

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(34)
        self.assertTrue(data.get("awaiting_human"))

    def test_blocked_child_with_parent_number_is_noop(self) -> None:
        # A dependency-blocked child created by the decomposer carries
        # `parent_number` in its pinned state but no `children` of its
        # own. Polling routes it through `_handle_blocked`, which must
        # leave it alone -- the parent's dep-graph walk is what
        # eventually relabels it `ready`. Without the parent_number
        # branch this would park the child as "manual relabel suspected"
        # and leave `awaiting_human=True` behind, which would then
        # corrupt the implementation phase once the parent unblocks it.
        gh = FakeGitHubClient()
        child = make_issue(35, label="blocked")
        gh.add_issue(child)
        gh.seed_state(35, parent_number=30)

        before_comments = list(gh.posted_comments)
        before_labels = list(gh.label_history)

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        data = gh.pinned_data(35)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(gh.posted_comments, before_comments)
        self.assertEqual(gh.label_history, before_labels)

    def test_no_dep_blocked_child_flipped_to_ready_by_walk(self) -> None:
        # Activation-recovery path: a no-dep child got stuck as `blocked`
        # because the decomposer's same-tick activation step crashed
        # (network blip etc.). The parent's `_handle_blocked` walk must
        # treat empty deps as deps-satisfied and flip the child to
        # `ready` so implementation can start.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=36,
            child_labels=["blocked", "blocked"],
            # No dep_graph -- both children have no recorded deps.
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Both children flipped to `ready`. Parent stays `blocked`
        # because no children are `done` yet.
        for child in children:
            flipped = [
                new for issue_n, new in gh.label_history
                if issue_n == child.number
            ]
            self.assertEqual(flipped, ["ready"])
        self.assertNotIn((36, "ready"), gh.label_history)

    def test_blocked_clears_awaiting_human_after_all_done(self) -> None:
        # A prior tick parked the parent on `awaiting_human=True` because
        # one child was `rejected`. The operator fixed the rejection
        # off-band; eventually all children become `done`. The parent
        # flip to `ready` MUST clear the stale park so
        # `_handle_implementing` (next tick) starts a fresh implementer
        # run rather than routing through `_resume_developer_on_human_reply`
        # and either replaying long-stale comments or sitting silent.
        gh = FakeGitHubClient()
        parent = make_issue(38, label="blocked")
        gh.add_issue(parent)
        child_a = make_issue(381, label="done")
        child_b = make_issue(382, label="done")
        gh.add_issue(child_a)
        gh.add_issue(child_b)
        gh.seed_state(
            38,
            children=[381, 382],
            awaiting_human=True,
            park_reason="rejected_child",
            last_action_comment_id=999,
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((38, "ready"), gh.label_history)
        data = gh.pinned_data(38)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))


class HandleUmbrellaTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Umbrella parents have no implementation of their own; the only
    terminal path is "every child resolved -> close the umbrella as
    `done`". The rejected/manually-closed/dep-graph-walk branches mirror
    `_handle_blocked`."""

    def _seed_umbrella_with_children(
        self,
        *,
        parent_number: int,
        child_labels: list[Optional[str]],
        dep_graph: Optional[dict] = None,
    ) -> tuple[FakeGitHubClient, FakeIssue, list[FakeIssue]]:
        gh = FakeGitHubClient()
        parent = make_issue(parent_number, label="umbrella")
        gh.add_issue(parent)
        children: list[FakeIssue] = []
        for i, lbl in enumerate(child_labels):
            child = make_issue(parent_number * 10 + i + 1, label=lbl)
            gh.add_issue(child)
            children.append(child)
        seed = {
            "children": [c.number for c in children],
            "umbrella": True,
        }
        if dep_graph is not None:
            seed["dep_graph"] = dep_graph
        gh.seed_state(parent_number, **seed)
        return gh, parent, children

    def test_dispatcher_routes_umbrella_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(60, label="umbrella")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_umbrella") as handler:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)

    def test_all_children_done_closes_umbrella_as_done(self) -> None:
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=61, child_labels=["done", "done"],
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Terminal `done` label and the issue is closed -- mirrors how
        # the merged path finalizes a regular issue.
        self.assertIn((61, "done"), gh.label_history)
        self.assertTrue(parent.closed)
        # `umbrella_resolved_at` stamp recorded so a future audit can
        # tell automatic-resolution apart from a manual close.
        self.assertIn("umbrella_resolved_at", gh.pinned_data(61))
        self.assertTrue(any(
            "all children resolved" in body and "closing umbrella" in body
            for n, body in gh.posted_comments if n == 61
        ))

    def test_some_children_in_progress_no_op(self) -> None:
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=62, child_labels=["done", "implementing"],
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertNotIn((62, "done"), gh.label_history)
        self.assertFalse(parent.closed)
        self.assertEqual(
            [b for n, b in gh.posted_comments if n == 62], [],
        )

    def test_rejected_child_parks_umbrella(self) -> None:
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=63, child_labels=["done", "rejected"],
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(63)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((63, "done"), gh.label_history)
        self.assertFalse(parent.closed)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rejected", last_comment)
        self.assertIn(f"#{children[1].number}", last_comment)

    def test_manually_closed_child_parks_umbrella(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(64, label="umbrella")
        gh.add_issue(parent)
        done_child = make_issue(641, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        closed_child = make_issue(642, label="implementing")
        closed_child.closed = True
        gh.add_issue(closed_child)
        gh.seed_state(64, children=[641, 642], umbrella=True)

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(64)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((64, "done"), gh.label_history)
        self.assertFalse(parent.closed)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("closed without reaching", last_comment)
        self.assertIn("#642", last_comment)

    def test_unblocks_middle_child_when_dep_done(self) -> None:
        # A child stuck `blocked` on a dep that's now `done` should be
        # flipped to `ready` exactly as `_handle_blocked` does -- an
        # umbrella's children can still depend on each other.
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=65,
            child_labels=["done", "blocked"],
            dep_graph={"1": [0]},
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        flipped = [
            new for issue_n, new in gh.label_history
            if issue_n == children[1].number
        ]
        self.assertEqual(flipped, ["ready"])
        self.assertNotIn((65, "done"), gh.label_history)
        self.assertFalse(parent.closed)

    def test_umbrella_with_no_recorded_children_parks(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(66, label="umbrella")
        gh.add_issue(parent)
        gh.seed_state(66, umbrella=True)

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(66)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((66, "done"), gh.label_history)
        self.assertFalse(parent.closed)


class CreateChildIssueAlwaysUsesParentRepoTest(unittest.TestCase):
    """`create_child_issue` is structurally bound to `self.repo` so a
    misuse cannot accidentally file a child against a different repo
    than the parent. Worth a regression test anyway.
    """

    def test_calls_self_repo_create_issue_with_parent_link(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        sentinel = MagicMock(name="created_issue")
        client.repo.create_issue.return_value = sentinel

        out = client.create_child_issue(
            title="A", body="do A", parent_number=42, labels=["ready"],
        )

        self.assertIs(out, sentinel)
        client.repo.create_issue.assert_called_once()
        kwargs = client.repo.create_issue.call_args.kwargs
        self.assertEqual(kwargs["title"], "A")
        self.assertEqual(kwargs["labels"], ["ready"])
        # Parent link prepended via the helper (not by the caller) so the
        # workflow code can hand the agent's raw body straight in.
        self.assertIn("Parent: #42", kwargs["body"])


class HandleReadyRoutesBackOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_body_drift_routes_ready_back_to_decomposing(self) -> None:
        # `ready` is reached only after a `single` decomposition decision
        # (no children created), so re-decomposing is safe. The handler
        # must clear the locked decomposer session so the next tick spawns
        # a fresh manifest derived against the new body.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="ready", body="updated body")
        gh.add_issue(issue)
        gh.seed_state(
            50,
            user_content_hash="stale-hash-from-prior-tick",
            decomposer_agent="claude",
            decomposer_session_id="old-sess",
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Routed back to decomposing; the implementer must NOT have run
        # this tick.
        self.assertIn((50, "decomposing"), gh.label_history)
        data = gh.pinned_data(50)
        # Session id dropped so the next tick spawns fresh, but the
        # recorded `decomposer_agent` spec is PRESERVED -- the
        # lock-on-first-spawn rule (see FullSpecPersistenceTest) means
        # a mid-flight config flip must not retarget the issue's
        # recorded role identity. The fresh spawn uses the recorded
        # spec via `_read_decomposer_session`.
        self.assertIsNone(data.get("decomposer_session_id"))
        self.assertEqual(data.get("decomposer_agent"), "claude")
        # New hash now persisted so the next decomposing tick sees a
        # stable baseline.
        self.assertNotEqual(
            data.get("user_content_hash"), "stale-hash-from-prior-tick",
        )
        # A human-visible notice is posted.
        self.assertTrue(any(
            "issue content changed" in body
            for _, body in gh.posted_comments
        ))

    def test_unchanged_ready_does_not_route_back(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(51, label="ready", body="stable body")
        gh.add_issue(issue)
        current = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            51,
            user_content_hash=current,
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # Falls through to the normal `ready` -> `implementing` flow.
        self.assertIn((51, "implementing"), gh.label_history)
        self.assertNotIn((51, "decomposing"), gh.label_history)


class HandleDecomposingResetsSessionOnHashChangeTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_hash_drift_drops_session_and_spawns_fresh_decomposer(
        self,
    ) -> None:
        # An issue parked at `decomposing awaiting_human` whose body the
        # human edited mid-thread should NOT resume the decomposer's
        # prior session (which would only see the human's reply, not the
        # new body). Drop the session id, clear the park flags, force a
        # fresh spawn against the new body.
        gh = FakeGitHubClient()
        issue = make_issue(
            90, label="decomposing", body="updated decomposition input",
        )
        # A pre-existing human comment so the resume path would otherwise
        # consume it; we want to verify the hash branch wins.
        issue.comments.append(FakeComment(
            id=2000, body="please reconsider", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            90,
            user_content_hash="stale-hash",
            decomposer_agent="claude",
            decomposer_session_id="old-sess",
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=1500,
            pickup_comment_id=900,
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="new-sess",
                last_message=(
                    "fits one\n\n```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "small"}\n'
                    "```"
                ),
            ),
            has_new_commits=False,
        )

        # The decomposer ran fresh (no resume of the stale session).
        mocks["run_agent"].assert_called_once()
        kwargs = mocks["run_agent"].call_args.kwargs
        self.assertIsNone(kwargs.get("resume_session_id"))
        data = gh.pinned_data(90)
        # The new session id from the fresh spawn was persisted, not the
        # stale one.
        self.assertEqual(data.get("decomposer_session_id"), "new-sess")
        # Notice posted.
        self.assertTrue(any(
            "issue content changed" in body
            for _, body in gh.posted_comments
        ))


class HandleBlockedHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `blocked` must route back to `decomposing` per
    the spec so a later `_handle_ready` does not skip the re-decomposer
    when the edited body now needs splitting. Both parent (children
    listed as orphans) and child (no orphans) cases route."""

    def test_parent_with_children_routes_to_decomposing(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(300, label="blocked", body="updated parent body")
        gh.add_issue(parent)
        # An in-flight child -- routing the parent orphans it on the
        # GitHub side; the notice must call this out so the operator can
        # close any obsolete children manually.
        child = make_issue(301, label="implementing")
        gh.add_issue(child)
        gh.seed_state(
            300,
            children=[301],
            decomposer_session_id="old-sess",
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Routed back to decomposing per spec ("Before validating: route
        # back to decomposing"). The next tick spawns a fresh decomposer
        # against the new body.
        self.assertIn((300, "decomposing"), gh.label_history)
        data = gh.pinned_data(300)
        self.assertFalse(data.get("awaiting_human"))
        # Manifest state cleared so half-finished-recovery does not fire.
        self.assertEqual(data.get("children"), [])
        self.assertIsNone(data.get("decomposer_session_id"))
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Notice explicitly lists the now-orphaned child so the operator
        # knows to close it manually if it no longer applies.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#301", notice)
        self.assertIn("ORPHANED", notice)

    def test_child_waiting_routes_to_decomposing(self) -> None:
        # A blocked child waiting on a sibling. Without routing to
        # `decomposing`, `_handle_ready` would later see the matching
        # baseline (because we silently absorbed the new hash) and skip
        # the re-decomposer, even if the edited child now needs
        # splitting -- the explicit reviewer concern.
        gh = FakeGitHubClient()
        child = make_issue(310, label="blocked", body="updated child body")
        gh.add_issue(child)
        gh.seed_state(
            310,
            parent_number=309,
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        self.assertIn((310, "decomposing"), gh.label_history)
        data = gh.pinned_data(310)
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Notice posted; no orphans for a child with no own children.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertNotIn("ORPHANED", notice)


class HandleUmbrellaHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `umbrella` parents never enter implementation,
    so a body edit cannot be picked up by any later stage's drift check.
    Route back to `decomposing` per spec so the new manifest is derived
    against the updated body; the previously-tracked children become
    orphans and are listed in the notice."""

    def test_edited_umbrella_routes_to_decomposing_before_closing(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        umbrella = make_issue(
            400, label="umbrella", body="updated umbrella body",
        )
        gh.add_issue(umbrella)
        # Children all done -- without the drift route, the umbrella
        # would close to `done` against the stale manifest on this
        # very tick.
        c1 = make_issue(401, label="done")
        c2 = make_issue(402, label="done")
        gh.add_issue(c1)
        gh.add_issue(c2)
        gh.seed_state(
            400,
            children=[401, 402],
            umbrella=True,
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, umbrella),
            run_agent=_agent(),
        )

        data = gh.pinned_data(400)
        # Routed back to decomposing per spec.
        self.assertIn((400, "decomposing"), gh.label_history)
        # Crucially: did NOT close the umbrella to `done`.
        self.assertNotIn((400, "done"), gh.label_history)
        self.assertFalse(umbrella.closed)
        # Manifest state cleared so half-finished-recovery does not fire
        # against the stale children list / umbrella flag.
        self.assertEqual(data.get("children"), [])
        self.assertIsNone(data.get("umbrella"))
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Orphans listed in the notice.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#401", notice)
        self.assertIn("#402", notice)
        self.assertIn("ORPHANED", notice)


class ReadyDriftClearsStaleManifestStateTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: a non-umbrella parent reaches `ready` after all
    its children finish (`_handle_blocked`'s all-done branch flips
    `blocked` -> `ready`), so the parent still carries `children` /
    `dep_graph` from the prior manifest. The drift branch in
    `_handle_ready` must clear that manifest state, otherwise the next
    `_handle_decomposing` tick's half-finished recovery would fire and
    flip back to `blocked` WITHOUT re-running the decomposer."""

    def test_ready_drift_clears_children_and_orphans_them(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(800, label="ready", body="updated parent body")
        gh.add_issue(parent)
        gh.seed_state(
            800,
            user_content_hash="stale-hash",
            # Children list survived from blocked->ready transition; the
            # children are all in `done` (which is how the parent
            # reached `ready` in the first place).
            children=[801, 802],
            dep_graph={"1": [0]},
            expected_children_count=2,
            pickup_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Routed back to decomposing AND manifest state cleared so
        # `_handle_decomposing`'s recovery branch (which keys on
        # `expected_children_count is not None OR children is non-empty`)
        # cannot fire and short-circuit the re-decompose.
        self.assertIn((800, "decomposing"), gh.label_history)
        data = gh.pinned_data(800)
        self.assertEqual(data.get("children"), [])
        self.assertIsNone(data.get("expected_children_count"))
        self.assertEqual(data.get("dep_graph"), {})
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Orphaned children listed in the notice so the operator can
        # close any that no longer apply.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#801", notice)
        self.assertIn("#802", notice)
        self.assertIn("ORPHANED", notice)


class DecomposingDriftBeforeHalfFinishedRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `_handle_decomposing` checks half-finished
    recovery before user-content drift. If the issue was edited while
    `expected_children_count` / `children` are present, the recovery
    branch finalizes to `blocked` / `umbrella` against the stale
    manifest. The drift check must run FIRST so the manifest gets
    re-derived against the new body."""

    def test_drift_with_children_clears_manifest_and_re_runs_decomposer(
        self,
    ) -> None:
        # Simulate the recovery shape: parent label is still
        # `decomposing` and `children` is non-empty (a crash between
        # child creation and the parent label flip), but the human has
        # since edited the body. Without the fix, the recovery branch
        # would finalize to `blocked` against the stale manifest.
        gh = FakeGitHubClient()
        parent = make_issue(
            1100, label="decomposing", body="updated body",
        )
        gh.add_issue(parent)
        # A real child issue so the orphan listing has something to
        # reference.
        child = make_issue(1101, label="blocked")
        gh.add_issue(child)
        gh.seed_state(
            1100,
            user_content_hash="stale-hash",
            children=[1101],
            expected_children_count=1,
            decomposer_session_id="old-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, parent),
            run_agent=_agent(
                session_id="new-sess",
                last_message=(
                    "fits one\n\n```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "small"}\n'
                    "```"
                ),
            ),
            has_new_commits=False,
        )

        # The decomposer ran fresh against the new body (the recovery
        # branch did NOT short-circuit to `blocked`).
        mocks["run_agent"].assert_called_once()
        # Manifest tracking cleared so the recovery branch cannot
        # fire on subsequent ticks against the stale state.
        data = gh.pinned_data(1100)
        self.assertEqual(data.get("children"), [])
        self.assertIsNone(data.get("expected_children_count"))
        self.assertEqual(data.get("dep_graph"), {})
        # New hash baseline persisted.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Parent did NOT finalize to `blocked` against the stale
        # manifest; instead the fresh decomposer voted `single` -> `ready`.
        self.assertNotIn((1100, "blocked"), gh.label_history)
        self.assertIn((1100, "ready"), gh.label_history)
        # Orphans listed in the notice.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#1101", notice)
        self.assertIn("ORPHANED", notice)


class ChildMergedPrAutoFinalizeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A child whose linked PR was merged externally but whose workflow
    label was never advanced past an in-flight stage (e.g. `validating`)
    used to look like a `manually_closed` child to `_handle_blocked` /
    `_handle_umbrella` and park the parent for human adjudication. The
    finalize helper detects the merge during the parent's poll and flips
    the child to `done`, so the parent's aggregation can proceed.
    """

    def _seed_child_with_merged_pr(
        self, gh: FakeGitHubClient, *, number: int, label: str, pr_number: int,
    ) -> FakeIssue:
        child = make_issue(number, label=label)
        child.closed = True
        gh.add_issue(child)
        pr = FakePR(
            number=pr_number,
            head_branch=f"orchestrator/issue-{number}",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(number, pr_number=pr_number)
        return child

    def test_blocked_recovers_child_with_merged_pr(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(70, label="blocked")
        gh.add_issue(parent)
        done_child = make_issue(701, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: a `validating` child whose PR was merged externally
        # (the human clicked Merge before the reviewer agent finished).
        # Used to park the parent on "manually closed"; must now be
        # finalized in-line and counted toward the all-done aggregation.
        self._seed_child_with_merged_pr(
            gh, number=702, label="validating", pr_number=7020,
        )
        gh.seed_state(70, children=[701, 702])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((702, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(702))
        # Parent flipped to ready because every child is now `done`.
        self.assertIn((70, "ready"), gh.label_history)
        # No manual-close park comment posted.
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 70
        ))

    def test_umbrella_recovers_child_with_merged_pr(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(80, label="umbrella")
        gh.add_issue(parent)
        done_child = make_issue(801, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        self._seed_child_with_merged_pr(
            gh, number=802, label="implementing", pr_number=8020,
        )
        gh.seed_state(80, children=[801, 802], umbrella=True)

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((802, "done"), gh.label_history)
        # Umbrella closes once both children are `done`.
        self.assertIn((80, "done"), gh.label_history)
        self.assertTrue(parent.closed)
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 80
        ))

    def test_blocked_still_parks_when_child_pr_not_merged(self) -> None:
        # Regression guard: when the child PR is closed-without-merge,
        # the finalize helper must NOT flip the child to `done`. The
        # original manually-closed park still fires.
        gh = FakeGitHubClient()
        parent = make_issue(71, label="blocked")
        gh.add_issue(parent)
        closed_child = make_issue(711, label="validating")
        closed_child.closed = True
        gh.add_issue(closed_child)
        pr = FakePR(
            number=7110,
            head_branch="orchestrator/issue-711",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(711, pr_number=7110)
        gh.seed_state(71, children=[711])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertNotIn((711, "done"), gh.label_history)
        self.assertTrue(gh.pinned_data(71).get("awaiting_human"))
