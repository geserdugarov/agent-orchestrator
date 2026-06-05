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
        # the local `orchestrator/geserdugarov__agent-orchestrator/issue-<n>` branch anchored at the
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
