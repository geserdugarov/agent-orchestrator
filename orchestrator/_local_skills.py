# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Filesystem discovery of skills and tools offered to local Codex runs."""
from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Iterable

_SKILL_ROOTS = (".agents/skills", ".claude/skills")
_SKILL_FILE = "SKILL.md"
_SYSTEM_SKILL_DIR = ".system"


def _direct_skill_names(root: Path) -> list[str]:
    """Return direct ``<root>/<name>/SKILL.md`` skill names."""
    names: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return names
    for entry in entries:
        if entry.name.startswith("."):
            continue
        with suppress(OSError):
            if entry.is_dir() and (Path(entry.path) / _SKILL_FILE).is_file():
                names.append(entry.name)
    return sorted(names)


def _add_skill_names(
    seen_names: dict[str, None],
    skill_names: Iterable[str],
) -> None:
    """Add names to an insertion-ordered deduplication map."""
    for skill_name in skill_names:
        seen_names.setdefault(skill_name, None)


def _global_codex_skill_names() -> list[str]:
    """Collect user and built-in skill names from the global Codex root."""
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    global_root = Path(codex_home) / "skills"
    return sorted(set(
        _direct_skill_names(global_root)
        + _direct_skill_names(global_root / _SYSTEM_SKILL_DIR)
    ))


def discover_local_skills(cwd: Path) -> tuple[str, ...]:
    """Enumerate names available to a Codex run rooted at ``cwd``.

    Repository roots are ordered before the global Codex roots and missing or
    unreadable directories contribute nothing. Only names are read; skill
    instruction contents remain outside analytics collection.
    """
    seen_names: dict[str, None] = {}
    for skill_root in _SKILL_ROOTS:
        _add_skill_names(
            seen_names,
            _direct_skill_names(cwd / skill_root),
        )
    _add_skill_names(seen_names, _global_codex_skill_names())
    return tuple(seen_names)


_CODEX_OFFERED_TOOLS: tuple[str, ...] = (
    "exec_command",
    "write_stdin",
    "update_plan",
    "request_user_input",
    "view_image",
    "multi_agent_v1",
    "get_goal",
    "create_goal",
    "update_goal",
    "web_search",
)


def discover_codex_tools() -> tuple[str, ...]:
    """Return the best-effort Codex offered-tools baseline."""
    return _CODEX_OFFERED_TOOLS
