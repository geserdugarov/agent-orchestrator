# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agents import (
    _AGENT_PROVIDER_AUTH_ALLOWLIST,
    _AGENT_WRITE_CREDENTIAL_LOCATORS,
    _claude_last_message,
    _filter_agent_env,
    _is_secret_shaped,
    _run_claude,
    _run_codex,
    parse_session_id,
    run_agent,
)


_CWD = Path("/tmp/agent-orchestrator-test-cwd-doesnt-matter")


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    # _run_subprocess uses Popen + communicate(timeout=...). The mock returns
    # (stdout, stderr) from communicate and exposes .returncode -- enough to
    # let tests assert on argv passed to Popen without spawning anything.
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    return proc


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

    def test_falls_back_to_assistant_text_when_no_result(self) -> None:
        events = [
            json.dumps({"type": "assistant", "message": {
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ],
            }}),
        ]
        self.assertEqual(_claude_last_message("\n".join(events)), "hello world")

    def test_returns_empty_when_no_recognizable_events(self) -> None:
        self.assertEqual(_claude_last_message(""), "")
        self.assertEqual(
            _claude_last_message('{"type":"system","subtype":"init"}'),
            "",
        )


class RunAgentDispatchTest(unittest.TestCase):
    def test_unknown_backend_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            run_agent("gemini", "prompt", _CWD)
        self.assertIn("gemini", str(cm.exception))

    def test_dispatches_to_codex(self) -> None:
        # Use stream-json-shaped output so parse_session_id has something to
        # find; the codex runner doesn't care about claude shape.
        sid = "abcdef12-3456-7890-abcd-ef1234567890"
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(stdout=json.dumps({"session_id": sid})),
        ) as run_mock:
            result = run_agent("codex", "p", _CWD)
        self.assertEqual(result.session_id, sid)
        self.assertEqual(result.exit_code, 0)
        argv = run_mock.call_args.args[0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[1], "exec")

    def test_dispatches_to_claude(self) -> None:
        sid = "cafe1234-5678-90ab-cdef-1234567890ab"
        events = [
            json.dumps({"type": "system", "session_id": sid}),
            json.dumps({"type": "result", "result": "shipped"}),
        ]
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(stdout="\n".join(events)),
        ) as run_mock:
            result = run_agent("claude", "p", _CWD)
        self.assertEqual(result.session_id, sid)
        self.assertEqual(result.last_message, "shipped")
        argv = run_mock.call_args.args[0]
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
            "ANTHROPIC_API_KEY": "sk-keep-me",
            "PATH": "/usr/bin",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", _CWD)
        passed_env = run_mock.call_args.kwargs["env"]
        self.assertNotIn("GITHUB_TOKEN", passed_env)
        self.assertNotIn("GH_TOKEN", passed_env)
        self.assertEqual(passed_env.get("ANTHROPIC_API_KEY"), "sk-keep-me")

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
            "PATH": "/usr/bin",
            "BUILD_NUMBER": "42",
            # Provider auth: must NOT be stripped.
            "ANTHROPIC_API_KEY": "sk-keep-anthropic",
            "OPENAI_API_KEY": "sk-keep-openai",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", _CWD)
        passed_env = run_mock.call_args.kwargs["env"]
        for stripped in (
            "STRIPE_API_KEY", "DATABASE_PASSWORD", "AWS_SECRET_ACCESS_KEY",
            "DEPLOY_TOKEN", "MY_CREDENTIAL", "PAGERDUTY_PAT", "VAULT_SECRET",
            "database_password", "TOKEN", "PASSWORD",
        ):
            self.assertNotIn(stripped, passed_env)
        # Non-secret vars survive.
        self.assertEqual(passed_env.get("PATH"), "/usr/bin")
        self.assertEqual(passed_env.get("BUILD_NUMBER"), "42")
        # Provider auth survives.
        self.assertEqual(
            passed_env.get("ANTHROPIC_API_KEY"), "sk-keep-anthropic",
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
            "PATH": "/usr/bin",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", _CWD)
        passed_env = run_mock.call_args.kwargs["env"]
        for stripped in _AGENT_WRITE_CREDENTIAL_LOCATORS:
            self.assertNotIn(
                stripped, passed_env,
                f"{stripped} must be stripped from the agent env",
            )
        self.assertEqual(passed_env.get("PATH"), "/usr/bin")

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
        with patch.dict("os.environ", env, clear=True), patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", _CWD)
        passed_env = run_mock.call_args.kwargs["env"]
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
        env = {"GH_HOST": "github.example.com", "PATH": "/usr/bin"}
        out = _filter_agent_env(env)
        self.assertNotIn("GH_HOST", out)
        self.assertEqual(out.get("PATH"), "/usr/bin")

    def test_drops_write_credential_locators_in_both_modes(self) -> None:
        # `_AGENT_WRITE_CREDENTIAL_LOCATORS` is stripped regardless of
        # the `allow_provider_auth` flag -- the verify path (False) and
        # the agent path (True) must both refuse to forward SSH agent /
        # askpass / GIT_SSH_COMMAND.
        env = {name: "value" for name in _AGENT_WRITE_CREDENTIAL_LOCATORS}
        for allow in (True, False):
            out = _filter_agent_env(env, allow_provider_auth=allow)
            for name in _AGENT_WRITE_CREDENTIAL_LOCATORS:
                self.assertNotIn(
                    name, out,
                    f"{name} must be stripped (allow_provider_auth={allow})",
                )

    def test_allowlist_preserves_provider_auth(self) -> None:
        # Every name in the provider-auth allowlist must survive the
        # shape filter; the agent CLI uses these to talk to its own
        # model and stripping them breaks the run.
        env = {name: "value-long-enough" for name in _AGENT_PROVIDER_AUTH_ALLOWLIST}
        out = _filter_agent_env(env)
        for name in _AGENT_PROVIDER_AUTH_ALLOWLIST:
            self.assertEqual(out.get(name), "value-long-enough")

    def test_allow_provider_auth_false_strips_provider_keys(self) -> None:
        # Verify-command path passes `allow_provider_auth=False` so the
        # agent's own provider keys are also stripped. A hostile
        # dependency executed under the verify shell would otherwise
        # gain billable access to the operator's model account.
        env = {name: "value-long-enough" for name in _AGENT_PROVIDER_AUTH_ALLOWLIST}
        env["PATH"] = "/usr/bin"
        out = _filter_agent_env(env, allow_provider_auth=False)
        for name in _AGENT_PROVIDER_AUTH_ALLOWLIST:
            self.assertNotIn(
                name, out,
                f"{name} must be stripped when allow_provider_auth=False",
            )
        # Non-secret entries still survive.
        self.assertEqual(out.get("PATH"), "/usr/bin")

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
            "PATH", "HOME", "BUILD_NUMBER", "CI", "USER",
            # Plain config-file locators (non-credential) must not match.
            "MY_CONFIG_FILE", "PROFILE_FILE",
        ):
            self.assertFalse(
                _is_secret_shaped(name), f"{name} should not look secret-shaped"
            )

    def test_empty_env_passthrough(self) -> None:
        self.assertEqual(_filter_agent_env({}), {})


class RunCodexCwdTest(unittest.TestCase):
    def test_dash_C_receives_absolute_path_for_relative_cwd(self) -> None:
        # codex applies `-C` AFTER it has already chdir'd into the subprocess
        # cwd, so a relative path resolves twice and codex hits "No such file
        # or directory (os error 2)". Pinning this guarantees the path passed
        # to `-C` is absolute even when WORKTREES_DIR (and the worktree path
        # derived from it) is relative.
        rel_cwd = Path("../wt-orchestrator/foo/issue-1")
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", rel_cwd)
        argv = run_mock.call_args.args[0]
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
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_claude("followup", _CWD, resume_session_id=sid)
        argv = run_mock.call_args.args[0]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)


class RunAgentExtraArgsTest(unittest.TestCase):
    """`extra_args` lets a role-specific config inject backend-CLI flags
    (e.g. `-m gpt-5.5` for codex, `--model X --effort high` for claude)
    into the spawned argv on both fresh and resumed runs while keeping the
    safety/output flags and prompt where they already are.
    """

    def _argv_for(
        self,
        backend: str,
        *,
        extra_args: tuple[str, ...],
        resume_session_id=None,
    ) -> list[str]:
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            run_agent(
                backend, "p", _CWD,
                resume_session_id=resume_session_id,
                extra_args=extra_args,
            )
        return list(run_mock.call_args.args[0])

    def test_codex_fresh_injects_extra_args_before_exec(self) -> None:
        # Codex global options (`-m`, `-c`) must appear BEFORE the `exec`
        # subcommand; the parser rejects them after the subcommand. The
        # safety/output flags and prompt must remain on the argv tail.
        argv = self._argv_for(
            "codex",
            extra_args=("-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )
        self.assertEqual(argv[1:5], [
            "-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"',
        ])
        self.assertEqual(argv[5], "exec")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "p")

    def test_codex_resume_injects_extra_args_before_exec(self) -> None:
        sid = "11111111-2222-3333-4444-555555555555"
        argv = self._argv_for(
            "codex",
            extra_args=("-m", "gpt-5.5"),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], ["-m", "gpt-5.5"])
        self.assertEqual(argv[3:5], ["exec", "resume"])
        # Resume session id and prompt are still the last two tokens; the
        # extra args must NOT have displaced them.
        self.assertEqual(argv[-2:], [sid, "p"])

    def test_claude_fresh_injects_extra_args_before_safety_flags(self) -> None:
        argv = self._argv_for(
            "claude",
            extra_args=("--model", "claude-opus-4-7", "--effort", "high"),
        )
        self.assertEqual(argv[1:5], [
            "--model", "claude-opus-4-7", "--effort", "high",
        ])
        # Safety + output flags survive immediately after the extra args.
        self.assertEqual(argv[5], "-p")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[-1], "p")

    def test_claude_resume_keeps_extra_args_and_resume_flag(self) -> None:
        sid = "deadbeef-1234-1234-1234-1234deadbeef"
        argv = self._argv_for(
            "claude",
            extra_args=("--model", "claude-opus-4-7"),
            resume_session_id=sid,
        )
        self.assertEqual(argv[1:3], ["--model", "claude-opus-4-7"])
        # `--resume <sid>` is appended after the safety flags and right
        # before the prompt, regardless of extra_args.
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertEqual(argv[-1], "p")

    def test_default_empty_extra_args_leaves_argv_unchanged(self) -> None:
        # Backward compat: callers that don't pass `extra_args` still get
        # the legacy argv with no inserted tokens. Sanity-checks both
        # backends so a future refactor that changes argv shape under
        # default callers fails this test loudly.
        codex_argv = self._argv_for("codex", extra_args=())
        self.assertEqual(codex_argv[1], "exec")
        claude_argv = self._argv_for("claude", extra_args=())
        self.assertEqual(claude_argv[1], "-p")


if __name__ == "__main__":
    unittest.main()
