# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused agent runtime tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import agents as _agents
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases


class RunCodexEnvScrubTest(unittest.TestCase):
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
        with (
            patch.dict(_agent_cases._OS_ENVIRON_TARGET, env, clear=True),
            patch(
                _agent_cases._POPEN_TARGET,
                return_value=_support.completed(),
            ) as run_mock,
        ):
            _agents._run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
            passed_env = dict(run_mock.call_args.kwargs[_agent_cases._ENV_KWARG])
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
        with (
            patch.dict(_agent_cases._OS_ENVIRON_TARGET, env, clear=True),
            patch(
                _agent_cases._POPEN_TARGET,
                return_value=_support.completed(),
            ) as run_mock,
        ):
            _agents._run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
            passed_env = dict(run_mock.call_args.kwargs[_agent_cases._ENV_KWARG])
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
        with (
            patch.dict(_agent_cases._OS_ENVIRON_TARGET, env, clear=True),
            patch(
                _agent_cases._POPEN_TARGET,
                return_value=_support.completed(),
            ) as run_mock,
        ):
            _agents._run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
            passed_env = dict(run_mock.call_args.kwargs[_agent_cases._ENV_KWARG])
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
        with (
            patch.dict(_agent_cases._OS_ENVIRON_TARGET, env, clear=True),
            patch(
                _agent_cases._POPEN_TARGET,
                return_value=_support.completed(),
            ) as run_mock,
        ):
            _agents._run_codex(_agent_cases._PROMPT, _agent_cases._CWD)
            passed_env = dict(run_mock.call_args.kwargs[_agent_cases._ENV_KWARG])
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
