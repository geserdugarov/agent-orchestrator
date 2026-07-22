# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory run, turn, and timeline usage HTML tests."""

import unittest


from orchestrator import trajectory_reader as tr


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


_ISSUE = 42


_TURN_INPUT = 12


_TURN_OUTPUT = 340


_TURN_CACHE_READ = 18240


_TURN_CACHE_WRITE = 512


_TURN_COST = 0.0123


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


def _claude_run_usage():
    """Run-summary usage payload for the claude per-turn HTML path."""
    return {
        "models": [_MODEL_CLAUDE],
        _INPUT_TOKENS: 41230,
        _OUTPUT_TOKENS: 5120,
        "cached_tokens": 0,
        _CACHE_READ: 812440,
        _CACHE_WRITE: 20110,
        "turns": 9,
        _COST_USD: 0.83,
        _COST_SOURCE: "reported",
    }


def _turn(**overrides):
    base = dict(
        turn=0,
        model=_MODEL_CLAUDE,
        input_tokens=_TURN_INPUT,
        output_tokens=_TURN_OUTPUT,
        cache_read_tokens=_TURN_CACHE_READ,
        cache_write_tokens=_TURN_CACHE_WRITE,
        cost_usd=_TURN_COST,
        cost_source=_ESTIMATED,
    )
    base.update(overrides)
    return tr.TurnUsageView(**base)


class RunUsageHtmlTest(unittest.TestCase):
    def test_claude_summary_chips_and_estimate_note(self) -> None:
        run = _run(
            run_usage=_claude_run_usage(),
            turns=[
                {
                    _TURN: 0,
                    "model": _MODEL_CLAUDE,
                    _INPUT_TOKENS: _TURN_INPUT,
                    _OUTPUT_TOKENS: _TURN_OUTPUT,
                    _CACHE_READ: _TURN_CACHE_READ,
                    _CACHE_WRITE: _TURN_CACHE_WRITE,
                    _COST_USD: _TURN_COST,
                    _COST_SOURCE: _ESTIMATED,
                }
            ],
        )
        html = _td()._run_usage_html(run)
        for fragment in (
            ">Run usage</span>",
            _MODEL_CLAUDE,
            "9 turns",
            "cache-read 812,440",
            "cache-write 20,110",
            "reported $0.83",
            "orch-traj-chip cost",
            "authoritative when reported",
            "claude-only estimates",
            "need not sum to it",
        ):
            self.assertIn(fragment, html)
        # `cached_tokens` is 0 on claude -> no always-zero cached chip.
        self.assertNotIn("cached ", html)
        # Authoritative run cost with its source, exact to the cent.
        # Note carries both honesty points for the claude (per-turn) path.

    def test_codex_summary_shows_not_available_note(self) -> None:
        run = _run(
            backend="codex",
            run_usage={
                "models": ["gpt-5-codex"],
                _INPUT_TOKENS: 1000,
                _OUTPUT_TOKENS: 200,
                "cached_tokens": 500,
                "turns": 3,
                _COST_USD: 0.05,
                _COST_SOURCE: _ESTIMATED,
            },
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
        self.assertNotIn("cache hit", _td()._turn_usage_html(_turn(cache_read_tokens=0)))

    def test_unpriced_turn_reads_est_na(self) -> None:
        html = _td()._turn_usage_html(_turn(cost_usd=None, cost_source="unknown-price"))
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
        run = _run(
            steps=[
                {_KIND: _TOOL_CALL, "name": _TOOL_BASH},
                {_KIND: _TOOL_RESULT, _TOOL_ID: "t"},
            ]
        )
        paired = _td()._timeline_with_usage(run)
        self.assertTrue(paired)
        self.assertTrue(all(strip is None for strip, _ in paired))

    def _run_with_turns(self):
        return _run(
            steps=[
                {_KIND: "assistant_message", "content": "a", _TURN: 0},
                {_KIND: _TOOL_CALL, "name": "Edit", _TOOL_ID: _T1, _TURN: 0},
                {_KIND: _TOOL_RESULT, _TOOL_ID: _T1},
                {_KIND: "assistant_message", "content": "b", _TURN: 1},
            ],
            turns=[
                {
                    _TURN: 0,
                    "model": "m",
                    _INPUT_TOKENS: 1,
                    _OUTPUT_TOKENS: 2,
                    _CACHE_READ: 3,
                    _CACHE_WRITE: 4,
                    _COST_USD: 0.01,
                    _COST_SOURCE: _ESTIMATED,
                },
                {
                    _TURN: 1,
                    "model": "m",
                    _INPUT_TOKENS: 5,
                    _OUTPUT_TOKENS: 6,
                    _CACHE_READ: 0,
                    _CACHE_WRITE: 0,
                    _COST_USD: 0.02,
                    _COST_SOURCE: _ESTIMATED,
                },
            ],
        )
