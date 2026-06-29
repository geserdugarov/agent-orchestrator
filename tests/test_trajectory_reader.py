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

    def test_filters_combine_conjunctively(self) -> None:
        runs = self._runs()
        self.assertEqual(
            tr.filter_runs(runs, repo="a/a", backends=["codex"]), []
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


if __name__ == "__main__":
    unittest.main()
