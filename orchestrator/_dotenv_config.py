# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Non-secret dotenv parsing and loading."""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Callable, Optional

_SECRET_KEYS = frozenset((
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
))
_TRUE_VALUES = frozenset(("1", "true", "on", "yes"))


def _has_matched_outer_quotes(dotenv_value: str) -> bool:
    """Return whether a value has one matching outer quote pair."""
    if len(dotenv_value) < 2:
        return False
    quote = dotenv_value[0]
    if quote not in ('"', "'"):
        return False
    return dotenv_value[-1] == quote


def strip_dotenv_quotes(dotenv_value: str) -> str:
    """Strip one matched outer quote pair while preserving inner quotes."""
    stripped_value = dotenv_value.strip()
    if _has_matched_outer_quotes(stripped_value):
        return stripped_value[1:-1]
    return stripped_value


def _loading_disabled(environ: Mapping[str, str]) -> bool:
    setting = environ.get("ORCHESTRATOR_SKIP_DOTENV", "")
    return setting.strip().lower() in _TRUE_VALUES


def _parse_entry(raw_line: str) -> Optional[tuple[str, str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    key, _, raw_value = line.partition("=")
    return key.strip(), strip_dotenv_quotes(raw_value)


def _load_entry(
    raw_line: str,
    env_path: Path,
    environ: MutableMapping[str, str],
    config_warning: Callable[[str], None],
) -> None:
    entry = _parse_entry(raw_line)
    if entry is None:
        return
    key, entry_value = entry
    if key in _SECRET_KEYS:
        config_warning(
            f"orchestrator: ignoring {key} in {env_path}; the implementer "
            "agent can read this file. Move the token to "
            "~/.config/<owner>/<repo>/token (path derived from REPO) "
            f"or export {key} before launching.",
        )
        return
    environ.setdefault(key, entry_value)


def load_dotenv(
    repo_root: Path,
    environ: MutableMapping[str, str],
    config_warning: Callable[[str], None],
) -> None:
    """Load safe entries from ``repo_root/.env`` into ``environ``."""
    if _loading_disabled(environ):
        return
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        _load_entry(raw_line, env_path, environ, config_warning)
