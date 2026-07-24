# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Non-secret ``.env`` parsing and loading.

Split from the resolver in ``environment`` so the loader stays a small leaf:
``environment`` imports ``load_dotenv`` (and the shared truthy-value set) from
here, never the reverse. Secret keys are refused because the implementer agent
can read this file.
"""
from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Callable

_SECRET_KEYS = frozenset((
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
))
_TRUE_VALUES = frozenset(("1", "true", "on", "yes"))


def strip_dotenv_quotes(dotenv_value: str) -> str:
    """Strip one matched outer quote pair while preserving inner quotes."""
    stripped_value = dotenv_value.strip()
    if len(stripped_value) < 2:
        return stripped_value
    quote = stripped_value[0]
    if quote in ('"', "'") and stripped_value[-1] == quote:
        return stripped_value[1:-1]
    return stripped_value


def _load_entry(
    raw_line: str,
    env_path: Path,
    environ: MutableMapping[str, str],
    config_warning: Callable[[str], None],
) -> None:
    """Load one ``.env`` line, warning on and skipping secret keys."""
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return
    raw_key, _sep, raw_value = line.partition("=")
    key = raw_key.strip()
    if key in _SECRET_KEYS:
        config_warning(
            f"orchestrator: ignoring {key} in {env_path}; the implementer "
            "agent can read this file. Move the token to "
            "~/.config/<owner>/<repo>/token (path derived from REPO) "
            f"or export {key} before launching.",
        )
        return
    environ.setdefault(key, strip_dotenv_quotes(raw_value))


def load_dotenv(
    repo_root: Path,
    environ: MutableMapping[str, str],
    config_warning: Callable[[str], None],
) -> None:
    """Load safe entries from ``repo_root/.env`` into ``environ``."""
    if environ.get("ORCHESTRATOR_SKIP_DOTENV", "").strip().lower() in _TRUE_VALUES:
        return
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        _load_entry(raw_line, env_path, environ, config_warning)
