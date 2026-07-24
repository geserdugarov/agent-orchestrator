# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared agent dispatch: selection, option normalization, result, logging."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import agents as _agents
from orchestrator.agents import models as _models
from orchestrator.agents import runner as _runner
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases

# (backend, facade alias) pairs so dispatch assertions cover both backends
# without duplicating the body per backend.
_BACKEND_ALIASES = (
    (_agent_cases._CODEX, "_run_codex"),
    (_agent_cases._CLAUDE, "_run_claude"),
)
_OPTIONS_KWARG = "options"
_LOGGER_NAME = "orchestrator.agents"
_PARSED_SESSION_ID = "cafe1234-5678-90ab-cdef-1234567890ab"
_KEPT_SESSION_ID = "deadbeef-1234-1234-1234-1234deadbeef"
_EXTRA_ARGS = (_agent_cases._MODEL_FLAG, _agent_cases._CODEX_MODEL)
_FINAL_MESSAGE = "final"
_TIMEOUT_SECONDS = 42


class RunAgentDispatchTest(unittest.TestCase):
    def test_unknown_backend_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "gemini"):
            _runner.run_agent("gemini", _agent_cases._PROMPT, _agent_cases._CWD)

    def test_dispatch_honors_patched_backend_alias(self) -> None:
        # `agents._run_codex` / `agents._run_claude` are the historical patch
        # site for redirecting a backend; dispatch reads the alias off the
        # facade at call time, so an override installed with `patch.object`
        # is honored rather than bypassed by the lazy re-export resolver.
        for backend, alias in _BACKEND_ALIASES:
            with self.subTest(backend=backend):
                sentinel = object()
                fake = MagicMock(return_value=sentinel)
                with patch.object(_agents, alias, fake):
                    dispatched = _runner.run_agent(
                        backend,
                        _agent_cases._PROMPT,
                        _agent_cases._CWD,
                    )
                self.assertIs(dispatched, sentinel)
                fake.assert_called_once()

    def test_dispatches_to_codex(self) -> None:
        # Use stream-json-shaped output so the session parser has something to
        # find; the codex runner doesn't care about claude shape.
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(
                stdout=json.dumps({_agent_cases._SESSION_ID_FIELD: _PARSED_SESSION_ID}),
            ),
        ) as run_mock:
            agent_result = _runner.run_agent(
                _agent_cases._CODEX,
                _agent_cases._PROMPT,
                _agent_cases._CWD,
            )
            argv = list(run_mock.call_args.args[0])
        self.assertEqual(agent_result.session_id, _PARSED_SESSION_ID)
        self.assertEqual(agent_result.exit_code, 0)
        self.assertEqual(argv[1], _agent_cases._CODEX_EXEC)

    def test_dispatches_to_claude(self) -> None:
        events = [
            json.dumps({_agent_cases._TYPE_FIELD: "system", _agent_cases._SESSION_ID_FIELD: _PARSED_SESSION_ID}),
            json.dumps({_agent_cases._TYPE_FIELD: _agent_cases._RESULT_FIELD, _agent_cases._RESULT_FIELD: "shipped"}),
        ]
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(stdout="\n".join(events)),
        ) as run_mock:
            agent_result = _runner.run_agent(
                _agent_cases._CLAUDE,
                _agent_cases._PROMPT,
                _agent_cases._CWD,
            )
            argv = list(run_mock.call_args.args[0])
        self.assertEqual(agent_result.session_id, _PARSED_SESSION_ID)
        self.assertEqual(agent_result.last_message, "shipped")
        self.assertEqual(argv[1], "-p")


class RunAgentOptionNormalizationTest(unittest.TestCase):
    """`run_agent` folds either option form into one `AgentRunOptions` before
    handing it to the selected backend as an explicit `options` keyword.
    """

    def test_legacy_keyword_fields_become_options(self) -> None:
        for backend, alias in _BACKEND_ALIASES:
            with self.subTest(backend=backend):
                fake = MagicMock()
                with patch.object(_agents, alias, fake):
                    _runner.run_agent(
                        backend,
                        _agent_cases._PROMPT,
                        _agent_cases._CWD,
                        resume_session_id=_KEPT_SESSION_ID,
                        extra_args=_EXTRA_ARGS,
                    )
                forwarded = fake.call_args.kwargs[_OPTIONS_KWARG]
                self.assertEqual(forwarded.resume_session_id, _KEPT_SESSION_ID)
                self.assertEqual(forwarded.extra_args, _EXTRA_ARGS)

    def test_explicit_options_pass_through(self) -> None:
        options = _models.AgentRunOptions(resume_session_id=_KEPT_SESSION_ID)
        for backend, alias in _BACKEND_ALIASES:
            with self.subTest(backend=backend):
                fake = MagicMock()
                with patch.object(_agents, alias, fake):
                    _runner.run_agent(
                        backend,
                        _agent_cases._PROMPT,
                        _agent_cases._CWD,
                        options=options,
                    )
                self.assertIs(fake.call_args.kwargs[_OPTIONS_KWARG], options)

    def test_mixing_forms_raises_type_error(self) -> None:
        # Mixing the explicit object with legacy keyword fields is rejected
        # rather than silently dropping one form.
        with self.assertRaises(TypeError):
            _runner.run_agent(
                _agent_cases._CODEX,
                _agent_cases._PROMPT,
                _agent_cases._CWD,
                options=_models.AgentRunOptions(),
                resume_session_id=_KEPT_SESSION_ID,
            )


class BuildAgentResultTest(unittest.TestCase):
    """Result assembly harvests the session id and carries the process streams."""

    def test_resume_session_id_wins_over_parsed(self) -> None:
        # A resumed run keeps its known session id even when fresh stdout also
        # carries one, so resuming never forks a new session.
        stdout = json.dumps({_agent_cases._SESSION_ID_FIELD: _PARSED_SESSION_ID})
        agent_result = _runner.build_agent_result(
            _models.AgentRunOptions(resume_session_id=_KEPT_SESSION_ID),
            self._process_result(stdout=stdout),
            _FINAL_MESSAGE,
        )
        self.assertEqual(agent_result.session_id, _KEPT_SESSION_ID)

    def test_fresh_run_parses_session_id_from_stdout(self) -> None:
        stdout = json.dumps({_agent_cases._SESSION_ID_FIELD: _PARSED_SESSION_ID})
        agent_result = _runner.build_agent_result(
            _models.AgentRunOptions(),
            self._process_result(stdout=stdout),
            _FINAL_MESSAGE,
        )
        self.assertEqual(agent_result.session_id, _PARSED_SESSION_ID)

    def test_carries_streams_and_leaves_usage_unset(self) -> None:
        # The captured streams and exit status copy across verbatim; usage is
        # populated later by the tracking layer, so a freshly built result
        # reads as usage-free.
        agent_result = _runner.build_agent_result(
            _models.AgentRunOptions(),
            self._process_result(stdout="out"),
            _FINAL_MESSAGE,
        )
        self.assertEqual(agent_result.last_message, _FINAL_MESSAGE)
        self.assertEqual(agent_result.stdout, "out")
        self.assertEqual(agent_result.stderr, "err")
        self.assertEqual(agent_result.exit_code, 0)
        self.assertFalse(agent_result.timed_out)
        self.assertIsNone(agent_result.usage)

    def _process_result(self, stdout: str = "") -> _models.SubprocessResult:
        return _models.SubprocessResult(
            stdout=stdout,
            stderr="err",
            exit_code=0,
            timed_out=False,
            interrupted=False,
        )


class LogAgentSpawnTest(unittest.TestCase):
    def test_logs_backend_without_prompt_or_env(self) -> None:
        # The spawn line records only backend, cwd, resume flag, and timeout;
        # the prompt and child environment never reach it.
        options = _models.AgentRunOptions(
            resume_session_id=_KEPT_SESSION_ID,
            timeout=_TIMEOUT_SECONDS,
        )
        with self.assertLogs(_LOGGER_NAME, level="INFO") as captured:
            _runner.log_agent_spawn(_agent_cases._CODEX, _agent_cases._CWD, options)
            spawn_logs = list(captured.records)
        self.assertEqual(len(spawn_logs), 1)
        message = spawn_logs[0].getMessage()
        self.assertIn("codex spawn", message)
        self.assertIn("resume=True", message)
        self.assertIn("timeout=42s", message)
        self.assertIn(str(_agent_cases._CWD), message)
