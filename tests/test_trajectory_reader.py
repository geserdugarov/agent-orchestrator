# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for `orchestrator.trajectory_reader`.

The reader is the pure, Streamlit-free read model behind the trajectory
viewer page: it reads the opt-in JSONL trajectory sink, parses each
`agent_trajectory` record defensively, and shapes the runs for filtering
/ display. These tests pin the parse resilience (foreign events,
malformed lines, missing fields), the newest-first ordering, the filter
semantics, and the summary aggregation -- all without touching Streamlit.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator.analytics as analytics
import orchestrator.trajectory_reader as tr


def _write_jsonl(path: Path, lines) -> None:
    """Write `lines` (dicts -> JSON, str -> verbatim) to `path`."""
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write(line + "\n")
            else:
                fh.write(json.dumps(line) + "\n")


def _record(**overrides):
    rec = {
        "ts": "2026-06-20T10:00:00+00:00",
        "repo": "acme/widgets",
        "issue": 42,
        "event": "agent_trajectory",
        "stage": "implementing",
        "agent_role": "developer",
        "backend": "claude",
        "steps": [],
    }
    rec.update(overrides)
    return rec


class ParseRecordTest(unittest.TestCase):

    def test_full_record_round_trips(self) -> None:
        rec = _record(
            session_id="sess-1",
            review_round=2,
            retry_count=1,
            user_input="do the thing",
            system_prompt="you are an agent",
            output="done",
            tools=["Bash", "Edit"],
            skills_triggered=["develop"],
            skills_available=["develop", "review"],
            steps=[
                {"kind": "tool_call", "name": "Bash",
                 "tool_id": "t1", "content": "ls -la"},
                {"kind": "tool_result", "name": None,
                 "tool_id": "t1", "content": "listing"},
            ],
            truncated=True,
        )
        run = tr.parse_record(rec, seq=3)
        assert run is not None
        self.assertEqual(run.seq, 3)
        self.assertEqual(run.issue, 42)
        self.assertEqual(run.review_round, 2)
        self.assertEqual(run.retry_count, 1)
        self.assertEqual(run.tools, ("Bash", "Edit"))
        self.assertEqual(run.skills_triggered, ("develop",))
        self.assertTrue(run.truncated)
        self.assertEqual(run.step_count, 2)
        self.assertEqual(run.tool_calls, 1)
        # A result step's missing name normalises to "" so the page
        # never has to guard against None.
        self.assertEqual(run.steps[1].name, "")
        self.assertTrue(run.steps[0].is_call)
        self.assertTrue(run.steps[1].is_result)

    def test_non_dict_returns_none(self) -> None:
        self.assertIsNone(tr.parse_record("nope", seq=0))
        self.assertIsNone(tr.parse_record(["a", "b"], seq=0))

    def test_foreign_event_returns_none(self) -> None:
        self.assertIsNone(
            tr.parse_record(_record(event="agent_exit"), seq=0)
        )
        self.assertIsNone(
            tr.parse_record({"repo": "x", "issue": 1}, seq=0)
        )

    def test_missing_optionals_default_cleanly(self) -> None:
        run = tr.parse_record(_record(), seq=0)
        assert run is not None
        self.assertEqual(run.session_id, "")
        self.assertIsNone(run.review_round)
        self.assertIsNone(run.retry_count)
        self.assertEqual(run.tools, ())
        self.assertEqual(run.steps, ())
        self.assertFalse(run.truncated)

    def test_step_without_kind_is_dropped(self) -> None:
        run = tr.parse_record(
            _record(steps=[
                {"name": "Bash", "content": "x"},     # no kind -> dropped
                {"kind": "tool_call", "name": "Edit"},
                "not-a-dict",                          # dropped
            ]),
            seq=0,
        )
        assert run is not None
        self.assertEqual(run.step_count, 1)
        self.assertEqual(run.steps[0].name, "Edit")

    def test_none_step_content_becomes_empty(self) -> None:
        run = tr.parse_record(
            _record(steps=[
                {"kind": "tool_result", "tool_id": "t1", "content": None},
            ]),
            seq=0,
        )
        assert run is not None
        self.assertEqual(run.steps[0].content, "")

    def test_issue_coerced_and_bad_issue_defaults_zero(self) -> None:
        self.assertEqual(tr.parse_record(_record(issue="7"), seq=0).issue, 7)
        self.assertEqual(
            tr.parse_record(_record(issue="bad"), seq=0).issue, 0
        )

    def test_review_round_string_coerced(self) -> None:
        run = tr.parse_record(_record(review_round="3"), seq=0)
        self.assertEqual(run.review_round, 3)


class ReadTrajectoriesTest(unittest.TestCase):

    def _read_from(self, lines):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "traj.jsonl"
            _write_jsonl(path, lines)
            return tr.read_trajectories(path=path)

    def test_skips_blank_malformed_and_foreign_lines(self) -> None:
        runs = self._read_from([
            _record(issue=1),
            "",                              # blank
            "{not valid json",              # malformed
            _record(issue=2, event="agent_exit"),  # foreign
            _record(issue=3),
        ])
        self.assertEqual({r.issue for r in runs}, {1, 3})

    def test_newest_first_by_timestamp(self) -> None:
        runs = self._read_from([
            _record(issue=1, ts="2026-06-20T10:00:00+00:00"),
            _record(issue=2, ts="2026-06-22T10:00:00+00:00"),
            _record(issue=3, ts="2026-06-21T10:00:00+00:00"),
        ])
        self.assertEqual([r.issue for r in runs], [2, 3, 1])

    def test_equal_timestamp_breaks_on_file_order_newest_last(self) -> None:
        # Same second-precision ts: the record appended later (higher
        # seq) sorts first so "most recent" stays intuitive.
        runs = self._read_from([
            _record(issue=1, ts="2026-06-20T10:00:00+00:00"),
            _record(issue=2, ts="2026-06-20T10:00:00+00:00"),
        ])
        self.assertEqual([r.issue for r in runs], [2, 1])

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                tr.read_trajectories(path=Path(d) / "absent.jsonl"), []
            )

    def test_disabled_sink_returns_empty(self) -> None:
        with patch.object(analytics, "TRAJECTORY_LOG_PATH", None):
            self.assertEqual(tr.read_trajectories(), [])

    def test_default_path_resolves_from_analytics_attr(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "traj.jsonl"
            _write_jsonl(path, [_record(issue=9)])
            with patch.object(analytics, "TRAJECTORY_LOG_PATH", path):
                runs = tr.read_trajectories()
        self.assertEqual([r.issue for r in runs], [9])


class ResolveLogPathTest(unittest.TestCase):

    def test_unconfigured_message_when_off(self) -> None:
        with patch.object(analytics, "TRAJECTORY_LOG_PATH", None):
            self.assertIsNone(tr.resolve_log_path())
            self.assertIsNotNone(tr.log_unconfigured_message())

    def test_no_message_when_configured(self) -> None:
        with patch.object(
            analytics, "TRAJECTORY_LOG_PATH", Path("/var/log/traj.jsonl")
        ):
            self.assertEqual(
                tr.resolve_log_path(), Path("/var/log/traj.jsonl")
            )
            self.assertIsNone(tr.log_unconfigured_message())


class FilterOptionsTest(unittest.TestCase):

    def test_distinct_sorted_non_empty(self) -> None:
        runs = [
            tr.parse_record(
                _record(repo="b/b", backend="codex", agent_role="reviewer",
                        stage="in_review"),
                seq=0,
            ),
            tr.parse_record(
                _record(repo="a/a", backend="claude", agent_role="developer",
                        stage="implementing"),
                seq=1,
            ),
            tr.parse_record(
                _record(repo="a/a", backend="claude", agent_role="",
                        stage=""),
                seq=2,
            ),
        ]
        opts = tr.filter_options(runs)
        self.assertEqual(opts.repos, ("a/a", "b/b"))
        self.assertEqual(opts.backends, ("claude", "codex"))
        # Empty role / stage are dropped, not offered as a blank choice.
        self.assertEqual(opts.agent_roles, ("developer", "reviewer"))
        self.assertEqual(opts.stages, ("implementing", "in_review"))


class FilterRunsTest(unittest.TestCase):

    def _runs(self):
        return [
            tr.parse_record(
                _record(issue=1, repo="a/a", backend="claude",
                        agent_role="developer", stage="implementing",
                        output="resolved the bug",
                        steps=[{"kind": "tool_call", "name": "Bash",
                                "content": "grep needle file.py"}],
                        skills_triggered=["develop"]),
                seq=0,
            ),
            tr.parse_record(
                _record(issue=2, repo="b/b", backend="codex",
                        agent_role="reviewer", stage="in_review",
                        output="looks good"),
                seq=1,
            ),
        ]

    def test_no_filters_returns_all(self) -> None:
        runs = self._runs()
        self.assertEqual(len(tr.filter_runs(runs)), 2)

    def test_repo_and_issue_exact_match(self) -> None:
        runs = self._runs()
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, repo="a/a")], [1]
        )
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, issue=2)], [2]
        )

    def test_multi_value_filters(self) -> None:
        runs = self._runs()
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, backends=["codex"])], [2]
        )
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, agent_roles=["developer"])],
            [1],
        )
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, stages=["in_review"])], [2]
        )

    def test_empty_multi_value_is_no_constraint(self) -> None:
        runs = self._runs()
        self.assertEqual(len(tr.filter_runs(runs, backends=[])), 2)
        self.assertEqual(len(tr.filter_runs(runs, stages=None)), 2)

    def test_query_spans_output_step_content_and_skill(self) -> None:
        runs = self._runs()
        # Output text.
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, query="resolved")], [1]
        )
        # Step content (a path inside a tool command).
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, query="file.py")], [1]
        )
        # Skill name, case-insensitive.
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, query="DEVELOP")], [1]
        )
        # Whitespace-only query is treated as no filter.
        self.assertEqual(len(tr.filter_runs(runs, query="   ")), 2)

    def test_query_matches_message_turn_content(self) -> None:
        # The newer `assistant_message` / `user_message` turns are steps
        # too, so the free-text search reaches their content like any
        # tool payload.
        runs = [
            tr.parse_record(
                _record(issue=1, steps=[
                    {"kind": "assistant_message",
                     "content": "I will refactor the cache layer"}]),
                seq=0,
            ),
            tr.parse_record(_record(issue=2), seq=1),
        ]
        self.assertEqual(
            [r.issue for r in tr.filter_runs(runs, query="refactor")], [1]
        )

    def test_filters_combine_conjunctively(self) -> None:
        runs = self._runs()
        self.assertEqual(
            tr.filter_runs(runs, repo="a/a", backends=["codex"]), []
        )

    def test_exclude_fixtures_default_off(self) -> None:
        # Backward-compatible default: fixtures are kept unless asked to
        # drop them.
        runs = [
            tr.parse_record(_record(issue=1, user_input="real work",
                                    session_id="uuid-1"), seq=0),
            tr.parse_record(_record(issue=2, user_input="ignored"), seq=1),
        ]
        self.assertEqual(len(tr.filter_runs(runs)), 2)

    def test_exclude_fixtures_drops_every_tell(self) -> None:
        runs = [
            tr.parse_record(_record(issue=1, user_input="real work",
                                    session_id="uuid-1"), seq=0),
            tr.parse_record(_record(issue=2, user_input="ignored"), seq=1),
            tr.parse_record(_record(issue=3, session_id="sess-7"), seq=2),
            tr.parse_record(_record(issue=4, steps=[
                {"kind": "tool_call", "name": "Skill",
                 "content": "develop"}]), seq=3),
        ]
        kept = tr.filter_runs(runs, exclude_fixtures=True)
        self.assertEqual([r.issue for r in kept], [1])

    def test_exclude_fixtures_combines_with_other_filters(self) -> None:
        # An issue filter that selects a fixture still drops it.
        runs = [
            tr.parse_record(_record(issue=2, user_input="ignored"), seq=0),
        ]
        self.assertEqual(
            tr.filter_runs(runs, issue=2, exclude_fixtures=True), []
        )


class SummarizeTest(unittest.TestCase):

    def test_counts(self) -> None:
        runs = [
            tr.parse_record(
                _record(issue=1, repo="a/a",
                        steps=[{"kind": "tool_call", "name": "Bash"},
                               {"kind": "tool_result", "tool_id": "t"}],
                        truncated=True),
                seq=0,
            ),
            tr.parse_record(
                _record(issue=1, repo="a/a",
                        steps=[{"kind": "tool_call", "name": "Edit"}]),
                seq=1,
            ),
            tr.parse_record(_record(issue=2, repo="b/b"), seq=2),
        ]
        s = tr.summarize(runs)
        self.assertEqual(s.total_runs, 3)
        # Two runs share (a/a, 1); (b/b, 2) is the third distinct issue.
        self.assertEqual(s.distinct_issues, 2)
        self.assertEqual(s.distinct_repos, 2)
        self.assertEqual(s.total_tool_calls, 2)
        self.assertEqual(s.truncated_runs, 1)

    def test_empty(self) -> None:
        s = tr.summarize([])
        self.assertEqual(
            (s.total_runs, s.distinct_issues, s.distinct_repos,
             s.total_tool_calls, s.truncated_runs),
            (0, 0, 0, 0, 0),
        )


class LabelTest(unittest.TestCase):

    def test_label_carries_issue_repo_and_round(self) -> None:
        run = tr.parse_record(
            _record(issue=42, repo="a/a", stage="implementing",
                    agent_role="developer", backend="claude",
                    review_round=1),
            seq=0,
        )
        label = run.label()
        self.assertIn("#42", label)
        self.assertIn("a/a", label)
        self.assertIn("implementing/developer", label)
        self.assertIn("round 1", label)

    def test_label_without_round_omits_it(self) -> None:
        run = tr.parse_record(_record(), seq=0)
        self.assertNotIn("round", run.label())

    def test_detail_label_drops_issue_and_repo(self) -> None:
        run = tr.parse_record(
            _record(issue=42, repo="a/a", stage="documenting",
                    agent_role="developer", backend="claude",
                    review_round=0),
            seq=0,
        )
        detail = run.detail_label()
        self.assertIn("documenting/developer · claude · round 0", detail)
        self.assertIn(run.ts, detail)
        # The repo / issue are picked separately, so they are dropped here.
        self.assertNotIn("#42", detail)
        self.assertNotIn("a/a", detail)

    def test_label_is_issue_repo_plus_detail_label(self) -> None:
        run = tr.parse_record(
            _record(issue=7, repo="a/a", stage="implementing",
                    agent_role="developer", backend="claude",
                    review_round=1),
            seq=0,
        )
        self.assertEqual(
            run.label(), f"#7 a/a · {run.detail_label()}"
        )


class TimelineTest(unittest.TestCase):
    """`TrajectoryRun.timeline` normalizes old and new records alike."""

    def test_old_steps_only_record_brackets_prompt_and_output(self) -> None:
        # A legacy record predates the text-turn timeline: its steps are
        # only tool_call / tool_result. The normalized timeline still
        # brackets them with the prompt and the final output, in order.
        run = tr.parse_record(
            _record(
                user_input="do the thing",
                output="all done",
                steps=[
                    {"kind": "tool_call", "name": "Bash",
                     "tool_id": "t1", "content": "ls"},
                    {"kind": "tool_result", "tool_id": "t1",
                     "content": "calc.py"},
                ],
            ),
            seq=0,
        )
        self.assertEqual(
            [e.kind for e in run.timeline],
            ["prompt", "tool_call", "tool_result", "output"],
        )
        self.assertTrue(run.timeline[0].is_prompt)
        self.assertEqual(run.timeline[0].content, "do the thing")
        self.assertTrue(run.timeline[-1].is_output)
        self.assertEqual(run.timeline[-1].content, "all done")
        # The middle tool_call keeps its name / id; the brackets carry none.
        call = run.timeline[1]
        self.assertEqual(call.name, "Bash")
        self.assertEqual(call.tool_id, "t1")
        self.assertEqual(run.timeline[0].name, "")
        self.assertEqual(run.timeline[0].tool_id, "")

    def test_new_mixed_timeline_preserves_interleaved_turns(self) -> None:
        # A record written since the timeline feature interleaves
        # assistant / user text turns with the tool steps; the normalized
        # timeline keeps stream order and adds the prompt / output brackets.
        run = tr.parse_record(
            _record(
                user_input="fix the parser",
                output="fixed",
                steps=[
                    {"kind": "assistant_message", "content": "let me look"},
                    {"kind": "tool_call", "name": "Read", "tool_id": "r1",
                     "content": "open x.py"},
                    {"kind": "tool_result", "tool_id": "r1",
                     "content": "body"},
                    {"kind": "user_message", "content": "now ship it"},
                    {"kind": "assistant_message", "content": "done"},
                ],
            ),
            seq=0,
        )
        self.assertEqual(
            [e.kind for e in run.timeline],
            ["prompt", "assistant_message", "tool_call", "tool_result",
             "user_message", "assistant_message", "output"],
        )

    def test_tool_calls_count_excludes_message_turns(self) -> None:
        # The message turns are steps but must not be counted as tool
        # calls -- the tally stays correct across record vintages.
        run = tr.parse_record(
            _record(steps=[
                {"kind": "assistant_message", "content": "thinking"},
                {"kind": "tool_call", "name": "Bash", "content": "ls"},
                {"kind": "tool_result", "tool_id": "t", "content": "out"},
                {"kind": "user_message", "content": "go on"},
                {"kind": "tool_call", "name": "Edit", "content": "patch"},
            ]),
            seq=0,
        )
        self.assertEqual(run.step_count, 5)
        self.assertEqual(run.tool_calls, 2)

    def test_brackets_omitted_when_field_empty(self) -> None:
        # No prompt and no output: the timeline is exactly the steps.
        run = tr.parse_record(
            _record(user_input="", output="",
                    steps=[{"kind": "tool_call", "name": "Bash"}]),
            seq=0,
        )
        self.assertEqual([e.kind for e in run.timeline], ["tool_call"])

    def test_prompt_only_record_is_single_bracket(self) -> None:
        run = tr.parse_record(
            _record(user_input="just a prompt", output=""), seq=0
        )
        timeline = run.timeline
        self.assertEqual([e.kind for e in timeline], ["prompt"])
        self.assertEqual(timeline[0].content, "just a prompt")

    def test_empty_record_has_empty_timeline(self) -> None:
        run = tr.parse_record(_record(user_input="", output=""), seq=0)
        self.assertEqual(run.timeline, ())


class FixtureIdentificationTest(unittest.TestCase):
    """`TrajectoryRun.is_fixture` flags synthetic test-suite records."""

    def test_ignored_prompt_is_fixture(self) -> None:
        self.assertTrue(
            tr.parse_record(_record(user_input="ignored"), seq=0).is_fixture
        )
        # Case and surrounding whitespace do not hide the sentinel.
        self.assertTrue(
            tr.parse_record(
                _record(user_input="  IGNORED "), seq=0
            ).is_fixture
        )

    def test_sess_session_id_is_fixture(self) -> None:
        self.assertTrue(
            tr.parse_record(_record(session_id="sess-dev"), seq=0).is_fixture
        )
        self.assertTrue(
            tr.parse_record(_record(session_id="sess-1"), seq=0).is_fixture
        )

    def test_skill_only_run_is_fixture(self) -> None:
        run = tr.parse_record(
            _record(
                user_input="real prompt",
                session_id="uuid-9",
                steps=[
                    {"kind": "tool_call", "name": "Skill",
                     "content": "develop"},
                    {"kind": "tool_call", "name": "Skill",
                     "content": "review"},
                ],
            ),
            seq=0,
        )
        self.assertTrue(run.is_fixture)

    def test_real_run_is_not_fixture(self) -> None:
        # A real prompt, a uuid session id, and mixed real tool work
        # (a Skill call among Bash / its result): no tell fires.
        run = tr.parse_record(
            _record(
                user_input="please fix issue 7",
                session_id="0f9a3c2e-1b4d-4a77-9c12-abcdef012345",
                steps=[
                    {"kind": "tool_call", "name": "Skill",
                     "content": "develop"},
                    {"kind": "tool_call", "name": "Bash",
                     "content": "pytest"},
                    {"kind": "tool_result", "tool_id": "t", "content": "ok"},
                ],
            ),
            seq=0,
        )
        self.assertFalse(run.is_fixture)

    def test_no_steps_run_is_not_skill_only(self) -> None:
        # An empty step list must not be read as a Skill-only run; only
        # the prompt / session tells can flag a stepless record.
        run = tr.parse_record(
            _record(user_input="real", session_id="abc123"), seq=0
        )
        self.assertFalse(run.is_fixture)


if __name__ == "__main__":
    unittest.main()
