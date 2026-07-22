# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Classify Codex SKILL.md command references by evidence tier."""

from __future__ import annotations

import re

from orchestrator import _usage_shell_segments as shell_segments


CodexTokenClassification = tuple[list[str], list[str], bool]

SKILL_PATH_RE = re.compile(r"(?<!\w)skills/([^/\s\"']+)/SKILL\.md\b")
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
OUTPUT_REDIRECT_RE = re.compile(r"^(?:\d+|&)?>>?\|?")
SED_INPLACE_RE = re.compile(r"^(?:-[a-zA-Z]*i|--in-place)")
READER_VERBS = frozenset(
    (
        "cat",
        "sed",
        "head",
        "tail",
        "less",
        "more",
        "bat",
        "nl",
    )
)


def codex_reads(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if ENV_ASSIGNMENT_RE.match(token):
            continue
        verb = token.rsplit("/", 1)[-1]
        if verb not in READER_VERBS:
            return False
        arguments = tokens[index + 1:]
        if verb == "sed" and any(SED_INPLACE_RE.match(argument) for argument in arguments):
            return False
        return True
    return False


def classify_codex_segment(
    segment: str,
    inferred: list[str],
    incidental: list[str],
) -> None:
    extend_codex_classification(segment.split(), inferred, incidental)


def extend_codex_classification(
    tokens: list[str],
    inferred: list[str],
    incidental: list[str],
) -> None:
    reads = codex_reads(tokens)
    previous_redirect = False
    for token in tokens:
        classified = classify_codex_token(token, reads, previous_redirect)
        inferred.extend(classified[0])
        incidental.extend(classified[1])
        previous_redirect = classified[2]


def classify_codex_token(
    token: str,
    reads: bool,
    previous_redirect: bool,
) -> CodexTokenClassification:
    redirect_match = OUTPUT_REDIRECT_RE.match(token)
    redirect_end = redirect_match.end() if redirect_match else -1
    names = SKILL_PATH_RE.findall(token)
    is_target = previous_redirect or 0 <= redirect_end < len(token)
    next_is_redirect = redirect_end == len(token)
    if is_target:
        return [], names, next_is_redirect
    if reads:
        return names, [], next_is_redirect
    return [], names, next_is_redirect


def classify_codex_command(command: str) -> tuple[list[str], list[str]]:
    inferred: list[str] = []
    incidental: list[str] = []
    script = shell_segments.unwrap_codex_command(command)
    for segment in shell_segments.split_codex_segments(script):
        classify_codex_segment(segment, inferred, incidental)
    return inferred, incidental
