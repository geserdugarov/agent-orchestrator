# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agents import (
    _AGENT_PROVIDER_AUTH_ALLOWLIST,
    _AGENT_WRITE_CREDENTIAL_LOCATORS,
    AgentResult,
    _claude_last_message,
    _filter_agent_env,
    _is_secret_shaped,
    _run_claude,
    _run_codex,
    parse_session_id,
    run_agent,
)

agents = sys.modules["orchestrator.agents"]

_CWD = Path("/tmp/agent-orchestrator-test-cwd-doesnt-matter")
# A real directory for tests that spawn an actual subprocess (Popen rejects a
# non-existent cwd); the mock-Popen tests above never touch the filesystem.
_REAL_CWD = Path(tempfile.gettempdir())
_POPEN_TARGET = "orchestrator.agents.subprocess.Popen"
_OS_ENVIRON_TARGET = "os.environ"
_CODEX = "codex"
_CLAUDE = "claude"
_PROMPT = "p"
_CODEX_EXEC = "exec"
_MODEL_FLAG = "-m"
_CODEX_MODEL = "gpt-5.5"
_CONFIG_FLAG = "-c"
_PYTHON_COMMAND_FLAG = "-c"
_CLAUDE_MODEL_FLAG = "--model"
_CLAUDE_MODEL = "claude-opus-4-7"
_RESUME_FLAG = "--resume"
_PATH_ENV = "PATH"
_SYSTEM_PATH = "/usr/bin"
_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
_ENV_KWARG = "env"
_SUBPROCESS_TIMEOUT_SECONDS = 30
_TERMINATION_GRACE_SECONDS = 0.05
_KILLPG = "killpg"
_AGENT_COMMAND = "agent"
_PARTIAL_CLAUDE_OUTPUT = json.dumps({"type": "assistant", "message": {
    "content": [{"type": "text", "text": "partial work so far"}],
}})


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    # _run_subprocess uses Popen + communicate(timeout=...). The mock returns
    # (stdout, stderr) from communicate and exposes .returncode -- enough to
    # let tests assert on argv passed to Popen without spawning anything.
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    return proc


def _killpg_group_empty(pid: int, sig: int) -> None:
    """`os.killpg` side_effect where the signal-0 liveness probe reports the
    group already empty, so the SIGKILL escalation must be skipped.
    """
    if sig == 0:
        raise ProcessLookupError


def _killpg_group_alive(pid: int, sig: int) -> None:
    """`os.killpg` side_effect where the signal-0 liveness probe succeeds, so
    a descendant outlived the leader and the group must still be SIGKILLed.
    """


@contextlib.contextmanager
def _registered_procs(*processes: object):
    with contextlib.ExitStack() as cleanup:
        for process in processes:
            agents._register_proc(process)
            cleanup.callback(agents._unregister_proc, process)
        yield


def _stop_process_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


class _RegistrationProbe:
    def __init__(self, process: object) -> None:
        self.process = process
        self.seen = False

    def __call__(self, *unused_args, **unused_kwargs) -> tuple[str, str]:
        with agents._running_procs_lock:
            self.seen = self.process in agents._running_procs
        return "{}", ""


class ParseSessionIdTest(unittest.TestCase):
    def test_codex_jsonl_session_id(self) -> None:
        # Codex's --json output has session_id at varied paths; the walker
        # picks any UUID at a known key, anywhere in the tree.
        line = json.dumps({
            "type": "task_started",
            "session_id": "11111111-2222-3333-4444-555555555555",
        })
        self.assertEqual(
            parse_session_id(line),
            "11111111-2222-3333-4444-555555555555",
        )

    def test_claude_stream_json_session_id(self) -> None:
        # Claude's stream-json puts session_id on the system/init event and
        # on most subsequent events; a top-level UUID at session_id is the
        # documented surface.
        events = [
            json.dumps({
                "type": "system",
                "subtype": "init",
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "tools": [],
            }),
            json.dumps({
                "type": "assistant",
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "message": {"role": "assistant", "content": []},
            }),
        ]
        self.assertEqual(
            parse_session_id("\n".join(events)),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

    def test_no_uuid_returns_none(self) -> None:
        self.assertIsNone(parse_session_id('{"type":"banner","msg":"hello"}'))

    def test_skips_unparseable_lines(self) -> None:
        out = (
            "not-json\n"
            + json.dumps({"session_id": "12341234-1234-1234-1234-123412341234"})
        )
        self.assertEqual(
            parse_session_id(out),
            "12341234-1234-1234-1234-123412341234",
        )


class ClaudeLastMessageTest(unittest.TestCase):
    def test_prefers_terminal_result_event(self) -> None:
        events = [
            json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "thinking..."}],
            }}),
            json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "final answer",
            }),
        ]
        self.assertEqual(_claude_last_message("\n".join(events)), "final answer")

    def test_falls_back_to_supported_message_shapes(self) -> None:
        cases = (
            (
                [{"type": "assistant", "message": {
                    "content": [
                        {"type": "text", "text": "hello "},
                        {"type": "text", "text": "world"},
                    ],
                }}],
                "hello world",
            ),
            ([{"type": "message", "content": "direct message"}], "direct message"),
        )
        for event_payloads, expected in cases:
            with self.subTest(expected=expected):
                events = [json.dumps(payload) for payload in event_payloads]
                self.assertEqual(
                    _claude_last_message("\n".join(events)),
                    expected,
                )

    def test_ignores_diagnostics_and_bad_blocks(self) -> None:
        events = [
            "diagnostic text outside the JSON stream",
            json.dumps(["not", "an", "event"]),
            json.dumps({"type": "system", "content": "not an answer"}),
            json.dumps({"type": "assistant", "message": {
                "content": [
                    {"type": "tool_use", "text": "ignored tool"},
                    {"type": "text", "text": 7},
                    "invalid block",
                    {"type": "text", "text": "kept answer"},
                ],
            }}),
        ]
        self.assertEqual(_claude_last_message("\n".join(events)), "kept answer")

    def test_keeps_last_string_result_for_error_event(self) -> None:
        events = [
            json.dumps({"type": "result", "result": "earlier result"}),
            json.dumps({
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "error details",
            }),
            json.dumps({"type": "result", "result": {"invalid": "shape"}}),
        ]
        self.assertEqual(_claude_last_message("\n".join(events)), "error details")

    def test_empty_without_known_events(self) -> None:
        self.assertEqual(_claude_last_message(""), "")
        self.assertEqual(
            _claude_last_message('{"type":"system","subtype":"init"}'),
            "",
        )


class RunAgentDispatchTest(unittest.TestCase):
    def test_unknown_backend_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "gemini"):
            run_agent("gemini", "prompt", _CWD)

    def test_dispatches_to_codex(self) -> None:
        # Use stream-json-shaped output so parse_session_id has something to
        # find; the codex runner doesn't care about claude shape.
        sid = "abcdef12-3456-7890-abcd-ef1234567890"
        with patch(
            _POPEN_TARGET,
            return_value=_completed(stdout=json.dumps({"session_id": sid})),
        ) as run_mock:
            agent_result = run_agent(_CODEX, _PROMPT, _CWD)
            argv = list(run_mock.call_args.args[0])
        self.assertEqual(agent_result.session_id, sid)
        self.assertEqual(agent_result.exit_code, 0)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[1], _CODEX_EXEC)

    def test_dispatches_to_claude(self) -> None:
        sid = "cafe1234-5678-90ab-cdef-1234567890ab"
        events = [
            json.dumps({"type": "system", "session_id": sid}),
            json.dumps({"type": "result", "result": "shipped"}),
        ]
        with patch(
            _POPEN_TARGET,
            return_value=_completed(stdout="\n".join(events)),
        ) as run_mock:
            agent_result = run_agent(_CLAUDE, _PROMPT, _CWD)
            argv = list(run_mock.call_args.args[0])
        self.assertEqual(agent_result.session_id, sid)
        self.assertEqual(agent_result.last_message, "shipped")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)


class RunCodexEnvScrubTest(unittest.TestCase):
    def test_github_credentials_are_stripped(self) -> None:
        # The agent must never see GITHUB_TOKEN (or any synonym); the
        # orchestrator owns all GitHub writes. Provider auth keys
        # (ANTHROPIC_API_KEY, OPENAI_*) must NOT be stripped -- those are how
        # the agent talks to its own model.
        env = {
            "GITHUB_TOKEN": "ghp_secret",
            "GH_TOKEN": "ghp_alt",
            _ANTHROPIC_API_KEY: "sk-keep-me",
            _PATH_ENV: _SYSTEM_PATH,
        }
        with patch.dict(_OS_ENVIRON_TARGET, env, clear=True), patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            _run_codex(_PROMPT, _CWD)
            passed_env = dict(run_mock.call_args.kwargs[_ENV_KWARG])
        self.assertNotIn("GITHUB_TOKEN", passed_env)
        self.assertNotIn("GH_TOKEN", passed_env)
        self.assertEqual(passed_env.get(_ANTHROPIC_API_KEY), "sk-keep-me")

    def test_production_secret_shapes_are_stripped(self) -> None:
        # Issue #213: extend the env boundary so common production-secret-
        # shaped variables don't ride into the agent subprocess. The
        # filter is shape-based (suffix + bare name) so it covers the
        # long tail without enumerating every provider.
        env = {
            "STRIPE_API_KEY": "sk_live_stripe",
            "DATABASE_PASSWORD": "hunter2",
            "AWS_SECRET_ACCESS_KEY": "deadbeef",
            "DEPLOY_TOKEN": "deploy-tok",
            "MY_CREDENTIAL": "mycred",
            "PAGERDUTY_PAT": "pd-pat-value",
            "VAULT_SECRET": "vault-val",
            # Lowercased should also be caught (case-insensitive).
            "database_password": "lowercase-pw",
            # Bare names (some build systems still set these unprefixed).
            "TOKEN": "bare-token",
            "PASSWORD": "bare-password",
            # Non-secret vars must pass through unchanged.
            _PATH_ENV: _SYSTEM_PATH,
            "BUILD_NUMBER": "42",
            # Provider auth: must NOT be stripped.
            _ANTHROPIC_API_KEY: "sk-keep-anthropic",
            "OPENAI_API_KEY": "sk-keep-openai",
        }
        with patch.dict(_OS_ENVIRON_TARGET, env, clear=True), patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            _run_codex(_PROMPT, _CWD)
            passed_env = dict(run_mock.call_args.kwargs[_ENV_KWARG])
        for stripped in (
            "STRIPE_API_KEY", "DATABASE_PASSWORD", "AWS_SECRET_ACCESS_KEY",
            "DEPLOY_TOKEN", "MY_CREDENTIAL", "PAGERDUTY_PAT", "VAULT_SECRET",
            "database_password", "TOKEN", "PASSWORD",
        ):
            self.assertNotIn(stripped, passed_env)
        # Non-secret vars survive.
        self.assertEqual(passed_env.get(_PATH_ENV), _SYSTEM_PATH)
        self.assertEqual(passed_env.get("BUILD_NUMBER"), "42")
        # Provider auth survives.
        self.assertEqual(
            passed_env.get(_ANTHROPIC_API_KEY), "sk-keep-anthropic",
        )
        self.assertEqual(passed_env.get("OPENAI_API_KEY"), "sk-keep-openai")

    def test_write_credential_locators_are_stripped(self) -> None:
        # Issue #213 review: write-credential pointers that aren't
        # secret-shaped but let an agent subprocess use the operator's
        # loaded ssh-agent / askpass binary / custom SSH wrapper to
        # push or authenticate as them. Stripping by exact name closes
        # this "no write credentials" gap.
        env = {
            "SSH_AUTH_SOCK": "/tmp/ssh-XXXX/agent.42",
            "SSH_ASKPASS": "/usr/lib/ssh/ssh-askpass",
            "GIT_ASKPASS": "/usr/share/git/askpass-helper",
            "GIT_SSH_COMMAND": "ssh -i ~/.ssh/deploy-key",
            _PATH_ENV: _SYSTEM_PATH,
        }
        with patch.dict(_OS_ENVIRON_TARGET, env, clear=True), patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            _run_codex(_PROMPT, _CWD)
            passed_env = dict(run_mock.call_args.kwargs[_ENV_KWARG])
        for stripped in _AGENT_WRITE_CREDENTIAL_LOCATORS:
            self.assertNotIn(
                stripped, passed_env,
                f"{stripped} must be stripped from the agent env",
            )
        self.assertEqual(passed_env.get(_PATH_ENV), _SYSTEM_PATH)

    def test_credential_file_locators_are_stripped(self) -> None:
        # Credential-file locators -- the env value is a filesystem path
        # the subprocess can open as the same user, not the secret
        # itself. Stripping the locator removes the trivial "follow the
        # pointer" exfiltration path. `ORCHESTRATOR_TOKEN_FILE` is the
        # orchestrator's OWN write-credential locator, often pointing at
        # a non-default path in multi-repo deployments -- the agent must
        # not see it.
        env = {
            "ORCHESTRATOR_TOKEN_FILE": "/etc/secrets/orch-token",
            "GOOGLE_APPLICATION_CREDENTIALS": "/etc/secrets/gcp.json",
            "AWS_SHARED_CREDENTIALS_FILE": "/etc/secrets/aws-creds",
            "MY_DB_PASSWORD_FILE": "/etc/secrets/db.pw",
            "TLS_KEY_FILE": "/etc/secrets/tls.key",
            "VAULT_SECRET_FILE": "/etc/secrets/vault",
            "AZURE_CREDENTIALS": "/etc/secrets/azure.json",
            # Bare-name credentials locator some tools accept.
            "CREDENTIALS": "/etc/secrets/creds",
            "TOKEN_FILE": "/etc/secrets/tok",
            # Non-credential path must pass through unchanged.
            "TMPDIR": "/tmp",
            "MY_CONFIG_FILE": "/etc/myapp/config.yaml",
        }
        with patch.dict(_OS_ENVIRON_TARGET, env, clear=True), patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            _run_codex(_PROMPT, _CWD)
            passed_env = dict(run_mock.call_args.kwargs[_ENV_KWARG])
        for stripped in (
            "ORCHESTRATOR_TOKEN_FILE",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AWS_SHARED_CREDENTIALS_FILE",
            "MY_DB_PASSWORD_FILE",
            "TLS_KEY_FILE",
            "VAULT_SECRET_FILE",
            "AZURE_CREDENTIALS",
            "CREDENTIALS",
            "TOKEN_FILE",
        ):
            self.assertNotIn(stripped, passed_env)
        # Non-credential file paths survive.
        self.assertEqual(passed_env.get("TMPDIR"), "/tmp")
        self.assertEqual(passed_env.get("MY_CONFIG_FILE"), "/etc/myapp/config.yaml")


class FilterAgentEnvTest(unittest.TestCase):
    """Unit-level coverage for the shared `_filter_agent_env` helper.

    The helper is the single boundary both agent subprocesses and the
    verify runner share, so its behavior is exercised in isolation here
    (no Popen spawn) for the edge cases the integration tests don't
    explicitly enumerate.
    """

    def test_drops_github_aliases_via_exact_match(self) -> None:
        # The GitHub-token alias list contains entries that don't match
        # the secret-shape suffix (e.g. `GH_HOST`); they must still be
        # stripped via `_FORBIDDEN_AGENT_ENV`.
        env = {"GH_HOST": "github.example.com", _PATH_ENV: _SYSTEM_PATH}
        filtered_env = _filter_agent_env(env)
        self.assertNotIn("GH_HOST", filtered_env)
        self.assertEqual(filtered_env.get(_PATH_ENV), _SYSTEM_PATH)

    def test_write_locators_stripped_in_both_modes(self) -> None:
        # `_AGENT_WRITE_CREDENTIAL_LOCATORS` is stripped regardless of
        # the `allow_provider_auth` flag -- the verify path (False) and
        # the agent path (True) must both refuse to forward SSH agent /
        # askpass / GIT_SSH_COMMAND.
        env = {name: "value" for name in _AGENT_WRITE_CREDENTIAL_LOCATORS}
        for allow in (True, False):
            filtered_env = _filter_agent_env(env, allow_provider_auth=allow)
            for name in _AGENT_WRITE_CREDENTIAL_LOCATORS:
                self.assertNotIn(
                    name, filtered_env,
                    f"{name} must be stripped (allow_provider_auth={allow})",
                )

    def test_allowlist_preserves_provider_auth(self) -> None:
        # Every name in the provider-auth allowlist must survive the
        # shape filter; the agent CLI uses these to talk to its own
        # model and stripping them breaks the run.
        env = {name: "value-long-enough" for name in _AGENT_PROVIDER_AUTH_ALLOWLIST}
        filtered_env = _filter_agent_env(env)
        for name in _AGENT_PROVIDER_AUTH_ALLOWLIST:
            self.assertEqual(filtered_env.get(name), "value-long-enough")

    def test_provider_auth_block_strips_keys(self) -> None:
        # Verify-command path passes `allow_provider_auth=False` so the
        # agent's own provider keys are also stripped. A hostile
        # dependency executed under the verify shell would otherwise
        # gain billable access to the operator's model account.
        env = {name: "value-long-enough" for name in _AGENT_PROVIDER_AUTH_ALLOWLIST}
        env[_PATH_ENV] = _SYSTEM_PATH
        filtered_env = _filter_agent_env(env, allow_provider_auth=False)
        for name in _AGENT_PROVIDER_AUTH_ALLOWLIST:
            self.assertNotIn(
                name, filtered_env,
                f"{name} must be stripped when allow_provider_auth=False",
            )
        # Non-secret entries still survive.
        self.assertEqual(filtered_env.get(_PATH_ENV), _SYSTEM_PATH)

    def test_secret_shape_predicate(self) -> None:
        # Direct check on the predicate so the contract is documented
        # independent of any caller. Suffix matches and bare names hit;
        # provider-shaped allowlisted names also hit the predicate (the
        # allowlist runs above it in `_filter_agent_env`).
        for name in (
            "FOO_TOKEN", "BAR_KEY", "BAZ_SECRET", "QUX_PASSWORD",
            "PD_PAT", "MY_CREDENTIAL", "TOKEN", "PASSWORD",
            "ANTHROPIC_API_KEY", "stripe_api_key",
            # Credential-file locator shapes (issue #213 review).
            "ORCHESTRATOR_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
            "AWS_SHARED_CREDENTIALS_FILE", "MY_DB_PASSWORD_FILE",
            "TLS_KEY_FILE", "VAULT_SECRET_FILE", "AZURE_CREDENTIALS",
            "CREDENTIALS", "TOKEN_FILE", "CREDENTIALS_FILE",
        ):
            self.assertTrue(
                _is_secret_shaped(name), f"{name} should look secret-shaped"
            )
        for name in (
            _PATH_ENV, "HOME", "BUILD_NUMBER", "CI", "USER",
            # Plain config-file locators (non-credential) must not match.
            "MY_CONFIG_FILE", "PROFILE_FILE",
        ):
            self.assertFalse(
                _is_secret_shaped(name), f"{name} should not look secret-shaped"
            )

    def test_empty_env_passthrough(self) -> None:
        self.assertEqual(_filter_agent_env({}), {})


class RunCodexCwdTest(unittest.TestCase):
    def test_dash_C_gets_full_path_for_relative_cwd(self) -> None:
        # codex applies `-C` AFTER it has already chdir'd into the subprocess
        # cwd, so a relative path resolves twice and codex hits "No such file
        # or directory (os error 2)". Pinning this guarantees the path passed
        # to `-C` is absolute even when WORKTREES_DIR (and the worktree path
        # derived from it) is relative.
        rel_cwd = Path("../wt-orchestrator/foo/issue-1")
        with patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            _run_codex(_PROMPT, rel_cwd)
            argv = list(run_mock.call_args.args[0])
        c_value = argv[argv.index("-C") + 1]
        self.assertTrue(
            Path(c_value).is_absolute(),
            f"-C path should be absolute, got {c_value!r}",
        )
        self.assertEqual(Path(c_value), rel_cwd.resolve())


class RunClaudeResumeTest(unittest.TestCase):
    def test_resume_passes_resume_session_id_arg(self) -> None:
        sid = "deadbeef-1234-1234-1234-1234deadbeef"
        with patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            _run_claude("followup", _CWD, resume_session_id=sid)
            argv = list(run_mock.call_args.args[0])
        self.assertIn(_RESUME_FLAG, argv)
        self.assertEqual(argv[argv.index(_RESUME_FLAG) + 1], sid)


class RunAgentExtraArgsTest(unittest.TestCase):
    """`extra_args` lets a role-specific config inject backend-CLI flags
    (e.g. `-m gpt-5.5` for codex, `--model X --effort high` for claude)
    into the spawned argv on both fresh and resumed runs while keeping the
    safety/output flags and prompt where they already are.
    """

    def test_codex_fresh_adds_args_before_exec(self) -> None:
        # Codex global options (`-m`, `-c`) must appear BEFORE the `exec`
        # subcommand; the parser rejects them after the subcommand. The
        # safety/output flags and prompt must remain on the argv tail.
        argv = self._argv_for(
            _CODEX,
            extra_args=(
                _MODEL_FLAG,
                _CODEX_MODEL,
                _CONFIG_FLAG,
                'model_reasoning_effort="xhigh"',
            ),
        )
        self.assertEqual(argv[1:5], [
            _MODEL_FLAG,
            _CODEX_MODEL,
            _CONFIG_FLAG,
            'model_reasoning_effort="xhigh"',
        ])
        self.assertEqual(argv[5], _CODEX_EXEC)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], _PROMPT)

    def test_codex_resume_adds_args_before_exec(self) -> None:
        sid = "11111111-2222-3333-4444-555555555555"
        argv = self._argv_for(
            _CODEX,
            extra_args=(_MODEL_FLAG, _CODEX_MODEL),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], [_MODEL_FLAG, _CODEX_MODEL])
        self.assertEqual(argv[3:5], [_CODEX_EXEC, "resume"])
        # Resume session id and prompt are still the last two tokens; the
        # extra args must NOT have displaced them.
        self.assertEqual(argv[-2:], [sid, _PROMPT])

    def test_claude_fresh_adds_args_before_safety(self) -> None:
        argv = self._argv_for(
            _CLAUDE,
            extra_args=(_CLAUDE_MODEL_FLAG, _CLAUDE_MODEL, "--effort", "high"),
        )
        self.assertEqual(argv[1:5], [
            _CLAUDE_MODEL_FLAG, _CLAUDE_MODEL, "--effort", "high",
        ])
        # Safety + output flags survive immediately after the extra args.
        self.assertEqual(argv[5], "-p")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[-1], _PROMPT)

    def test_claude_resume_keeps_args_and_flag(self) -> None:
        sid = "deadbeef-1234-1234-1234-1234deadbeef"
        argv = self._argv_for(
            _CLAUDE,
            extra_args=(_CLAUDE_MODEL_FLAG, _CLAUDE_MODEL),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], [_CLAUDE_MODEL_FLAG, _CLAUDE_MODEL])
        # `--resume <sid>` is appended after the safety flags and right
        # before the prompt, regardless of extra_args.
        self.assertIn(_RESUME_FLAG, argv)
        self.assertEqual(argv[argv.index(_RESUME_FLAG) + 1], sid)
        self.assertEqual(argv[-1], _PROMPT)

    def test_empty_default_keeps_argv_unchanged(self) -> None:
        # Backward compat: callers that don't pass `extra_args` still get
        # the legacy argv with no inserted tokens. Sanity-checks both
        # backends so a future refactor that changes argv shape under
        # default callers fails this test loudly.
        codex_argv = self._argv_for(_CODEX, extra_args=())
        self.assertEqual(codex_argv[1], _CODEX_EXEC)
        claude_argv = self._argv_for(_CLAUDE, extra_args=())
        self.assertEqual(claude_argv[1], "-p")

    def _argv_for(
        self,
        backend: str,
        *,
        extra_args: tuple[str, ...],
        resume_session_id=None,
    ) -> list[str]:
        with patch(
            _POPEN_TARGET,
            return_value=_completed(),
        ) as run_mock:
            run_agent(
                backend, _PROMPT, _CWD,
                resume_session_id=resume_session_id,
                extra_args=extra_args,
            )
            return list(run_mock.call_args.args[0])


class TerminateAllRunningTest(unittest.TestCase):
    """`terminate_all_running` is the shutdown hook that kills in-flight agent
    process groups so a restart does not hang for up to `AGENT_TIMEOUT`. It
    must SIGTERM every registered group, SIGKILL anything still alive at the
    shared grace deadline, and be a clean no-op when nothing is in flight.
    """

    def test_no_procs_is_noop(self) -> None:
        # Registry empty between tests (every spawn unregisters in a finally),
        # so this exercises the early return with no signals sent.
        with patch.object(agents.os, _KILLPG) as killpg:
            self.assertEqual(agents.terminate_all_running(), 0)
            killpg.assert_not_called()

    def test_no_sigkill_after_all_groups_exit(self) -> None:
        # Both leaders exit on SIGTERM and the signal-0 group probe reports the
        # group empty, so no SIGKILL is sent -- the clean-shutdown path.
        proc1, proc2 = MagicMock(), MagicMock()
        proc1.pid = 111
        proc2.pid = 222
        proc1.wait.return_value = 0
        proc2.wait.return_value = 0
        with _registered_procs(proc1, proc2):
            with patch.object(
                agents.os, _KILLPG, side_effect=_killpg_group_empty,
            ) as signal_mock:
                terminated_count = agents.terminate_all_running(grace=0.5)
                sent = {call.args for call in signal_mock.call_args_list}
        self.assertEqual(terminated_count, 2)
        self.assertIn((111, signal.SIGTERM), sent)
        self.assertIn((222, signal.SIGTERM), sent)
        self.assertNotIn((111, signal.SIGKILL), sent)
        self.assertNotIn((222, signal.SIGKILL), sent)

    def test_sigkill_if_child_outlives_leader(self) -> None:
        # Regression: the leader exits on SIGTERM but a descendant in the same
        # group ignored it. `proc.wait()` returns, yet the signal-0 probe shows
        # the group still alive, so the group must be SIGKILLed -- otherwise the
        # grandchild keeps mutating the worktree after the orchestrator exits.
        proc = MagicMock()
        proc.pid = 555
        proc.wait.return_value = 0  # leader exits promptly on SIGTERM
        with _registered_procs(proc):
            with patch.object(
                agents.os, _KILLPG, side_effect=_killpg_group_alive,
            ) as signal_mock:
                agents.terminate_all_running(grace=_TERMINATION_GRACE_SECONDS)
                sent = [call.args for call in signal_mock.call_args_list]
        self.assertIn((555, signal.SIGTERM), sent)
        self.assertIn((555, 0), sent)  # group liveness probed after leader exit
        self.assertIn((555, signal.SIGKILL), sent)

    def test_sigkills_straggler_past_deadline(self) -> None:
        # A group that never exits on SIGTERM must be SIGKILLed once the
        # shared grace deadline elapses.
        proc = MagicMock()
        proc.pid = 333
        proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd=_AGENT_COMMAND, timeout=_TERMINATION_GRACE_SECONDS,
        )
        with _registered_procs(proc):
            with patch.object(agents.os, _KILLPG) as killpg:
                agents.terminate_all_running(grace=_TERMINATION_GRACE_SECONDS)
                calls = [call.args for call in killpg.call_args_list]
        self.assertIn((333, signal.SIGTERM), calls)
        self.assertIn((333, signal.SIGKILL), calls)

    def test_missing_group_is_swallowed(self) -> None:
        # The leader can exit between the snapshot and the killpg; the
        # ProcessLookupError race must not propagate.
        proc = MagicMock()
        proc.pid = 444
        proc.wait.return_value = 0
        with _registered_procs(proc):
            with patch.object(
                agents.os, _KILLPG, side_effect=ProcessLookupError,
            ):
                self.assertEqual(
                    agents.terminate_all_running(
                        grace=_TERMINATION_GRACE_SECONDS,
                    ),
                    1,
                )

    def test_process_group_alive_real_process(self) -> None:
        # The mock tests can't exercise the actual `killpg(_, 0)` probe the
        # SIGKILL decision now relies on, so drive a real process group:
        # alive while the leader runs, empty once it is killed and reaped.
        proc = subprocess.Popen(
            [sys.executable, _PYTHON_COMMAND_FLAG, "import time; time.sleep(120)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(_stop_process_group, proc)
            self.assertTrue(agents._process_group_alive(proc.pid))
        self.assertFalse(agents._process_group_alive(proc.pid))


class TerminateProcessGroupTest(unittest.TestCase):
    """`_terminate_process_group` is the per-timeout cleanup. It must mirror
    `terminate_all_running`'s safety model: after the leader exits it probes
    the group with `killpg(_, 0)` and SIGKILLs any surviving descendant, so a
    build grandchild the agent forked cannot keep mutating the worktree after
    the timeout has already been recorded.
    """

    def test_sigkill_if_child_outlives_leader(self) -> None:
        # The leader exits on SIGTERM but a descendant in the same group
        # ignored it. `proc.wait()` returns, yet the signal-0 probe shows the
        # group still alive, so the group must be SIGKILLed.
        proc = MagicMock()
        proc.pid = 777
        proc.wait.return_value = 0  # leader exits promptly on SIGTERM

        with patch.object(
            agents.os, _KILLPG, side_effect=_killpg_group_alive,
        ) as signal_mock:
            agents._terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertIn((777, signal.SIGTERM), sent)
        self.assertIn((777, 0), sent)  # group liveness probed after leader exit
        self.assertIn((777, signal.SIGKILL), sent)

    def test_no_sigkill_when_group_fully_exited(self) -> None:
        # Leader exits and the signal-0 probe reports the group empty, so no
        # SIGKILL is sent -- the clean path.
        proc = MagicMock()
        proc.pid = 778
        proc.wait.return_value = 0

        with patch.object(
            agents.os, _KILLPG, side_effect=_killpg_group_empty,
        ) as signal_mock:
            agents._terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertIn((778, signal.SIGTERM), sent)
        self.assertIn((778, 0), sent)
        self.assertNotIn((778, signal.SIGKILL), sent)

    def test_sigkills_straggler_past_deadline(self) -> None:
        # The leader never exits on SIGTERM; once the grace `wait` times out
        # the group is SIGKILLed without a probe (a live leader means a live
        # group).
        proc = MagicMock()
        proc.pid = 779
        proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd=_AGENT_COMMAND, timeout=5,
        )
        with patch.object(agents.os, _KILLPG) as killpg:
            agents._terminate_process_group(proc)
            calls = [call.args for call in killpg.call_args_list]
        self.assertIn((779, signal.SIGTERM), calls)
        self.assertIn((779, signal.SIGKILL), calls)
        self.assertNotIn((779, 0), calls)  # no probe when the leader is alive

    def test_first_sigterm_lookup_needs_no_kill(self) -> None:
        # The group already exited between the timeout firing and the killpg;
        # the ProcessLookupError race short-circuits before any wait/SIGKILL.
        proc = MagicMock()
        proc.pid = 780
        with patch.object(
            agents.os, _KILLPG, side_effect=ProcessLookupError,
        ) as signal_mock:
            agents._terminate_process_group(proc)
            sent = [call.args for call in signal_mock.call_args_list]
        self.assertEqual(
            sent,
            [(780, signal.SIGTERM)],
        )
        proc.wait.assert_not_called()


class RunSubprocessRegistrationTest(unittest.TestCase):
    """`_run_subprocess` must register its child for the lifetime of the run
    so the shutdown sweep can reach it, and clear it afterward so the registry
    does not leak completed processes.
    """

    def test_registers_during_run_and_clears_after(self) -> None:
        proc = _completed(stdout="{}", returncode=0)
        registration_probe = _RegistrationProbe(proc)
        proc.communicate.side_effect = registration_probe
        with patch(_POPEN_TARGET, return_value=proc):
            agents._run_subprocess([_AGENT_COMMAND], _CWD, {}, 10)

        self.assertTrue(registration_probe.seen, "child not registered during the run")
        with agents._running_procs_lock:
            self.assertNotIn(proc, agents._running_procs)


class CommunicateBoundedTest(unittest.TestCase):
    """`_communicate_bounded` is the shared drain primitive both the agent
    runner and the verify runner call. Its contract: return the captured
    streams (coercing an absent stream to ``""``) on completion, and ``None``
    when the drain itself blocks past the cap so the caller can escalate.
    """

    def test_returns_streams_coercing_absent_to_empty(self) -> None:
        proc = MagicMock()
        proc.communicate.return_value = (None, None)
        self.assertEqual(agents._communicate_bounded(proc, 5), ("", ""))

    def test_returns_none_on_timeout(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=_AGENT_COMMAND, timeout=5,
        )
        self.assertIsNone(agents._communicate_bounded(proc, 5))


class InterruptedClassificationTest(unittest.TestCase):
    """A run cut short by SIGTERM/SIGKILL -- the shape the orchestrator's
    shutdown sweep (`terminate_all_running`) produces when it kills an
    in-flight agent group -- must surface as `interrupted=True`, distinct from
    a normal completion and from the orchestrator's own `timed_out` path.
    """

    def test_signal_exit_marked_interrupted(self) -> None:
        # Both shutdown-sweep signals produce a completed-but-interrupted run:
        # negative returncode, `interrupted=True`, and `timed_out=False`.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                *_, exit_code, timed_out, interrupted = self._kill_self(sig)
                self.assertEqual(exit_code, -sig)
                self.assertFalse(timed_out)
                self.assertTrue(interrupted)

    def test_clean_exit_not_interrupted(self) -> None:
        # A normal non-zero failure (exit 3) is a completed run, NOT an
        # interruption -- the two must stay distinguishable downstream.
        cmd = [sys.executable, _PYTHON_COMMAND_FLAG, "import sys; sys.exit(3)"]
        *_, exit_code, timed_out, interrupted = agents._run_subprocess(
            cmd, _REAL_CWD, dict(os.environ), _SUBPROCESS_TIMEOUT_SECONDS,
        )
        self.assertEqual(exit_code, 3)
        self.assertFalse(timed_out)
        self.assertFalse(interrupted)

    def test_own_timeout_is_timed_out(self) -> None:
        # A child that outlives our own `timeout` drives the timeout branch:
        # `_terminate_process_group` reaps the group and the run is classified
        # `timed_out=True`, `interrupted=False`, exit_code=-1 -- distinct from
        # the shutdown-sweep interruption above even though both signal the
        # group. Real child + 1s timeout so the whole flatten path is exercised.
        cmd = [sys.executable, _PYTHON_COMMAND_FLAG, "import time; time.sleep(30)"]
        *_, exit_code, timed_out, interrupted = agents._run_subprocess(
            cmd, _REAL_CWD, dict(os.environ), 1
        )
        self.assertEqual(exit_code, -1)
        self.assertTrue(timed_out)
        self.assertFalse(interrupted)

    def test_run_codex_threads_interrupted_through(self) -> None:
        with patch(
            _POPEN_TARGET,
            return_value=_completed(returncode=-signal.SIGTERM),
        ):
            agent_result = _run_codex(_PROMPT, _CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertFalse(agent_result.timed_out)
        self.assertEqual(agent_result.exit_code, -signal.SIGTERM)

    def test_run_claude_threads_interrupted_through(self) -> None:
        with patch(
            _POPEN_TARGET,
            return_value=_completed(returncode=-signal.SIGKILL),
        ):
            agent_result = _run_claude(_PROMPT, _CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertFalse(agent_result.timed_out)

    def test_clean_run_reports_not_interrupted(self) -> None:
        with patch(
            _POPEN_TARGET,
            return_value=_completed(returncode=0),
        ):
            agent_result = run_agent(_CODEX, _PROMPT, _CWD)
        self.assertFalse(agent_result.interrupted)

    def test_agent_result_interrupted_defaults_false(self) -> None:
        # Backwards-compat: existing positional/keyword constructions that omit
        # the new field still build and read `interrupted` as False.
        agent_result = AgentResult(
            session_id=None,
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout="",
            stderr="",
        )
        self.assertFalse(agent_result.interrupted)

    def _kill_self(self, sig: signal.Signals) -> tuple[str, str, int, bool, bool]:
        # Drive a REAL child that signals itself, so the negative returncode is
        # produced by the kernel + Popen exactly as it is when the shutdown
        # sweep SIGTERMs/SIGKILLs the group, not synthesized by a mock.
        cmd = [
            sys.executable, _PYTHON_COMMAND_FLAG,
            f"import os, signal; os.kill(os.getpid(), {int(sig)})",
        ]
        return agents._run_subprocess(
            cmd,
            _REAL_CWD,
            dict(os.environ),
            _SUBPROCESS_TIMEOUT_SECONDS,
        )


class ClaudeLastMessageGatingTest(unittest.TestCase):
    """The assistant/message fallback is a forward-compat crutch for clean
    runs only. An interrupted or non-zero claude run with no terminal
    `result` event must expose an empty `last_message` rather than treating
    the last streamed chunk as the agent's considered final answer.
    """

    def test_fallback_gated_off_directly(self) -> None:
        # With the fallback disabled, a transcript carrying only assistant
        # chunks yields ""; a terminal result event is still honored.
        self.assertEqual(
            _claude_last_message(
                _PARTIAL_CLAUDE_OUTPUT,
                allow_assistant_fallback=False,
            ),
            "",
        )
        result_frame = json.dumps({"type": "result", "result": "final"})
        with_result = f"{_PARTIAL_CLAUDE_OUTPUT}\n{result_frame}"
        self.assertEqual(
            _claude_last_message(with_result, allow_assistant_fallback=False),
            "final",
        )

    def test_interrupted_no_result_is_empty(self) -> None:
        with patch(
            _POPEN_TARGET,
            return_value=_completed(
                stdout=_PARTIAL_CLAUDE_OUTPUT, returncode=-signal.SIGTERM,
            ),
        ):
            agent_result = _run_claude(_PROMPT, _CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "")

    def test_nonzero_no_result_is_empty(self) -> None:
        with patch(
            _POPEN_TARGET,
            return_value=_completed(stdout=_PARTIAL_CLAUDE_OUTPUT, returncode=1),
        ):
            agent_result = _run_claude(_PROMPT, _CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.exit_code, 1)
        self.assertEqual(agent_result.last_message, "")

    def test_interrupted_result_is_kept(self) -> None:
        # A run that emitted the terminal result before being killed still
        # surfaces that result -- the gate only suppresses the partial-chunk
        # fallback, never the documented final-message channel.
        result_frame = json.dumps(
            {"type": "result", "result": "done before kill"},
        )
        out = f"{_PARTIAL_CLAUDE_OUTPUT}\n{result_frame}"
        with patch(
            _POPEN_TARGET,
            return_value=_completed(stdout=out, returncode=-signal.SIGKILL),
        ):
            agent_result = _run_claude(_PROMPT, _CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "done before kill")

    def test_clean_run_still_uses_assistant_fallback(self) -> None:
        # The clean-completion path keeps the forward-compat fallback so a
        # schema drift that drops the result event does not silently blank the
        # final message on a successful run.
        with patch(
            _POPEN_TARGET,
            return_value=_completed(stdout=_PARTIAL_CLAUDE_OUTPUT, returncode=0),
        ):
            agent_result = _run_claude(_PROMPT, _CWD)
        self.assertFalse(agent_result.interrupted)
        self.assertEqual(agent_result.last_message, "partial work so far")


if __name__ == "__main__":
    unittest.main()
