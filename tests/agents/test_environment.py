# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Credential-filtering owner tests."""

from __future__ import annotations

import unittest

from orchestrator.agents import environment as _environment
from tests import agent_test_values as _agent_cases


class FilterAgentEnvTest(unittest.TestCase):
    """Unit-level coverage for the `environment.filter_agent_env` boundary.

    The helper is the single boundary both agent subprocesses and the
    verify runner share, so its behavior is exercised in isolation here
    (no Popen spawn) for the edge cases the integration tests don't
    explicitly enumerate.
    """

    def test_drops_github_aliases_via_exact_match(self) -> None:
        # The GitHub-token alias list contains entries that don't match
        # the secret-shape suffix (e.g. `GH_HOST`); they must still be
        # stripped via `_FORBIDDEN_AGENT_ENV`.
        env = {"GH_HOST": "github.example.com", _agent_cases._PATH_ENV: _agent_cases._SYSTEM_PATH}
        filtered_env = _environment.filter_agent_env(env)
        self.assertNotIn("GH_HOST", filtered_env)
        self.assertEqual(filtered_env.get(_agent_cases._PATH_ENV), _agent_cases._SYSTEM_PATH)

    def test_write_locators_stripped_in_both_modes(self) -> None:
        # `_AGENT_WRITE_CREDENTIAL_LOCATORS` is stripped regardless of the
        # `allow_provider_auth` flag -- the verify path (False) and the
        # agent path (True) must both refuse to forward SSH agent /
        # askpass / GIT_SSH_COMMAND.
        env = {name: "value" for name in _environment._AGENT_WRITE_CREDENTIAL_LOCATORS}
        for allow in (True, False):
            filtered_env = _environment.filter_agent_env(env, allow_provider_auth=allow)
            for name in _environment._AGENT_WRITE_CREDENTIAL_LOCATORS:
                self.assertNotIn(
                    name,
                    filtered_env,
                    f"{name} must be stripped (allow_provider_auth={allow})",
                )

    def test_allowlist_preserves_provider_auth(self) -> None:
        # Every name in the provider-auth allowlist must survive the
        # shape filter; the agent CLI uses these to talk to its own
        # model and stripping them breaks the run.
        env = {name: "value-long-enough" for name in _environment._AGENT_PROVIDER_AUTH_ALLOWLIST}
        filtered_env = _environment.filter_agent_env(env)
        for name in _environment._AGENT_PROVIDER_AUTH_ALLOWLIST:
            self.assertEqual(filtered_env.get(name), "value-long-enough")

    def test_provider_auth_block_strips_keys(self) -> None:
        # Verify-command path passes `allow_provider_auth=False` so the
        # agent's own provider keys are also stripped. A hostile
        # dependency executed under the verify shell would otherwise
        # gain billable access to the operator's model account.
        env = {name: "value-long-enough" for name in _environment._AGENT_PROVIDER_AUTH_ALLOWLIST}
        env[_agent_cases._PATH_ENV] = _agent_cases._SYSTEM_PATH
        filtered_env = _environment.filter_agent_env(env, allow_provider_auth=False)
        for name in _environment._AGENT_PROVIDER_AUTH_ALLOWLIST:
            self.assertNotIn(
                name,
                filtered_env,
                f"{name} must be stripped when allow_provider_auth=False",
            )
        # Non-secret entries still survive.
        self.assertEqual(filtered_env.get(_agent_cases._PATH_ENV), _agent_cases._SYSTEM_PATH)

    def test_secret_shape_predicate(self) -> None:
        # Direct check on the predicate so the contract is documented
        # independent of any caller. Suffix matches and bare names hit;
        # provider-shaped allowlisted names also hit the predicate (the
        # allowlist runs above it in `environment.filter_agent_env`).
        for name in (
            "FOO_TOKEN",
            "BAR_KEY",
            "BAZ_SECRET",
            "QUX_PASSWORD",
            "PD_PAT",
            "MY_CREDENTIAL",
            "TOKEN",
            "PASSWORD",
            "ANTHROPIC_API_KEY",
            "stripe_api_key",
            # Credential-file locator shapes (issue #213 review).
            "ORCHESTRATOR_TOKEN_FILE",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AWS_SHARED_CREDENTIALS_FILE",
            "MY_DB_PASSWORD_FILE",
            "TLS_KEY_FILE",
            "VAULT_SECRET_FILE",
            "AZURE_CREDENTIALS",
            "CREDENTIALS",
            "TOKEN_FILE",
            "CREDENTIALS_FILE",
        ):
            self.assertTrue(_environment.is_secret_shaped(name), f"{name} should look secret-shaped")
        for name in (
            _agent_cases._PATH_ENV,
            "HOME",
            "BUILD_NUMBER",
            "CI",
            "USER",
            # Plain config-file locators (non-credential) must not match.
            "MY_CONFIG_FILE",
            "PROFILE_FILE",
        ):
            self.assertFalse(_environment.is_secret_shaped(name), f"{name} should not look secret-shaped")

    def test_empty_env_passthrough(self) -> None:
        self.assertEqual(_environment.filter_agent_env({}), {})
