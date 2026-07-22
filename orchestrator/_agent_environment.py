# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Credential filtering and git identity for agent subprocesses."""
from __future__ import annotations

import os
from typing import Optional

from orchestrator import config

_FORBIDDEN_AGENT_ENV = frozenset((
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
    "GH_HOST",
))
_AGENT_WRITE_CREDENTIAL_LOCATORS = frozenset((
    "SSH_AUTH_SOCK",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "GIT_SSH_COMMAND",
))
_AGENT_SECRET_SUFFIXES = (
    "_TOKEN",
    "_KEY",
    "_SECRET",
    "_PASSWORD",
    "_PAT",
    "_CREDENTIAL",
    "_TOKEN_FILE",
    "_KEY_FILE",
    "_SECRET_FILE",
    "_PASSWORD_FILE",
    "_CREDENTIAL_FILE",
    "_CREDENTIALS",
    "_CREDENTIALS_FILE",
)
_AGENT_SECRET_BARE_NAMES = frozenset((
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PAT",
    "CREDENTIAL",
    "TOKEN_FILE",
    "CREDENTIALS",
    "CREDENTIALS_FILE",
))
_AGENT_PROVIDER_AUTH_ALLOWLIST = frozenset((
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
))


def is_secret_shaped(env_name: str) -> bool:
    """Return whether an environment name looks credential-bearing."""
    normalized_name = env_name.upper()
    if normalized_name in _AGENT_SECRET_BARE_NAMES:
        return True
    return any(
        normalized_name.endswith(secret_suffix)
        for secret_suffix in _AGENT_SECRET_SUFFIXES
    )


def _env_key_allowed(
    env_key: str,
    *,
    allow_provider_auth: bool,
) -> bool:
    if env_key in _FORBIDDEN_AGENT_ENV:
        return False
    if env_key in _AGENT_WRITE_CREDENTIAL_LOCATORS:
        return False
    if not is_secret_shaped(env_key):
        return True
    return allow_provider_auth and env_key in _AGENT_PROVIDER_AUTH_ALLOWLIST


def filter_agent_env(
    environ: dict[str, str],
    *,
    allow_provider_auth: bool = True,
) -> dict[str, str]:
    """Remove write credentials and secret-shaped values from an env."""
    return {
        env_key: env_value
        for env_key, env_value in environ.items()
        if _env_key_allowed(
            env_key,
            allow_provider_auth=allow_provider_auth,
        )
    }


def agent_env(extra_env: Optional[dict[str, str]]) -> dict[str, str]:
    """Build the filtered agent environment with orchestrator git identity."""
    environ = filter_agent_env(dict(os.environ))
    environ["GIT_AUTHOR_NAME"] = config.AGENT_GIT_NAME
    environ["GIT_AUTHOR_EMAIL"] = config.AGENT_GIT_EMAIL
    environ["GIT_COMMITTER_NAME"] = config.AGENT_GIT_NAME
    environ["GIT_COMMITTER_EMAIL"] = config.AGENT_GIT_EMAIL
    if extra_env:
        environ.update(extra_env)
    return environ
