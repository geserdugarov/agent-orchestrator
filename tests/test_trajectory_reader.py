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
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import analytics
from orchestrator import trajectory_reader as tr
# Bind after `tr` so `records` is the leaf the facade re-exports: importing
# `trajectory_reader` evicts and rebuilds `_trajectory_records`, so a binding
# taken before it would point at the discarded pre-eviction leaf.
from orchestrator import _trajectory_records as records

# Trajectory record / step / turn JSON field keys.
_KIND = "kind"
_NAME = "name"
_CONTENT_KEY = "content"
_TOOL_ID = "tool_id"
_TURN = "turn"
_INPUT_TOKENS = "input_tokens"
_OUTPUT_TOKENS = "output_tokens"
_COST_USD = "cost_usd"
_COST_SOURCE = "cost_source"

# Step kinds and the prompt / output timeline brackets.
_TOOL_CALL = "tool_call"
_TOOL_RESULT = "tool_result"
_ASSISTANT_MESSAGE = "assistant_message"
_TL_PROMPT = "prompt"
_TL_OUTPUT = "output"

# Backends, the claude model name, and cost-source labels.
_BACKEND_CLAUDE = "claude"
_BACKEND_CODEX = "codex"
_MODEL_CLAUDE = "claude-opus-4-8"
_REPORTED = "reported"
_UNKNOWN_PRICE = "unknown-price"

# Repos, stages, roles, tool / skill names, and a reused tool id.
_REPO_A = "a/a"
_REPO_B = "b/b"
_STAGE_IMPLEMENTING = "implementing"
_STAGE_IN_REVIEW = "in_review"
_ROLE_DEVELOPER = "developer"
_TOOL_BASH = "Bash"
_TOOL_EDIT = "Edit"
_TOOL_SKILL = "Skill"
_SKILL_DEVELOP = "develop"
_T1 = "t1"

# Fixture prompt / output / step-content samples.
_PROMPT_DO_THING = "do the thing"
_IGNORED = "ignored"
_DONE = "done"
_LS = "ls"

# Sink timestamp and the module / package names the reload guards resolve.
_TS = "2026-06-20T10:00:00+00:00"
_LOG_PATH_ATTR = "TRAJECTORY_LOG_PATH"
_READER_MODULE = "orchestrator.trajectory_reader"
_ANALYTICS_MODULE = "orchestrator.analytics"
_CONFIG_MODULE = "orchestrator.config"
_ORCHESTRATOR_PKG = "orchestrator"

_ISSUE = 42
_USAGE_INPUT = 12
_USAGE_OUTPUT = 340
_USAGE_CACHE_READ = 18240
_USAGE_CACHE_WRITE = 512
_RUN_COST = 0.83
_TURN0_COST = 0.0123
_CODEX_INPUT = 100
_CODEX_OUTPUT = 50


def _write_jsonl(path: Path, lines) -> None:
    """Write `lines` (dicts -> JSON, str -> verbatim) to `path`."""
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write("{0}\n".format(line))
            else:
                fh.write("{0}\n".format(json.dumps(line)))


def _record(**overrides):
    record = {
        "ts": _TS,
        "repo": "acme/widgets",
        "issue": _ISSUE,
        "event": "agent_trajectory",
        "stage": _STAGE_IMPLEMENTING,
        "agent_role": _ROLE_DEVELOPER,
        "backend": _BACKEND_CLAUDE,
        "steps": [],
    }
    record.update(overrides)
    return record


class ParseRecordTest(unittest.TestCase):

    def test_full_record_round_trips(self) -> None:
        record = _record(
            session_id="sess-1",
            review_round=2,
            retry_count=1,
            user_input=_PROMPT_DO_THING,
            system_prompt="you are an agent",
            output=_DONE,
            tools=[_TOOL_BASH, _TOOL_EDIT],
            skills_triggered=[_SKILL_DEVELOP],
            skills_available=[_SKILL_DEVELOP, "review"],
            steps=[
                {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                 _TOOL_ID: _T1, _CONTENT_KEY: "ls -la"},
                {_KIND: _TOOL_RESULT, _NAME: None,
                 _TOOL_ID: _T1, _CONTENT_KEY: "listing"},
            ],
            truncated=True,
        )
        run = tr.parse_record(record, seq=3)
        assert run is not None
        for field, expected in (
            ("seq", 3),
            ("issue", _ISSUE),
            ("review_round", 2),
            ("retry_count", 1),
            ("tools", (_TOOL_BASH, _TOOL_EDIT)),
            ("skills_triggered", (_SKILL_DEVELOP,)),
        ):
            self.assertEqual(getattr(run, field), expected, field)
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

    def test_accepts_obj_keyword(self) -> None:
        # `obj` is the public keyword; callers may pass the record by name.
        run = tr.parse_record(obj=_record(issue=7), seq=0)
        assert run is not None
        self.assertEqual(run.issue, 7)

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
        self.assertIsNone(run.run_usage)
        self.assertEqual(run.turns, ())
        self.assertFalse(run.truncated)

    def test_step_without_kind_is_dropped(self) -> None:
        run = tr.parse_record(
            _record(steps=[
                {_NAME: _TOOL_BASH, _CONTENT_KEY: "x"},     # no kind -> dropped
                {_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT},
                "not-a-dict",                          # dropped
            ]),
            seq=0,
        )
        assert run is not None
        self.assertEqual(run.step_count, 1)
        self.assertEqual(run.steps[0].name, _TOOL_EDIT)

    def test_none_step_content_becomes_empty(self) -> None:
        run = tr.parse_record(
            _record(steps=[
                {_KIND: _TOOL_RESULT, _TOOL_ID: _T1, _CONTENT_KEY: None},
            ]),
            seq=0,
        )
        assert run is not None
        self.assertEqual(run.steps[0].content, "")

    def test_issue_coerced_bad_value_defaults_zero(self) -> None:
        coerced = tr.parse_record(_record(issue="7"), seq=0)
        self.assertEqual(coerced.issue, 7)
        uncoercible = tr.parse_record(_record(issue="bad"), seq=0)
        self.assertEqual(uncoercible.issue, 0)

    def test_review_round_string_coerced(self) -> None:
        run = tr.parse_record(_record(review_round="3"), seq=0)
        self.assertEqual(run.review_round, 3)


def _usage_record(**overrides):
    """A claude record carrying run + per-turn usage and turn-stamped steps."""
    record = _record(
        user_input="fix the parser",
        output=_DONE,
        run_usage={
            "models": [_MODEL_CLAUDE],
            "turns": 2,
            _INPUT_TOKENS: _USAGE_INPUT,
            _OUTPUT_TOKENS: _USAGE_OUTPUT,
            "cached_tokens": 0,
            "cache_read_tokens": _USAGE_CACHE_READ,
            "cache_write_tokens": _USAGE_CACHE_WRITE,
            _COST_USD: _RUN_COST,
            _COST_SOURCE: _REPORTED,
        },
        turns=[
            {_TURN: 0, "model": _MODEL_CLAUDE, _INPUT_TOKENS: _USAGE_INPUT,
             _OUTPUT_TOKENS: _USAGE_OUTPUT, "cache_read_tokens": _USAGE_CACHE_READ,
             "cache_write_tokens": _USAGE_CACHE_WRITE, _COST_USD: _TURN0_COST,
             _COST_SOURCE: "estimated"},
            {_TURN: 1, "model": _MODEL_CLAUDE, _INPUT_TOKENS: 5,
             _OUTPUT_TOKENS: 120, "cache_read_tokens": 900,
             "cache_write_tokens": 0, _COST_USD: None,
             _COST_SOURCE: _UNKNOWN_PRICE},
        ],
        steps=[
            {_KIND: _ASSISTANT_MESSAGE, _TURN: 0, _CONTENT_KEY: "let me look"},
            {_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT, _TOOL_ID: "e1",
             _TURN: 0, _CONTENT_KEY: "patch"},
            {_KIND: _TOOL_RESULT, _TOOL_ID: "e1", _CONTENT_KEY: "ok"},
            {_KIND: _ASSISTANT_MESSAGE, _TURN: 1, _CONTENT_KEY: _DONE},
        ],
    )
    record.update(overrides)
    return record


class UsageParsingTest(unittest.TestCase):
    """The reader exposes run- and per-turn usage, tolerantly parsed."""

    def test_full_usage_parses_and_exposes_helpers(self) -> None:
        run = tr.parse_record(_usage_record(), seq=0)
        assert run is not None and run.run_usage is not None
        # Run summary round-trips.
        self.assertEqual(run.run_usage.models, (_MODEL_CLAUDE,))
        self.assertEqual(run.run_usage.input_tokens, _USAGE_INPUT)
        self.assertEqual(run.run_usage.turns, 2)
        self.assertEqual(run.run_usage.cost_source, _REPORTED)
        # Per-turn breakdown round-trips, including the unpriced turn.
        self.assertEqual(len(run.turns), 2)
        self.assertEqual(run.turns[0].turn, 0)
        self.assertEqual(run.turns[0].cost_usd, _TURN0_COST)
        self.assertIsNone(run.turns[1].cost_usd)
        self.assertEqual(run.turns[1].cost_source, _UNKNOWN_PRICE)
        # Convenience helpers read the authoritative run figures.
        self.assertEqual(run.model, _MODEL_CLAUDE)
        self.assertEqual(run.cost_usd, _RUN_COST)
        self.assertEqual(run.cost_source, _REPORTED)
        # total = input + output + cache_read + cache_write.
        self.assertEqual(
            run.total_tokens,
            _USAGE_INPUT + _USAGE_OUTPUT + _USAGE_CACHE_READ + _USAGE_CACHE_WRITE,
        )

    def test_usage_for_turn_lookup(self) -> None:
        run = tr.parse_record(_usage_record(), seq=0)
        assert run is not None
        self.assertEqual(run.usage_for_turn(0).cost_usd, _TURN0_COST)
        self.assertEqual(run.usage_for_turn(1).cost_source, _UNKNOWN_PRICE)
        # A turn input / bracket carries turn=None -> no usage.
        self.assertIsNone(run.usage_for_turn(None))
        # An index with no recorded turn (codex, a budget-dropped turn).
        self.assertIsNone(run.usage_for_turn(9))

    def test_step_and_timeline_turn_propagate(self) -> None:
        run = tr.parse_record(_usage_record(), seq=0)
        assert run is not None
        # Billed steps carry their turn; the tool_result input stays None.
        self.assertEqual(
            [step.turn for step in run.steps], [0, 0, None, 1]
        )
        # The timeline mirrors the step turn so the page can render the
        # per-turn strip at the boundary; the brackets carry no turn.
        self.assertEqual(
            [(entry.kind, entry.turn) for entry in run.timeline],
            [(_TL_PROMPT, None), (_ASSISTANT_MESSAGE, 0), (_TOOL_CALL, 0),
             (_TOOL_RESULT, None), (_ASSISTANT_MESSAGE, 1),
             (_TL_OUTPUT, None)],
        )

    def test_pre_usage_record_is_compatible(self) -> None:
        # A record written before the usage feature: no run_usage, no
        # turns, no step.turn. It parses with empty defaults and renders
        # exactly as before -- timeline and helpers all degrade cleanly.
        run = tr.parse_record(
            _record(
                user_input=_PROMPT_DO_THING,
                output=_DONE,
                steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                        _TOOL_ID: _T1, _CONTENT_KEY: _LS}],
            ),
            seq=0,
        )
        assert run is not None
        self.assertIsNone(run.run_usage)
        self.assertEqual(run.turns, ())
        self.assertEqual(run.model, "")
        self.assertIsNone(run.cost_usd)
        self.assertEqual(run.cost_source, "")
        self.assertEqual(run.total_tokens, 0)
        self.assertIsNone(run.usage_for_turn(0))
        self.assertEqual(
            [(entry.kind, entry.turn) for entry in run.timeline],
            [(_TL_PROMPT, None), (_TOOL_CALL, None), (_TL_OUTPUT, None)],
        )

    def test_malformed_usage_is_tolerated(self) -> None:
        # run_usage not a dict -> None; a non-dict turns entry dropped; a
        # non-numeric cost / turn index coerced away, never raising.
        run = tr.parse_record(
            _record(
                run_usage="oops",
                turns=[
                    "not-a-dict",
                    {_TURN: "bad", "model": _MODEL_CLAUDE,
                     _COST_USD: "free"},
                ],
                steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT,
                        _TURN: "nope", _CONTENT_KEY: "x"}],
            ),
            seq=0,
        )
        assert run is not None
        self.assertIsNone(run.run_usage)
        # The non-dict entry is dropped; the malformed one survives with its
        # bad fields coerced away, and is unreachable by index.
        self.assertEqual(len(run.turns), 1)
        self.assertIsNone(run.turns[0].turn)
        self.assertIsNone(run.turns[0].cost_usd)
        self.assertIsNone(run.usage_for_turn(0))
        self.assertIsNone(run.steps[0].turn)
        # Helpers still answer without a run_usage.
        self.assertEqual(run.total_tokens, 0)
        self.assertIsNone(run.cost_usd)

    def test_non_list_array_fields_are_tolerated(self) -> None:
        # A hand-edited record with a scalar where an array is expected
        # (`"turns": 1`, `"steps": 1`) must yield an empty section, not a
        # `TypeError` when the reader iterates it.
        run = tr.parse_record(_record(turns=1, steps=1), seq=0)
        assert run is not None
        self.assertEqual(run.turns, ())
        self.assertEqual(run.steps, ())
        self.assertIsNone(run.usage_for_turn(0))

    def test_codex_run_usage_without_per_turn_detail(self) -> None:
        # Codex records the run summary but no per-turn breakdown: run_usage
        # present, turns empty, every step.turn None. Its run_usage also
        # omits the cache buckets, exercising the numeric-field 0 default.
        run = tr.parse_record(
            _record(
                backend=_BACKEND_CODEX,
                run_usage={"models": ["gpt-5"], _INPUT_TOKENS: _CODEX_INPUT,
                           _OUTPUT_TOKENS: _CODEX_OUTPUT, _COST_USD: 0.02,
                           _COST_SOURCE: "estimated"},
                steps=[{_KIND: _TOOL_CALL, _NAME: "shell",
                        _CONTENT_KEY: _LS}],
            ),
            seq=0,
        )
        assert run is not None and run.run_usage is not None
        self.assertEqual(run.turns, ())
        self.assertEqual(run.model, "gpt-5")
        self.assertEqual(run.run_usage.cache_read_tokens, 0)
        # cached_tokens is a subset of input on codex, so the total is
        # input + output with the (0) cache buckets.
        self.assertEqual(run.total_tokens, _CODEX_INPUT + _CODEX_OUTPUT)
        self.assertIsNone(run.usage_for_turn(0))
        self.assertIsNone(run.steps[0].turn)


class ReadTrajectoriesTest(unittest.TestCase):

    def test_skips_blank_malformed_and_foreign_lines(self) -> None:
        runs = self._read_from([
            _record(issue=1),
            "",                              # blank
            "{not valid json",              # malformed
            _record(issue=2, event="agent_exit"),  # foreign
            _record(issue=3),
        ])
        self.assertEqual({run.issue for run in runs}, {1, 3})

    def test_newest_first_by_timestamp(self) -> None:
        runs = self._read_from([
            _record(issue=1, ts=_TS),
            _record(issue=2, ts="2026-06-22T10:00:00+00:00"),
            _record(issue=3, ts="2026-06-21T10:00:00+00:00"),
        ])
        self.assertEqual([run.issue for run in runs], [2, 3, 1])

    def test_equal_time_uses_file_order_newest_last(self) -> None:
        # Same second-precision ts: the record appended later (higher
        # seq) sorts first so "most recent" stays intuitive.
        runs = self._read_from([
            _record(issue=1, ts=_TS),
            _record(issue=2, ts=_TS),
        ])
        self.assertEqual([run.issue for run in runs], [2, 1])

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertEqual(
                tr.read_trajectories(path=Path(tmp_dir) / "absent.jsonl"), []
            )

    def test_disabled_sink_returns_empty(self) -> None:
        with patch.object(analytics, _LOG_PATH_ATTR, None):
            self.assertEqual(tr.read_trajectories(), [])

    def test_default_path_uses_analytics_attr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "traj.jsonl"
            _write_jsonl(path, [_record(issue=9)])
            with patch.object(analytics, _LOG_PATH_ATTR, path):
                runs = tr.read_trajectories()
        self.assertEqual([run.issue for run in runs], [9])

    def test_unreadable_file_warns_and_returns_empty(self) -> None:
        # Pointing the reader at a directory raises IsADirectoryError -- an
        # OSError that is not FileNotFoundError -- so the read takes the
        # warn-and-empty branch instead of the silent missing-file one. The
        # warning is emitted on the public `orchestrator.trajectory_reader`
        # logger even though the read pipeline lives in the private
        # `_trajectory_records` leaf, so an operator's log filter keyed on
        # that name still sees it.
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertLogs(_READER_MODULE, level="WARNING") as captured:
                runs = tr.read_trajectories(path=Path(tmp_dir))
                self.assertEqual(runs, [])
                self.assertEqual(len(captured.records), 1)
                self.assertEqual(captured.records[0].name, _READER_MODULE)
                self.assertIn("could not read trajectory log", captured.output[0])

    def _read_from(self, lines):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "traj.jsonl"
            _write_jsonl(path, lines)
            return tr.read_trajectories(path=path)


class ResolveLogPathTest(unittest.TestCase):

    def test_unconfigured_message_when_off(self) -> None:
        with patch.object(analytics, _LOG_PATH_ATTR, None):
            self.assertIsNone(tr.resolve_log_path())
            self.assertIsNotNone(tr.log_unconfigured_message())

    def test_no_message_when_configured(self) -> None:
        with patch.object(
            analytics, _LOG_PATH_ATTR, Path("/var/log/traj.jsonl")
        ):
            self.assertEqual(
                tr.resolve_log_path(), Path("/var/log/traj.jsonl")
            )
            self.assertIsNone(tr.log_unconfigured_message())


class FilterOptionsTest(unittest.TestCase):

    def test_distinct_sorted_non_empty(self) -> None:
        runs = [
            tr.parse_record(
                _record(repo=_REPO_B, backend=_BACKEND_CODEX, agent_role="reviewer",
                        stage=_STAGE_IN_REVIEW),
                seq=0,
            ),
            tr.parse_record(
                _record(repo=_REPO_A, backend=_BACKEND_CLAUDE, agent_role=_ROLE_DEVELOPER,
                        stage=_STAGE_IMPLEMENTING),
                seq=1,
            ),
            tr.parse_record(
                _record(repo=_REPO_A, backend=_BACKEND_CLAUDE, agent_role="",
                        stage=""),
                seq=2,
            ),
        ]
        opts = tr.filter_options(runs)
        self.assertEqual(opts.repos, (_REPO_A, _REPO_B))
        self.assertEqual(opts.backends, (_BACKEND_CLAUDE, _BACKEND_CODEX))
        # Empty role / stage are dropped, not offered as a blank choice.
        self.assertEqual(opts.agent_roles, (_ROLE_DEVELOPER, "reviewer"))
        self.assertEqual(opts.stages, (_STAGE_IMPLEMENTING, _STAGE_IN_REVIEW))


class FilterRunsTest(unittest.TestCase):

    def test_no_filters_returns_all(self) -> None:
        runs = self._runs()
        self.assertEqual(len(tr.filter_runs(runs)), 2)

    def test_repo_and_issue_exact_match(self) -> None:
        runs = self._runs()
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, repo=_REPO_A)], [1]
        )
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, issue=2)], [2]
        )

    def test_multi_value_filters(self) -> None:
        runs = self._runs()
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, backends=[_BACKEND_CODEX])], [2]
        )
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, agent_roles=[_ROLE_DEVELOPER])],
            [1],
        )
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, stages=[_STAGE_IN_REVIEW])], [2]
        )

    def test_empty_multi_value_is_no_constraint(self) -> None:
        runs = self._runs()
        self.assertEqual(len(tr.filter_runs(runs, backends=[])), 2)
        self.assertEqual(len(tr.filter_runs(runs, stages=None)), 2)

    def test_query_spans_output_steps_and_skills(self) -> None:
        runs = self._runs()
        # Output text.
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="resolved")], [1]
        )
        # Step content (a path inside a tool command).
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="file.py")], [1]
        )
        # Skill name, case-insensitive.
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="DEVELOP")], [1]
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
                    {_KIND: _ASSISTANT_MESSAGE,
                     _CONTENT_KEY: "I will refactor the cache layer"}]),
                seq=0,
            ),
            tr.parse_record(_record(issue=2), seq=1),
        ]
        self.assertEqual(
            [run.issue for run in tr.filter_runs(runs, query="refactor")], [1]
        )

    def test_filters_combine_preserving_order(self) -> None:
        base_runs = self._runs()
        matching_later = tr.parse_record(
            _record(
                issue=1,
                repo=_REPO_A,
                backend=_BACKEND_CLAUDE,
                agent_role=_ROLE_DEVELOPER,
                stage=_STAGE_IMPLEMENTING,
                output="resolved another bug",
            ),
            seq=9,
        )
        matching_fixture = tr.parse_record(
            _record(
                issue=1,
                repo=_REPO_A,
                backend=_BACKEND_CLAUDE,
                agent_role=_ROLE_DEVELOPER,
                stage=_STAGE_IMPLEMENTING,
                user_input=_IGNORED,
                output="resolved fixture bug",
            ),
            seq=8,
        )
        runs = [
            base_runs[0],
            matching_fixture,
            matching_later,
            base_runs[1],
        ]
        self.assertEqual(
            [
                run.seq
                for run in tr.filter_runs(
                    runs,
                    repo=_REPO_A,
                    backends=[_BACKEND_CLAUDE],
                    agent_roles=[_ROLE_DEVELOPER],
                    stages=[_STAGE_IMPLEMENTING],
                    issue=1,
                    query="RESOLVED",
                    exclude_fixtures=True,
                )
            ],
            [0, 9],
        )

    def test_exclude_fixtures_default_off(self) -> None:
        # Backward-compatible default: fixtures are kept unless asked to
        # drop them.
        runs = [
            tr.parse_record(_record(issue=1, user_input="real work",
                                    session_id="uuid-1"), seq=0),
            tr.parse_record(_record(issue=2, user_input=_IGNORED), seq=1),
        ]
        self.assertEqual(len(tr.filter_runs(runs)), 2)

    def test_exclude_fixtures_drops_every_tell(self) -> None:
        runs = [
            tr.parse_record(_record(issue=1, user_input="real work",
                                    session_id="uuid-1"), seq=0),
            tr.parse_record(_record(issue=2, user_input=_IGNORED), seq=1),
            tr.parse_record(_record(issue=3, session_id="sess-7"), seq=2),
            tr.parse_record(_record(issue=4, steps=[
                {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                 _CONTENT_KEY: _SKILL_DEVELOP}]), seq=3),
        ]
        kept = tr.filter_runs(runs, exclude_fixtures=True)
        self.assertEqual([run.issue for run in kept], [1])

    def test_fixture_exclusion_combines_with_filters(self) -> None:
        # An issue filter that selects a fixture still drops it.
        runs = [
            tr.parse_record(_record(issue=2, user_input=_IGNORED), seq=0),
        ]
        self.assertEqual(
            tr.filter_runs(runs, issue=2, exclude_fixtures=True), []
        )

    def _runs(self):
        return [
            tr.parse_record(
                _record(issue=1, repo=_REPO_A, backend=_BACKEND_CLAUDE,
                        agent_role=_ROLE_DEVELOPER, stage=_STAGE_IMPLEMENTING,
                        output="resolved the bug",
                        steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                                _CONTENT_KEY: "grep needle file.py"}],
                        skills_triggered=[_SKILL_DEVELOP]),
                seq=0,
            ),
            tr.parse_record(
                _record(issue=2, repo=_REPO_B, backend=_BACKEND_CODEX,
                        agent_role="reviewer", stage=_STAGE_IN_REVIEW,
                        output="looks good"),
                seq=1,
            ),
        ]


class SummarizeTest(unittest.TestCase):

    def test_counts(self) -> None:
        runs = [
            tr.parse_record(
                _record(issue=1, repo=_REPO_A,
                        steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH},
                               {_KIND: _TOOL_RESULT, _TOOL_ID: "t"}],
                        truncated=True),
                seq=0,
            ),
            tr.parse_record(
                _record(issue=1, repo=_REPO_A,
                        steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT}]),
                seq=1,
            ),
            tr.parse_record(_record(issue=2, repo=_REPO_B), seq=2),
        ]
        summary = tr.summarize(runs)
        self.assertEqual(summary.total_runs, 3)
        # Two runs share (a/a, 1); (b/b, 2) is the third distinct issue.
        self.assertEqual(summary.distinct_issues, 2)
        self.assertEqual(summary.distinct_repos, 2)
        self.assertEqual(summary.total_tool_calls, 2)
        self.assertEqual(summary.truncated_runs, 1)

    def test_empty(self) -> None:
        summary = tr.summarize([])
        self.assertEqual(
            (summary.total_runs, summary.distinct_issues, summary.distinct_repos,
             summary.total_tool_calls, summary.truncated_runs, summary.total_cost_usd),
            (0, 0, 0, 0, 0, 0.0),
        )

    def test_total_cost_sums_only_priced_runs(self) -> None:
        # The KPI sums the authoritative run cost; a run with no run_usage
        # (pre-usage record) or an unpriced cost (None) contributes nothing
        # rather than a spurious 0.
        runs = [
            tr.parse_record(_record(
                issue=1,
                run_usage={_COST_USD: 0.8, _COST_SOURCE: _REPORTED}),
                seq=0),
            tr.parse_record(_record(
                issue=2,
                run_usage={_COST_USD: 0.2, _COST_SOURCE: "estimated"}),
                seq=1),
            tr.parse_record(_record(issue=3), seq=2),
            tr.parse_record(_record(
                issue=4,
                run_usage={_COST_USD: None, _COST_SOURCE: _UNKNOWN_PRICE}),
                seq=3),
        ]
        self.assertAlmostEqual(tr.summarize(runs).total_cost_usd, 1.0)


class LabelTest(unittest.TestCase):

    def test_label_carries_issue_repo_and_round(self) -> None:
        run = tr.parse_record(
            _record(issue=_ISSUE, repo=_REPO_A, stage=_STAGE_IMPLEMENTING,
                    agent_role=_ROLE_DEVELOPER, backend=_BACKEND_CLAUDE,
                    review_round=1),
            seq=0,
        )
        label = run.label()
        self.assertIn("#42", label)
        self.assertIn(_REPO_A, label)
        self.assertIn("implementing/developer", label)
        self.assertIn("round 1", label)

    def test_label_without_round_omits_it(self) -> None:
        run = tr.parse_record(_record(), seq=0)
        self.assertNotIn("round", run.label())

    def test_detail_label_drops_issue_and_repo(self) -> None:
        run = tr.parse_record(
            _record(issue=_ISSUE, repo=_REPO_A, stage="documenting",
                    agent_role=_ROLE_DEVELOPER, backend=_BACKEND_CLAUDE,
                    review_round=0),
            seq=0,
        )
        detail = run.detail_label()
        self.assertIn("documenting/developer · claude · round 0", detail)
        self.assertIn(run.ts, detail)
        # The repo / issue are picked separately, so they are dropped here.
        self.assertNotIn("#42", detail)
        self.assertNotIn(_REPO_A, detail)

    def test_label_is_issue_repo_plus_detail_label(self) -> None:
        run = tr.parse_record(
            _record(issue=7, repo=_REPO_A, stage=_STAGE_IMPLEMENTING,
                    agent_role=_ROLE_DEVELOPER, backend=_BACKEND_CLAUDE,
                    review_round=1),
            seq=0,
        )
        self.assertEqual(
            run.label(), "#7 a/a · {0}".format(run.detail_label())
        )


class TimelineTest(unittest.TestCase):
    """`TrajectoryRun.timeline` normalizes old and new records alike."""

    def test_old_step_record_wraps_prompt_and_output(self) -> None:
        # A legacy record predates the text-turn timeline: its steps are
        # only tool_call / tool_result. The normalized timeline still
        # brackets them with the prompt and the final output, in order.
        run = tr.parse_record(
            _record(
                user_input=_PROMPT_DO_THING,
                output="all done",
                steps=[
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                     _TOOL_ID: _T1, _CONTENT_KEY: _LS},
                    {_KIND: _TOOL_RESULT, _TOOL_ID: _T1,
                     _CONTENT_KEY: "calc.py"},
                ],
            ),
            seq=0,
        )
        self.assertEqual(
            [entry.kind for entry in run.timeline],
            [_TL_PROMPT, _TOOL_CALL, _TOOL_RESULT, _TL_OUTPUT],
        )
        self.assertTrue(run.timeline[0].is_prompt)
        self.assertEqual(run.timeline[0].content, _PROMPT_DO_THING)
        self.assertTrue(run.timeline[-1].is_output)
        self.assertEqual(run.timeline[-1].content, "all done")
        # The middle tool_call keeps its name / id; the brackets carry none.
        call = run.timeline[1]
        self.assertEqual(call.name, _TOOL_BASH)
        self.assertEqual(call.tool_id, _T1)
        self.assertEqual(run.timeline[0].name, "")
        self.assertEqual(run.timeline[0].tool_id, "")

    def test_mixed_timeline_keeps_interleaved_turns(self) -> None:
        # A record written since the timeline feature interleaves
        # assistant / user text turns with the tool steps; the normalized
        # timeline keeps stream order and adds the prompt / output brackets.
        run = tr.parse_record(
            _record(
                user_input="fix the parser",
                output="fixed",
                steps=[
                    {_KIND: _ASSISTANT_MESSAGE, _CONTENT_KEY: "let me look"},
                    {_KIND: _TOOL_CALL, _NAME: "Read", _TOOL_ID: "r1",
                     _CONTENT_KEY: "open x.py"},
                    {_KIND: _TOOL_RESULT, _TOOL_ID: "r1",
                     _CONTENT_KEY: "body"},
                    {_KIND: "user_message", _CONTENT_KEY: "now ship it"},
                    {_KIND: _ASSISTANT_MESSAGE, _CONTENT_KEY: _DONE},
                ],
            ),
            seq=0,
        )
        self.assertEqual(
            [entry.kind for entry in run.timeline],
            [_TL_PROMPT, _ASSISTANT_MESSAGE, _TOOL_CALL, _TOOL_RESULT,
             "user_message", _ASSISTANT_MESSAGE, _TL_OUTPUT],
        )

    def test_tool_calls_count_excludes_message_turns(self) -> None:
        # The message turns are steps but must not be counted as tool
        # calls -- the tally stays correct across record vintages.
        run = tr.parse_record(
            _record(steps=[
                {_KIND: _ASSISTANT_MESSAGE, _CONTENT_KEY: "thinking"},
                {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH, _CONTENT_KEY: _LS},
                {_KIND: _TOOL_RESULT, _TOOL_ID: "t", _CONTENT_KEY: "out"},
                {_KIND: "user_message", _CONTENT_KEY: "go on"},
                {_KIND: _TOOL_CALL, _NAME: _TOOL_EDIT, _CONTENT_KEY: "patch"},
            ]),
            seq=0,
        )
        self.assertEqual(run.step_count, 5)
        self.assertEqual(run.tool_calls, 2)

    def test_brackets_omitted_when_field_empty(self) -> None:
        # No prompt and no output: the timeline is exactly the steps.
        run = tr.parse_record(
            _record(user_input="", output="",
                    steps=[{_KIND: _TOOL_CALL, _NAME: _TOOL_BASH}]),
            seq=0,
        )
        self.assertEqual([entry.kind for entry in run.timeline], [_TOOL_CALL])

    def test_prompt_only_record_is_single_bracket(self) -> None:
        run = tr.parse_record(
            _record(user_input="just a prompt", output=""), seq=0
        )
        timeline = run.timeline
        self.assertEqual([entry.kind for entry in timeline], [_TL_PROMPT])
        self.assertEqual(timeline[0].content, "just a prompt")

    def test_empty_record_has_empty_timeline(self) -> None:
        run = tr.parse_record(_record(user_input="", output=""), seq=0)
        self.assertEqual(run.timeline, ())


class FixtureIdentificationTest(unittest.TestCase):
    """`TrajectoryRun.is_fixture` flags synthetic test-suite records."""

    def test_ignored_prompt_is_fixture(self) -> None:
        self.assertTrue(
            tr.parse_record(_record(user_input=_IGNORED), seq=0).is_fixture
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
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                     _CONTENT_KEY: _SKILL_DEVELOP},
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                     _CONTENT_KEY: "review"},
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
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_SKILL,
                     _CONTENT_KEY: _SKILL_DEVELOP},
                    {_KIND: _TOOL_CALL, _NAME: _TOOL_BASH,
                     _CONTENT_KEY: "pytest"},
                    {_KIND: _TOOL_RESULT, _TOOL_ID: "t", _CONTENT_KEY: "ok"},
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


def _reload_reader_world(log_path, hermetic):
    """Reload analytics + reader against `log_path` and return the fresh pair.

    Pops only the PUBLIC modules a caller would reload -- not the private
    `_trajectory_records` leaf -- so the reload test exercises the facade's own
    eviction rather than masking it.
    """
    import importlib
    reload_env = {**hermetic, _LOG_PATH_ATTR: str(log_path)}
    with patch.dict(os.environ, reload_env, clear=True):
        for name in (_READER_MODULE, _ANALYTICS_MODULE, _CONFIG_MODULE):
            sys.modules.pop(name, None)
        # Re-import through `importlib` so a popped submodule is rebuilt
        # rather than resolved from the parent package's stale attribute.
        fresh_analytics = importlib.import_module(_ANALYTICS_MODULE)
        fresh_reader = importlib.import_module(_READER_MODULE)
        return fresh_analytics, fresh_reader


def _snapshot_and_arm_orchestrator_reset(test):
    """Snapshot every `orchestrator*` module + the package namespace, restore after `test`.

    Importing a submodule binds it as an attribute of its parent package, so an
    A/B reload rebinds `orchestrator.analytics` (and `.config` /
    `.trajectory_reader` / `._trajectory_records`) on the persistent
    `orchestrator` package object. Restoring `sys.modules` alone would leave
    `from orchestrator import analytics` (how the reader leaf resolves
    `TRAJECTORY_LOG_PATH`) pointing at a discarded reload, so the package's own
    namespace is snapshotted and reverted too.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name.startswith(_ORCHESTRATOR_PKG)
    }
    orchestrator_pkg = sys.modules[_ORCHESTRATOR_PKG]
    test.addCleanup(
        _restore_orchestrator_modules, saved, orchestrator_pkg,
        dict(orchestrator_pkg.__dict__),
    )


def _restore_orchestrator_modules(saved, orchestrator_pkg, saved_pkg_attrs):
    """Evict the current `orchestrator*` modules and reinstate the snapshot."""
    stale = [
        name for name in sys.modules
        if name.startswith(_ORCHESTRATOR_PKG)
    ]
    for name in stale:
        sys.modules.pop(name, None)
    sys.modules.update(saved)
    orchestrator_pkg.__dict__.clear()
    orchestrator_pkg.__dict__.update(saved_pkg_attrs)


class ModuleLayoutTest(unittest.TestCase):
    """Pin the facade / read-leaf split so callers keep one import site.

    The record and view dataclasses, the log-path resolution, and the JSONL
    parsing / reading pipeline live in the private
    `orchestrator._trajectory_records` leaf; `orchestrator.trajectory_reader`
    re-exports them under the same names and owns the filtering and
    summary aggregation. The dashboard and the tests reach everything through
    `trajectory_reader`, so the re-exported names must stay the same objects
    the leaf defines and the filter surface must stay defined on the facade.
    """

    def test_read_surface_reexported_from_leaf(self) -> None:
        for name in (
            "TrajectoryStepView",
            "TimelineEntry",
            "TurnUsageView",
            "RunUsageView",
            "TrajectoryRun",
            "resolve_log_path",
            "log_unconfigured_message",
            "read_trajectories",
            "parse_record",
            "TRAJECTORY_EVENT",
            "TIMELINE_PROMPT",
            "TIMELINE_OUTPUT",
            "UNCONFIGURED_LOG_MESSAGE",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(tr, name), getattr(records, name))

    def test_read_symbols_have_leaf_module_of_record(self) -> None:
        for symbol in (
            tr.TrajectoryRun,
            tr.TrajectoryStepView,
            tr.parse_record,
            tr.read_trajectories,
            tr.resolve_log_path,
        ):
            with self.subTest(symbol=symbol.__name__):
                self.assertEqual(
                    symbol.__module__, "orchestrator._trajectory_records"
                )

    def test_filter_surface_defined_on_facade(self) -> None:
        for symbol in (
            tr.FilterOptions,
            tr.RunFilterOptions,
            tr.TrajectorySummary,
            tr.filter_options,
            tr.filter_runs,
            tr.summarize,
        ):
            with self.subTest(symbol=symbol.__name__):
                self.assertEqual(
                    symbol.__module__, _READER_MODULE
                )

    def test_reload_binds_reader_to_its_world(self) -> None:
        """A reloaded reader resolves its own world's `TRAJECTORY_LOG_PATH`.

        Reloading `orchestrator.analytics` and `orchestrator.trajectory_reader`
        together must give the fresh reader a leaf bound to the fresh analytics
        instance, and the earlier world's reader must keep resolving the earlier
        world's path -- the A/B env isolation the single-module reader had.
        Without the facade evicting its cached `_trajectory_records`, the fresh
        reader would re-export the stale leaf and resolve the previous path.
        """
        hermetic = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        _snapshot_and_arm_orchestrator_reset(self)
        with tempfile.TemporaryDirectory() as work_dir:
            path_a = Path(work_dir) / "a.jsonl"
            analytics_a, reader_a = self._load_world(path_a, hermetic)
            analytics_b, reader_b = self._load_world(
                Path(work_dir) / "b.jsonl", hermetic
            )
            # Each reader's leaf is bound to its own analytics instance, so
            # world A still resolves world A after world B has been loaded.
            self.assertIsNot(reader_a, reader_b)
            self.assertIsNot(analytics_a, analytics_b)
            self.assertEqual(reader_a.resolve_log_path(), path_a)

    def _load_world(self, path, hermetic):
        """Reload the reader against `path` and confirm it resolves that path."""
        fresh_analytics, fresh_reader = _reload_reader_world(path, hermetic)
        self.assertEqual(fresh_reader.resolve_log_path(), path)
        return fresh_analytics, fresh_reader


if __name__ == "__main__":
    unittest.main()
