# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the non-Streamlit logic in `orchestrator.trajectory_dashboard`.

The Streamlit import inside `trajectory_dashboard.main` is deliberately
lazy so the orchestrator polling tick never pulls the optional
`dashboard` group in. These tests exercise the pure inline-HTML builders
(topbar, KPI strip, metadata grid, chips, run table, timeline entry,
fixture-aware run picker label) and assert the same two invariants the
analytics dashboard holds: the module loads without `streamlit`
installed, and the `streamlit run orchestrator/trajectory_dashboard.py`
script-launch `sys.path` shape resolves the absolute imports.
"""
from __future__ import annotations

import importlib
import runpy
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator import trajectory_reader as tr

# Trajectory step / turn JSON field keys and step kinds asserted in the
# inline-HTML fixtures.
_KIND = "kind"
_TOOL_ID = "tool_id"
_TOOL_CALL = "tool_call"
_TOOL_RESULT = "tool_result"
_INPUT_TOKENS = "input_tokens"
_OUTPUT_TOKENS = "output_tokens"
_CACHE_READ = "cache_read_tokens"
_CACHE_WRITE = "cache_write_tokens"
_COST_USD = "cost_usd"
_COST_SOURCE = "cost_source"
_TURN = "turn"
_ESTIMATED = "estimated"

_MODEL_CLAUDE = "claude-opus-4-8"
_TOOL_BASH = "Bash"
_T1 = "t1"
_REPO_UNSAFE = "o/<r&>"

_ISSUE = 42
_TURN_INPUT = 12
_TURN_OUTPUT = 340
_TURN_CACHE_READ = 18240
_TURN_CACHE_WRITE = 512
_TURN_COST = 0.0123

# Module / package names the script-launch guards resolve.
_ORCH = "orchestrator"
_ORCH_PREFIX = "orchestrator."
_SCRIPT_LAUNCH = "script_launch"
_DASHBOARD_MODULE = "orchestrator.trajectory_dashboard"
_SCRIPT_LAUNCH_MODULE = "orchestrator.script_launch"
_READER_MODULE = "orchestrator.trajectory_reader"


def _is_orchestrator_module(name):
    return name == _ORCH or name.startswith(_ORCH_PREFIX)


def _is_orch_or_script_launch(name):
    return _is_orchestrator_module(name) or name == _SCRIPT_LAUNCH


def _is_stray_launch_module(name):
    return name in (_DASHBOARD_MODULE, _SCRIPT_LAUNCH_MODULE, _SCRIPT_LAUNCH)


def _snapshot_modules(predicate):
    return {
        name: module
        for name, module in sys.modules.items()
        if predicate(name)
    }


def _drop_modules(predicate):
    stale = [name for name in sys.modules if predicate(name)]
    for name in stale:
        sys.modules.pop(name, None)


def _strip_repo_root(repo_root):
    resolved_root = repo_root.resolve()
    kept = [
        entry for entry in sys.path
        if not entry or Path(entry).resolve() != resolved_root
    ]
    sys.path.clear()
    sys.path.extend(kept)


def _restore_launch_state(original_path, saved_modules, predicate):
    sys.path.clear()
    sys.path.extend(original_path)
    _drop_modules(predicate)
    sys.modules.update(saved_modules)


def _arm_launch_cleanup(test, predicate):
    """Snapshot `sys.path` + the matching modules and register their restore.

    A `runpy` script-launch strips the repo root and evicts `orchestrator.*`;
    capturing the pre-launch state here undoes it after the test, whatever it
    asserts.
    """
    test.addCleanup(
        _restore_launch_state,
        list(sys.path),
        _snapshot_modules(predicate),
        predicate,
    )


def _td():
    from orchestrator import trajectory_dashboard as td
    return td


def _run(**overrides):
    record = {
        "ts": "2026-06-20T10:00:00+00:00",
        "repo": "acme/widgets",
        "issue": _ISSUE,
        "event": "agent_trajectory",
        "stage": "implementing",
        "agent_role": "developer",
        "backend": "claude",
        "steps": [],
    }
    record.update(overrides)
    return tr.parse_record(record, seq=0)


class LazyImportTest(unittest.TestCase):
    """The page module must load without importing `streamlit`,
    `pandas`, or `plotly` -- the same boundary `orchestrator.dashboard`
    holds so the polling tick never needs the dashboard group.
    """

    def test_dashboard_only_modules_absent_after_load(self) -> None:
        for mod in (
            "orchestrator.trajectory_dashboard",
            "streamlit",
            "pandas",
            "plotly",
        ):
            sys.modules.pop(mod, None)
        # `import_module` re-executes off the popped `sys.modules`, so the
        # load is real; a `from orchestrator import ...` could bind a stale
        # package attribute and pass without importing the module at all.
        importlib.import_module(_DASHBOARD_MODULE)
        self.assertNotIn("streamlit", sys.modules)
        self.assertNotIn("pandas", sys.modules)
        self.assertNotIn("plotly", sys.modules)


class ScriptPathLaunchTest(unittest.TestCase):
    """Guard `streamlit run orchestrator/trajectory_dashboard.py`.

    Streamlit executes the file as a top-level script via `runpy` with
    only the *script's* directory on `sys.path` (not the repo root), so a
    naked relative import or a bare absolute import without the sys.path
    shim raises `ImportError` before any Streamlit code can render. We
    reproduce that launch shape here without pulling Streamlit in (the
    dashboard group is opt-in): strip the repo root, insert the script's
    dir, then `runpy` the file with a non-`__main__` run name so `main()`
    is not invoked.
    """

    def test_runs_without_repo_root_on_syspath(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / _ORCH / "trajectory_dashboard.py"

        _arm_launch_cleanup(self, _is_orchestrator_module)

        _strip_repo_root(repo_root)
        sys.path.insert(0, str(script.parent))
        _drop_modules(_is_orchestrator_module)

        namespace = runpy.run_path(str(script), run_name="not_main")
        self.assertIn("main", namespace)
        self.assertIn("trajectory_reader", namespace)

    def test_stale_parent_cannot_shadow_repo(self) -> None:
        # With only `orchestrator/` on `sys.path`, importing `orchestrator.<x>`
        # before the shim prepends the repo root would bind the parent
        # `orchestrator` package to whatever stale copy is importable and route
        # every later absolute import through it. The shim adds the repo root
        # without importing `orchestrator.*` first, so the real package
        # resolves even with a decoy parent behind the script dir on the path.
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / _ORCH / "trajectory_dashboard.py"

        _arm_launch_cleanup(self, _is_orch_or_script_launch)
        with tempfile.TemporaryDirectory() as decoy_root:
            # A bare `orchestrator` package with none of the real submodules,
            # standing in for a stale install that shadows the repo root.
            decoy_pkg = Path(decoy_root) / _ORCH
            decoy_pkg.mkdir()
            (decoy_pkg / "__init__.py").write_text("")
            _strip_repo_root(repo_root)
            sys.path.insert(0, decoy_root)
            sys.path.insert(0, str(script.parent))
            _drop_modules(_is_orch_or_script_launch)

            namespace = runpy.run_path(str(script), run_name="not_main")
            self.assertIn("main", namespace)
            # The real reader landed -- not the decoy package (which has no
            # `trajectory_reader` submodule and would raise on import).
            self.assertEqual(
                namespace["trajectory_reader"].__name__,
                _READER_MODULE,
            )

    def test_package_import_ignores_stray_script(self) -> None:
        # A package import (`import orchestrator.trajectory_dashboard`) must
        # resolve the shim via `orchestrator.script_launch`, never a bare
        # `import script_launch`. An unrelated top-level `script_launch.py`
        # earlier on `sys.path` would otherwise shadow the helper or fail the
        # import outright, so the package path must not probe the bare name.
        _arm_launch_cleanup(self, _is_stray_launch_module)
        with tempfile.TemporaryDirectory() as stray_dir:
            # A stray top-level `script_launch` that detonates on import, so a
            # bare `import script_launch` during the package import would fail
            # loudly instead of silently binding the wrong helper.
            (Path(stray_dir) / "script_launch.py").write_text(
                "raise RuntimeError('stray script_launch must not be imported')\n"
            )
            sys.path.insert(0, stray_dir)
            _drop_modules(_is_stray_launch_module)
            module = importlib.import_module(_DASHBOARD_MODULE)
            self.assertTrue(hasattr(module, "main"))
            # The package path used `orchestrator.script_launch` and never
            # probed the bare name, so the stray stayed unimported.
            self.assertNotIn(_SCRIPT_LAUNCH, sys.modules)


class TopbarHtmlTest(unittest.TestCase):

    def test_carries_title_and_in_view_pill(self) -> None:
        html = _td()._topbar_html(10, 3)
        self.assertIn("orch-topbar", html)
        self.assertIn("Orchestrator Trajectories", html)
        self.assertIn("10 recorded", html)
        self.assertIn("3 / 10", html)


class KpiStripHtmlTest(unittest.TestCase):

    def test_tiles_truncated_foot_and_cost(self) -> None:
        summary = tr.TrajectorySummary(
            total_runs=5, distinct_issues=3, distinct_repos=2,
            total_tool_calls=11, truncated_runs=1, total_cost_usd=12.5,
        )
        html = _td()._kpi_strip_html(summary)
        self.assertIn("orch-kpis", html)
        for label in ("Runs", "Issues", "Repos", "Tool calls", "Total cost"):
            self.assertIn(">{0}</span>".format(label), html)
        self.assertIn("1 truncated", html)
        # Exact cents even above $10 -- the compact `fmt_money` would read
        # `$12`, dropping the authoritative figure's cents.
        self.assertIn(">$12.50</div>", html)

    def test_no_truncated_reads_none_and_zero_cost(self) -> None:
        html = _td()._kpi_strip_html(tr.TrajectorySummary(total_runs=2))
        self.assertIn("none truncated", html)
        self.assertIn(">$0.00</div>", html)


class MetaHtmlTest(unittest.TestCase):

    def test_only_present_fields_render(self) -> None:
        run = _run(session_id="sess-1", review_round=2)
        html = _td()._meta_html(run)
        self.assertIn(">Repo</div>", html)
        self.assertIn(">acme/widgets</div>", html)
        self.assertIn(">Review round</div>", html)
        self.assertIn(">sess-1</div>", html)
        # No retry_count on this run -> the tile is omitted entirely.
        self.assertNotIn(">Retry count</div>", html)

    def test_html_escaped(self) -> None:
        run = _run(repo=_REPO_UNSAFE)
        html = _td()._meta_html(run)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertNotIn(_REPO_UNSAFE, html)


class ChipsHtmlTest(unittest.TestCase):

    def test_label_and_pills(self) -> None:
        html = _td()._labeled_chips_html("Tools offered", [_TOOL_BASH, "Edit"])
        self.assertIn("Tools offered", html)
        self.assertIn(">Bash</span>", html)
        self.assertIn(">Edit</span>", html)

    def test_empty_is_blank(self) -> None:
        self.assertEqual(_td()._labeled_chips_html("Tools", []), "")

    def test_escaped(self) -> None:
        html = _td()._labeled_chips_html("Skills", ["<x>"])
        self.assertIn("&lt;x&gt;", html)
        self.assertNotIn("<x>", html)


class RunsTableHtmlTest(unittest.TestCase):

    def test_headers_and_row_cells(self) -> None:
        run = _run(
            issue=_ISSUE, review_round=1,
            steps=[{_KIND: _TOOL_CALL, "name": _TOOL_BASH},
                   {_KIND: _TOOL_RESULT, _TOOL_ID: "t"}],
        )
        html = _td()._runs_table_html([run])
        for header in ("Issue", "Repo", "Stage", "Role", "Backend",
                       "Round", "Steps", "Tool calls", "Recorded"):
            self.assertIn(">{0}</th>".format(header), html)
        self.assertIn("#42", html)
        self.assertIn(">acme/widgets</td>", html)
        # 2 steps, 1 of which is a tool call.
        self.assertIn(">2</td>", html)
        self.assertIn(">1</td>", html)

    def test_repo_escaped(self) -> None:
        html = _td()._runs_table_html([_run(repo=_REPO_UNSAFE)])
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertNotIn(_REPO_UNSAFE, html)

    def test_fixture_row_flagged(self) -> None:
        # `ignored` is the sentinel prompt that marks a synthetic fixture.
        run = _run(user_input="ignored")
        self.assertTrue(run.is_fixture)
        html = _td()._runs_table_html([run])
        self.assertIn('<tr class="fixture">', html)
        self.assertIn("orch-traj-fixture-tag", html)
        self.assertIn(">fixture</span>", html)

    def test_real_row_not_flagged(self) -> None:
        run = _run()
        self.assertFalse(run.is_fixture)
        html = _td()._runs_table_html([run])
        self.assertNotIn('class="fixture"', html)
        self.assertNotIn("orch-traj-fixture-tag", html)


class TimelineEntryHtmlTest(unittest.TestCase):

    def test_prompt_bracket_badge(self) -> None:
        entry = tr.TimelineEntry(kind=tr.TIMELINE_PROMPT, content="do x")
        html = _td()._timeline_entry_html(entry, 0)
        self.assertIn("orch-traj-badge prompt", html)
        self.assertIn(">prompt</span>", html)
        # 0-based index renders 1-based for humans.
        self.assertIn(">1</span>", html)

    def test_output_bracket_badge(self) -> None:
        entry = tr.TimelineEntry(kind=tr.TIMELINE_OUTPUT, content="done")
        html = _td()._timeline_entry_html(entry, 4)
        self.assertIn("orch-traj-badge output", html)
        self.assertIn(">final output</span>", html)
        self.assertIn(">5</span>", html)

    def test_tool_call_badge_name_and_id(self) -> None:
        entry = tr.TimelineEntry(kind=_TOOL_CALL, name=_TOOL_BASH, tool_id=_T1)
        html = _td()._timeline_entry_html(entry, 1)
        self.assertIn("orch-traj-badge call", html)
        self.assertIn(">tool call</span>", html)
        self.assertIn(">Bash</span>", html)
        self.assertIn(_T1, html)

    def test_tool_result_badge(self) -> None:
        entry = tr.TimelineEntry(kind=_TOOL_RESULT, tool_id=_T1)
        html = _td()._timeline_entry_html(entry, 2)
        self.assertIn("orch-traj-badge result", html)
        self.assertIn(">tool result</span>", html)

    def test_assistant_turn_badge(self) -> None:
        entry = tr.TimelineEntry(kind="assistant_message", content="hi")
        html = _td()._timeline_entry_html(entry, 0)
        self.assertIn("orch-traj-badge assistant", html)
        self.assertIn(">assistant</span>", html)

    def test_user_turn_badge(self) -> None:
        entry = tr.TimelineEntry(kind="user_message", content="more")
        html = _td()._timeline_entry_html(entry, 0)
        self.assertIn("orch-traj-badge user", html)
        self.assertIn(">user turn</span>", html)

    def test_unknown_kind_falls_through(self) -> None:
        entry = tr.TimelineEntry(kind="weird")
        html = _td()._timeline_entry_html(entry, 0)
        self.assertIn("orch-traj-badge result", html)
        self.assertIn(">weird</span>", html)

    def test_name_escaped(self) -> None:
        entry = tr.TimelineEntry(kind=_TOOL_CALL, name="<x>")
        html = _td()._timeline_entry_html(entry, 0)
        self.assertIn("&lt;x&gt;", html)
        self.assertNotIn("<x></span>", html)


class RunPickerLabelTest(unittest.TestCase):

    def test_fixture_run_prefixed(self) -> None:
        run = _run(session_id="sess-9")
        self.assertTrue(run.is_fixture)
        label = _td()._run_picker_label(run)
        self.assertTrue(label.startswith("[fixture] "))
        self.assertIn(run.detail_label(), label)

    def test_real_run_plain_label(self) -> None:
        # The per-run picker drops repo / issue (chosen in the cascading
        # selectors above it) and shows only the `detail_label` cohort.
        run = _run()
        self.assertEqual(_td()._run_picker_label(run), run.detail_label())
        self.assertNotIn(run.repo, _td()._run_picker_label(run))


def _claude_run_usage():
    """Run-summary usage payload for the claude per-turn HTML path."""
    return {
        "models": [_MODEL_CLAUDE],
        _INPUT_TOKENS: 41230, _OUTPUT_TOKENS: 5120, "cached_tokens": 0,
        _CACHE_READ: 812440, _CACHE_WRITE: 20110,
        "turns": 9, _COST_USD: 0.83, _COST_SOURCE: "reported",
    }


def _turn(**overrides):
    base = dict(
        turn=0, model=_MODEL_CLAUDE,
        input_tokens=_TURN_INPUT, output_tokens=_TURN_OUTPUT,
        cache_read_tokens=_TURN_CACHE_READ, cache_write_tokens=_TURN_CACHE_WRITE,
        cost_usd=_TURN_COST, cost_source=_ESTIMATED,
    )
    base.update(overrides)
    return tr.TurnUsageView(**base)


class RunUsageHtmlTest(unittest.TestCase):

    def test_claude_summary_chips_and_estimate_note(self) -> None:
        run = _run(
            run_usage=_claude_run_usage(),
            turns=[{_TURN: 0, "model": _MODEL_CLAUDE,
                    _INPUT_TOKENS: _TURN_INPUT, _OUTPUT_TOKENS: _TURN_OUTPUT,
                    _CACHE_READ: _TURN_CACHE_READ, _CACHE_WRITE: _TURN_CACHE_WRITE,
                    _COST_USD: _TURN_COST, _COST_SOURCE: _ESTIMATED}],
        )
        html = _td()._run_usage_html(run)
        self.assertIn(">Run usage</span>", html)
        self.assertIn(_MODEL_CLAUDE, html)
        self.assertIn("9 turns", html)
        self.assertIn("cache-read 812,440", html)
        self.assertIn("cache-write 20,110", html)
        # `cached_tokens` is 0 on claude -> no always-zero cached chip.
        self.assertNotIn("cached ", html)
        # Authoritative run cost with its source, exact to the cent.
        self.assertIn("reported $0.83", html)
        self.assertIn("orch-traj-chip cost", html)
        # Note carries both honesty points for the claude (per-turn) path.
        self.assertIn("authoritative when reported", html)
        self.assertIn("claude-only estimates", html)
        self.assertIn("need not sum to it", html)

    def test_codex_summary_shows_not_available_note(self) -> None:
        run = _run(
            backend="codex",
            run_usage={"models": ["gpt-5-codex"], _INPUT_TOKENS: 1000,
                       _OUTPUT_TOKENS: 200, "cached_tokens": 500,
                       "turns": 3, _COST_USD: 0.05,
                       _COST_SOURCE: _ESTIMATED},
            turns=[],
        )
        html = _td()._run_usage_html(run)
        self.assertIn("gpt-5-codex", html)
        # Codex has no read/write split, so `cached_tokens` is its only cache
        # signal and must reach the row.
        self.assertIn("cached 500", html)
        self.assertIn("estimated $0.05", html)
        # Codex has no per-turn detail: it gets the run summary plus a note,
        # and never the per-turn estimate caveat.
        self.assertIn("not available for this backend", html)
        self.assertNotIn("need not sum to it", html)

    def test_pre_usage_record_renders_nothing(self) -> None:
        self.assertEqual(_td()._run_usage_html(_run()), "")

    def test_unpriced_run_names_source_without_cost(self) -> None:
        run = _run(run_usage={"models": [], _COST_SOURCE: "no-usage"})
        html = _td()._run_usage_html(run)
        # Unpriced -> the cost chip names the source, no dollar figure.
        self.assertIn(">no-usage</span>", html)
        self.assertNotIn("$", html)


class TurnUsageHtmlTest(unittest.TestCase):

    def test_strip_carries_model_tokens_and_est_cost(self) -> None:
        html = _td()._turn_usage_html(_turn())
        self.assertIn("orch-traj-turn", html)
        self.assertIn(_MODEL_CLAUDE, html)
        self.assertIn("in 12 tok", html)
        self.assertIn("out 340 tok", html)
        self.assertIn("cache-read 18,240", html)
        self.assertIn("cache-write 512", html)
        # Sub-cent precision so a small estimate is not floored to `$0.00`.
        self.assertIn("est. $0.0123", html)

    def test_cache_hit_chip_only_when_cache_read(self) -> None:
        self.assertIn("cache hit", _td()._turn_usage_html(_turn()))
        self.assertNotIn(
            "cache hit", _td()._turn_usage_html(_turn(cache_read_tokens=0))
        )

    def test_unpriced_turn_reads_est_na(self) -> None:
        html = _td()._turn_usage_html(
            _turn(cost_usd=None, cost_source="unknown-price")
        )
        self.assertIn("est. n/a", html)

    def test_model_escaped(self) -> None:
        html = _td()._turn_usage_html(_turn(model="<m>"))
        self.assertIn("&lt;m&gt;", html)
        self.assertNotIn("<m></span>", html)


class TimelineUsageBoundaryTest(unittest.TestCase):
    """`_timeline_with_usage` pairs each entry with the strip drawn above it:
    a strip on the first entry of every assistant turn, `None` everywhere
    else -- turn inputs and later entries of the same turn included.
    """

    def test_strip_only_at_first_entry_of_each_turn(self) -> None:
        paired = _td()._timeline_with_usage(self._run_with_turns())
        strips = [strip for strip, _ in paired]
        self.assertEqual(len(strips), 4)
        self.assertIsNotNone(strips[0])
        self.assertEqual(strips[0].turn, 0)
        # Same turn's tool call and the turn-input result carry no strip.
        self.assertIsNone(strips[1])
        self.assertIsNone(strips[2])
        self.assertIsNotNone(strips[3])
        self.assertEqual(strips[3].turn, 1)

    def test_no_strip_on_turn_none_entries(self) -> None:
        for strip, entry in _td()._timeline_with_usage(self._run_with_turns()):
            if entry.turn is None:
                self.assertIsNone(strip)

    def test_pre_usage_run_pairs_entries_with_none(self) -> None:
        run = _run(steps=[{_KIND: _TOOL_CALL, "name": _TOOL_BASH},
                          {_KIND: _TOOL_RESULT, _TOOL_ID: "t"}])
        paired = _td()._timeline_with_usage(run)
        self.assertTrue(paired)
        self.assertTrue(all(strip is None for strip, _ in paired))

    def _run_with_turns(self):
        return _run(
            steps=[
                {_KIND: "assistant_message", "content": "a", _TURN: 0},
                {_KIND: _TOOL_CALL, "name": "Edit", _TOOL_ID: _T1,
                 _TURN: 0},
                {_KIND: _TOOL_RESULT, _TOOL_ID: _T1},
                {_KIND: "assistant_message", "content": "b", _TURN: 1},
            ],
            turns=[
                {_TURN: 0, "model": "m", _INPUT_TOKENS: 1,
                 _OUTPUT_TOKENS: 2, _CACHE_READ: 3,
                 _CACHE_WRITE: 4, _COST_USD: 0.01,
                 _COST_SOURCE: _ESTIMATED},
                {_TURN: 1, "model": "m", _INPUT_TOKENS: 5,
                 _OUTPUT_TOKENS: 6, _CACHE_READ: 0,
                 _CACHE_WRITE: 0, _COST_USD: 0.02,
                 _COST_SOURCE: _ESTIMATED},
            ],
        )


class CardHeaderHtmlTest(unittest.TestCase):

    def test_title_and_sub_escaped(self) -> None:
        html = _td()._card_header_html("Title <b>", "Sub & more")
        self.assertIn("orch-card-title", html)
        self.assertIn("Title &lt;b&gt;", html)
        self.assertIn("Sub &amp; more", html)


# The pure inline-HTML builders defined in the Streamlit-free leaf that the
# page module exposes under the same name.
_LEAF_HTML_MEMBERS = (
    "_topbar_html",
    "_kpi_strip_html",
    "_card_header_html",
    "_meta_html",
    "_labeled_chips_html",
    "_run_usage_html",
    "_runs_table_html",
    "_run_picker_label",
    "_timeline_entry_html",
    "_timeline_with_usage",
    "_turn_usage_html",
)


class TrajectoryHtmlExtractionTest(unittest.TestCase):
    """The trajectory viewer's pure inline-HTML builders live in the
    Streamlit-free `orchestrator._trajectory_dashboard_html` leaf, and
    `orchestrator.trajectory_dashboard` exposes each under the same name
    so the page (and these tests) resolve to the same object.
    """

    def test_html_members_defined_in_leaf(self) -> None:
        from orchestrator import _trajectory_dashboard_html as leaf
        for name in _LEAF_HTML_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(leaf, name).__module__,
                    "orchestrator._trajectory_dashboard_html",
                )

    def test_page_reaches_the_leaf_objects(self) -> None:
        from orchestrator import _trajectory_dashboard_html as leaf
        page = _td()
        for name in _LEAF_HTML_MEMBERS:
            with self.subTest(name=name):
                self.assertIs(getattr(page, name), getattr(leaf, name))


if __name__ == "__main__":
    unittest.main()
