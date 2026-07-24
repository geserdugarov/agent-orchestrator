# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex backend command, environment, resume, interruption, last-message."""

from __future__ import annotations

import signal
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import agents as _agents
from orchestrator.agents import models as _models
from orchestrator.agents.backends import codex as _codex
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases

_LAST_MESSAGE_PATH = Path("/tmp/codex-last-message-doesnt-matter.txt")


def _codex_argv(
    *,
    cwd: Path = _agent_cases._CWD,
    extra_args: tuple[str, ...] = (),
    resume_session_id: str | None = None,
) -> list[str]:
    return _codex.codex_command(
        _agent_cases._PROMPT,
        cwd,
        _LAST_MESSAGE_PATH,
        _models.AgentRunOptions(
            extra_args=extra_args,
            resume_session_id=resume_session_id,
        ),
    )


class CodexCommandTest(unittest.TestCase):
    """Argv shape, `extra_args` placement, resume syntax, and absolute `-C`."""

    def test_dash_C_gets_full_path_for_relative_cwd(self) -> None:
        # codex applies `-C` AFTER it has already chdir'd into the subprocess
        # cwd, so a relative path resolves twice and codex hits "No such file
        # or directory (os error 2)". Pinning this guarantees the path passed
        # to `-C` is absolute even when WORKTREES_DIR (and the worktree path
        # derived from it) is relative.
        rel_cwd = Path("../wt-orchestrator/foo/issue-1")
        argv = _codex_argv(cwd=rel_cwd)
        c_value = argv[argv.index("-C") + 1]
        self.assertTrue(
            Path(c_value).is_absolute(),
            f"-C path should be absolute, got {c_value!r}",
        )
        self.assertEqual(Path(c_value), rel_cwd.resolve())

    def test_fresh_adds_args_before_exec(self) -> None:
        # Codex global options (`-m`, `-c`) must appear BEFORE the `exec`
        # subcommand; the parser rejects them after the subcommand. The
        # safety/output flags and prompt must remain on the argv tail.
        argv = _codex_argv(
            extra_args=(
                _agent_cases._MODEL_FLAG,
                _agent_cases._CODEX_MODEL,
                _agent_cases._CONFIG_FLAG,
                'model_reasoning_effort="xhigh"',
            ),
        )
        self.assertEqual(
            argv[1:5],
            [
                _agent_cases._MODEL_FLAG,
                _agent_cases._CODEX_MODEL,
                _agent_cases._CONFIG_FLAG,
                'model_reasoning_effort="xhigh"',
            ],
        )
        self.assertEqual(argv[5], _agent_cases._CODEX_EXEC)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], _agent_cases._PROMPT)

    def test_resume_adds_args_before_exec(self) -> None:
        sid = "11111111-2222-3333-4444-555555555555"
        argv = _codex_argv(
            extra_args=(_agent_cases._MODEL_FLAG, _agent_cases._CODEX_MODEL),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], [_agent_cases._MODEL_FLAG, _agent_cases._CODEX_MODEL])
        self.assertEqual(argv[3:5], [_agent_cases._CODEX_EXEC, "resume"])
        # Resume session id and prompt are still the last two tokens; the
        # extra args must NOT have displaced them.
        self.assertEqual(argv[-2:], [sid, _agent_cases._PROMPT])

    def test_empty_default_keeps_exec_first(self) -> None:
        # Backward compat: callers that don't pass `extra_args` still get the
        # legacy argv with `exec` immediately after the binary and no inserted
        # tokens.
        self.assertEqual(_codex_argv()[1], _agent_cases._CODEX_EXEC)


class CodexEnvScrubTest(unittest.TestCase):
    """The Codex runner threads the scrubbed child env into the subprocess."""

    def test_github_credentials_are_stripped(self) -> None:
        # The agent must never see GITHUB_TOKEN (or any synonym); the
        # orchestrator owns all GitHub writes. Provider auth keys
        # (ANTHROPIC_API_KEY, OPENAI_*) must NOT be stripped -- those are how
        # the agent talks to its own model.
        env = {
            "GITHUB_TOKEN": "ghp_secret",
            "GH_TOKEN": "ghp_alt",
            _agent_cases._ANTHROPIC_API_KEY: "sk-keep-me",
            _agent_cases._PATH_ENV: _agent_cases._SYSTEM_PATH,
        }
        passed_env = self._codex_child_env(env)
        self.assertNotIn("GITHUB_TOKEN", passed_env)
        self.assertNotIn("GH_TOKEN", passed_env)
        self.assertEqual(passed_env.get(_agent_cases._ANTHROPIC_API_KEY), "sk-keep-me")

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
            _agent_cases._PATH_ENV: _agent_cases._SYSTEM_PATH,
            "BUILD_NUMBER": "42",
            # Provider auth: must NOT be stripped.
            _agent_cases._ANTHROPIC_API_KEY: "sk-keep-anthropic",
            "OPENAI_API_KEY": "sk-keep-openai",
        }
        passed_env = self._codex_child_env(env)
        for stripped in (
            "STRIPE_API_KEY",
            "DATABASE_PASSWORD",
            "AWS_SECRET_ACCESS_KEY",
            "DEPLOY_TOKEN",
            "MY_CREDENTIAL",
            "PAGERDUTY_PAT",
            "VAULT_SECRET",
            "database_password",
            "TOKEN",
            "PASSWORD",
        ):
            self.assertNotIn(stripped, passed_env)
        # Non-secret vars survive.
        self.assertEqual(passed_env.get(_agent_cases._PATH_ENV), _agent_cases._SYSTEM_PATH)
        self.assertEqual(passed_env.get("BUILD_NUMBER"), "42")
        # Provider auth survives.
        self.assertEqual(
            passed_env.get(_agent_cases._ANTHROPIC_API_KEY),
            "sk-keep-anthropic",
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
            _agent_cases._PATH_ENV: _agent_cases._SYSTEM_PATH,
        }
        passed_env = self._codex_child_env(env)
        for stripped in _agents._AGENT_WRITE_CREDENTIAL_LOCATORS:
            self.assertNotIn(
                stripped,
                passed_env,
                f"{stripped} must be stripped from the agent env",
            )
        self.assertEqual(passed_env.get(_agent_cases._PATH_ENV), _agent_cases._SYSTEM_PATH)

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
        passed_env = self._codex_child_env(env)
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

    def _codex_child_env(self, env: dict[str, str]) -> dict[str, str]:
        with (
            patch.dict(_agent_cases._OS_ENVIRON_TARGET, env, clear=True),
            patch(
                _agent_cases._POPEN_TARGET,
                return_value=_support.completed(),
            ) as run_mock,
        ):
            _codex.run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
            return dict(run_mock.call_args.kwargs[_agent_cases._ENV_KWARG])


class CodexInterruptedResultTest(unittest.TestCase):
    """A run cut short by the shutdown sweep threads `interrupted` through.

    The SIGTERM/SIGKILL classification lives in the process owner; the Codex
    runner must carry that flag onto its `AgentResult`, distinct from a normal
    completion and from the orchestrator's own `timed_out` path.
    """

    def test_signal_exit_threads_interrupted_through(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(returncode=-signal.SIGTERM),
        ):
            agent_result = _codex.run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertTrue(agent_result.interrupted)
        self.assertFalse(agent_result.timed_out)
        self.assertEqual(agent_result.exit_code, -signal.SIGTERM)

    def test_clean_run_reports_not_interrupted(self) -> None:
        with patch(
            _agent_cases._POPEN_TARGET,
            return_value=_support.completed(returncode=0),
        ):
            agent_result = _codex.run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
        self.assertFalse(agent_result.interrupted)


class CodexLastMessageTest(unittest.TestCase):
    """The scratch last-message file lives outside the worktree and is read
    back as the agent's final output, then removed once the run ends.
    """

    def test_scratch_file_lives_outside_worktree(self) -> None:
        with _codex.codex_last_message_file() as scratch_path:
            self.assertTrue(scratch_path.exists())
            self.assertTrue(scratch_path.name.startswith("codex-last-"))
            self.assertTrue(scratch_path.name.endswith(".txt"))

    def test_read_returns_written_contents(self) -> None:
        with _codex.codex_last_message_file() as scratch_path:
            scratch_path.write_text("final answer")
            self.assertEqual(_codex.read_last_message(scratch_path), "final answer")

    def test_spent_scratch_removed_and_reads_empty(self) -> None:
        # The per-spawn scratch file must not outlive the run; a later read of
        # the spent path yields an empty final message rather than raising --
        # the shape a crashed or timed-out backend that never wrote it leaves.
        spent_path = self._spent_scratch_path()
        self.assertFalse(spent_path.exists())
        self.assertEqual(_codex.read_last_message(spent_path), "")

    def _spent_scratch_path(self) -> Path:
        # Returning from inside the block runs the context manager's cleanup,
        # so the returned path points at an already-unlinked scratch file.
        with _codex.codex_last_message_file() as scratch_path:
            return scratch_path


if __name__ == "__main__":
    unittest.main()
