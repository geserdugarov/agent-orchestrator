# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Small parsers for runtime configuration values."""
from __future__ import annotations

from typing import Callable, NoReturn


class PositiveIntParser:
    """Callable positive-integer parser bound to the config error funnel."""

    def __init__(self, config_error: Callable[[str], NoReturn]) -> None:
        self._config_error = config_error

    def __call__(
        self,
        setting_name: str,
        raw_setting: str,
        default: int,
    ) -> int:
        stripped_setting = (raw_setting or "").strip()
        if not stripped_setting:
            return default
        try:
            parsed_setting = int(stripped_setting)
        except ValueError:
            self._config_error(
                f"orchestrator: {setting_name}={raw_setting!r} is not a "
                "valid integer; expected a positive integer (>= 1)",
            )
        if parsed_setting < 1:
            self._config_error(
                f"orchestrator: {setting_name}={raw_setting!r} must be >= 1 "
                "(zero or negative would block all work)",
            )
        return parsed_setting


def parse_hitl_handles(raw_handles: str) -> tuple[str, ...]:
    """Normalize, deduplicate, and preserve configured handle order."""
    handles: list[str] = []
    seen_handles: set[str] = set()
    for raw_handle in raw_handles.split(","):
        hitl_handle = raw_handle.strip().lstrip("@").strip()
        if not hitl_handle or hitl_handle in seen_handles:
            continue
        handles.append(hitl_handle)
        seen_handles.add(hitl_handle)
    return tuple(handles)


def parse_verify_commands(raw_commands: str) -> tuple[str, ...]:
    """Split newline/semicolon commands, dropping blanks and comments."""
    commands: list[str] = []
    for raw_line in raw_commands.replace(";", "\n").splitlines():
        command = raw_line.strip()
        if command and not command.startswith("#"):
            commands.append(command)
    return tuple(commands)
