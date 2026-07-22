# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Quote-aware command unwrapping and shell-segment scanning."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


SHELL_WRAPPER_RE = re.compile(r"^\s*(?:\S*/)?(?:ba)?sh\s+-[a-z]*c\s+")


def is_escaped_double_quote(
    text: str,
    index: int,
    quote: str,
    character: str,
) -> bool:
    next_index = index + 1
    if quote != '"' or character != "\\":
        return False
    if next_index >= len(text):
        return False
    return text[next_index] in ('"', "\\", "$", "`")


def read_quoted(text: str) -> str:
    quote = text[0]
    decoded: list[str] = []
    index = 1
    while index < len(text):
        character = text[index]
        if character == quote:
            return "".join(decoded)
        if is_escaped_double_quote(text, index, quote, character):
            decoded.append(text[index + 1])
            index += 2
            continue
        decoded.append(character)
        index += 1
    return "".join(decoded)


def unwrap_codex_command(command: str) -> str:
    match = SHELL_WRAPPER_RE.match(command)
    if match is None:
        return command
    wrapped_script = command[match.end():]
    if wrapped_script[:1] in ("'", '"'):
        return read_quoted(wrapped_script)
    return command


@dataclass
class ShellSegmentScanner:
    script: str
    segments: list[str] = field(default_factory=list)
    segment_start: int = 0
    quote: str = ""
    index: int = 0

    def split(self) -> list[str]:
        while self.index < len(self.script):
            self._advance()
        self.segments.append(self.script[self.segment_start:])
        return self.segments

    def _advance(self) -> None:
        if self._skip_escaped_character():
            return
        character = self.script[self.index]
        if self.quote:
            if character == self.quote:
                self.quote = ""
            self.index += 1
            return
        if character in ("'", '"'):
            self.quote = character
            self.index += 1
            return
        operator_width = self._operator_width()
        if operator_width:
            self.segments.append(self.script[self.segment_start:self.index])
            self.index += operator_width
            self.segment_start = self.index
            return
        self.index += 1

    def _skip_escaped_character(self) -> bool:
        if self.script[self.index] != "\\" or self.quote == "'":
            return False
        if self.index + 1 >= len(self.script):
            return False
        self.index += 2
        return True

    def _operator_width(self) -> int:
        operator_end = self.index + 2
        if self.script[self.index:operator_end] in ("&&", "||"):
            return 2
        if self.script[self.index] in (";", "\n", "|"):
            return 1
        return 0


def split_codex_segments(script: str) -> list[str]:
    return ShellSegmentScanner(script).split()
