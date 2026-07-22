# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow redaction."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

config = _owner.config
os = _owner.os
_REDACT_MIN_VALUE_LEN = _state._REDACT_MIN_VALUE_LEN
_SECRET_KEY_NAMES = _state._SECRET_KEY_NAMES
_SECRET_KEY_SUFFIXES = _state._SECRET_KEY_SUFFIXES


def _is_secret_environment_value(key: str, env_value: str) -> bool:
    """Whether an environment entry is shaped like a usable secret."""
    if not env_value or len(env_value) < _REDACT_MIN_VALUE_LEN:
        return False
    upper_key = key.upper()
    return upper_key in _SECRET_KEY_NAMES or any(
        upper_key.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES
    )


def _redact_environment_secrets(text: str) -> str:
    """Replace every secret-shaped process environment value."""
    redacted = text
    for key, env_value in os.environ.items():
        if _owner._is_secret_environment_value(key, env_value):
            redacted = redacted.replace(env_value, "***")
    return redacted


def _redact_configured_github_token(text: str) -> str:
    """Redact the PAT even when it came from a token file, not the env."""
    token = config.GITHUB_TOKEN
    if token and len(token) >= _REDACT_MIN_VALUE_LEN:
        return text.replace(token, "***")
    return text


def _redact_secrets(text: str) -> str:
    """Replace values of secret-shaped env vars in `text` with `***`.

    Called before any stderr is surfaced to GitHub or the log so a
    prompt-injected agent that echoes its own provider key cannot exfiltrate
    it via a park comment. Snapshot of os.environ at call time, so a key
    that was unset between subprocess spawn and the post is no longer
    redacted -- acceptable since it also no longer leaks anything reachable
    from the agent.
    """
    if not text:
        return text
    # GITHUB_TOKEN may have been resolved from ORCHESTRATOR_TOKEN_FILE (or
    # the default ~/.config/<repo>/token path) rather than the process env,
    # in which case the environment scan never sees it. The explicit token
    # pass also covers git/gh stderr that quotes a file-backed credential.
    return _owner._redact_configured_github_token(
        _owner._redact_environment_secrets(text)
    )
