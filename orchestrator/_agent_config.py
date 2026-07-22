# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shell-like agent backend specification parsing."""
from __future__ import annotations

import shlex
from typing import Callable, NoReturn

ConfigError = Callable[[str], NoReturn]
_CLAUDE_BACKEND = "claude"


def _agent_spec_tokens(
    setting_name: str,
    agent_spec: str,
    config_error: ConfigError,
) -> list[str]:
    """Shell-split a backend specification and reject invalid input."""
    raw_spec = (agent_spec or "").strip()
    if not raw_spec:
        config_error(
            f"orchestrator: {setting_name}={agent_spec!r} is empty; "
            "expected 'codex' or 'claude' (optionally followed by CLI args)",
        )
    try:
        spec_tokens = shlex.split(raw_spec)
    except ValueError as error:
        config_error(
            f"orchestrator: {setting_name}={agent_spec!r} is not a valid "
            f"shell-like command spec ({error}); expected 'codex' or "
            "'claude' (optionally followed by CLI args)",
        )
    if not spec_tokens:
        config_error(
            f"orchestrator: {setting_name}={agent_spec!r} parses to no "
            "tokens; expected 'codex' or 'claude' "
            "(optionally followed by CLI args)",
        )
    return spec_tokens


def parse_agent_spec(
    setting_name: str,
    agent_spec: str,
    config_error: ConfigError,
) -> tuple[str, tuple[str, ...]]:
    """Parse a shell-like backend specification into backend and arguments."""
    spec_tokens = _agent_spec_tokens(setting_name, agent_spec, config_error)
    backend = spec_tokens[0].lower()
    if backend not in ("codex", _CLAUDE_BACKEND):
        config_error(
            f"orchestrator: {setting_name}={agent_spec!r} first token "
            f"{spec_tokens[0]!r} is invalid; expected 'codex' or 'claude'",
        )
    return backend, tuple(spec_tokens[1:])
