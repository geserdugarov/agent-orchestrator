# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Extract which agent skills a run triggered from its JSONL stdout.

A skill-trigger extractor (``parse_claude_skills`` / ``parse_codex_skills`` /
``parse_agent_skills``) reuses ``_usage_metrics``'s shared event iterator and
resilience contract to record which agent *skills* a run triggered. It reads
only the skill name -- never the ``Skill`` tool's ``args`` -- and is
observation-only.

This module is the private home of the skill-trigger parsing. Its public
surface -- the ``SkillTriggers`` dataclass and the ``parse_claude_skills`` /
``parse_codex_skills`` / ``parse_agent_skills`` trio -- is re-exported from
``orchestrator.usage`` for callers (``analytics``). ``usage`` also reuses the
offered-set init-frame helpers (``_claude_init_field`` /
``_ordered_unique_names``) and the shared skill/trajectory JSONL vocabulary
(``_CONTENT_KEY`` / ``_COMMAND_EXECUTION``) defined here for its sibling
trajectory classifier, so the init-frame parsing and resilience contract stay
defined once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# The shared event iterator and JSONL protocol / backend vocabulary the
# skill-trigger extractors reuse (keeping the resilience contract defined once,
# in ``_usage_metrics``).
from orchestrator._usage_metrics import _iter_events
from orchestrator._usage_metrics import (
    _ASSISTANT,
    _CLAUDE,
    _CODEX,
    _ID,
    _INPUT,
    _ITEM_KEY,
    _MESSAGE,
    _TYPE,
)


# Skill/trajectory JSONL protocol field and message-type values this module
# reads on top of the shared usage vocabulary re-used from ``_usage_metrics``.
# ``usage`` re-imports both for its sibling trajectory classifier.
_CONTENT_KEY = "content"
_COMMAND_EXECUTION = "command_execution"


# --- skill-trigger extractor ------------------------------------------------


@dataclass(frozen=True)
class SkillTriggers:
    """Which agent skills a single run triggered, parsed from its JSONL stdout.

    ``triggered`` lists the distinct skill names in first-seen order;
    ``trigger_counts`` maps each name to how many times it fired, so a run
    that pulls ``develop`` in twice records ``{"develop": 2}`` while
    ``triggered`` still carries it once. ``available`` is the *offered*-skills
    set: on claude it is read from the dedicated ``skills`` array in the
    ``system``/``init`` frame, confirmed against a captured real stream; on
    codex it stays best-effort and empty until that stream's field is
    confirmed. It varies independently of ``triggered`` and is empty -- never
    an error -- when the frame or field is absent.

    Only the skill *name* is ever read: the ``Skill`` tool's ``input`` can
    carry an ``args`` string echoing issue or user content, and that field is
    deliberately never touched (Privacy, same doc). A missing or renamed
    field yields an empty result, never an exception -- the same resilience
    contract the usage parsers in ``_usage_metrics`` honor.
    """

    triggered: tuple[str, ...] = ()
    trigger_counts: dict[str, int] = field(default_factory=dict)
    available: tuple[str, ...] = ()


def _collect(
    names: Iterable[str], available: Iterable[str] = (),
) -> SkillTriggers:
    """Fold first-seen skill names into the de-duplicated / counted shape.

    ``available`` is passed through verbatim (already de-duplicated by the
    caller) so the offered set rides the same constructor as the triggered
    one; codex callers omit it and it defaults to empty.
    """
    order: list[str] = []
    counts: dict[str, int] = {}
    for name in names:
        if name not in counts:
            order.append(name)
            counts[name] = 0
        counts[name] += 1
    return SkillTriggers(
        triggered=tuple(order),
        trigger_counts=counts,
        available=tuple(available),
    )


def _claude_skill_name(block: Any) -> Optional[str]:
    """Return the skill name from a ``Skill`` tool_use block, else ``None``.

    Reads only ``input.skill``; ``input.args`` is never inspected (Privacy).
    """
    if not isinstance(block, dict):
        return None
    if block.get(_TYPE) != "tool_use" or block.get("name") != "Skill":
        return None
    inp = block.get(_INPUT)
    if not isinstance(inp, dict):
        return None
    skill = inp.get("skill")
    if isinstance(skill, str) and skill:
        return skill
    return None


def _claude_init_field(
    events: Iterable[dict[str, Any]], field_name: str,
) -> Any:
    for event in events:
        if event.get(_TYPE) != "system":
            continue
        if event.get("subtype") != "init":
            continue
        return event.get(field_name)
    return None


def _ordered_unique_names(raw_names: Any) -> tuple[str, ...]:
    if not isinstance(raw_names, list):
        return ()
    ordered_names: list[str] = []
    seen_names: set[str] = set()
    for name in raw_names:
        if not isinstance(name, str):
            continue
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        ordered_names.append(name)
    return tuple(ordered_names)


def _claude_offered_skills(events: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Read the offered-skills set from claude's ``system``/``init`` frame.

    The headless ``--output-format stream-json`` init frame carries a
    dedicated top-level ``skills`` array -- the skill names on offer to the
    session, repo-local (``develop`` / ``review``) and built-in alike --
    confirmed against a captured real stream. Read defensively: a missing or
    renamed field, or a non-string entry, filters out rather than raising;
    names are de-duplicated in first-seen order. The first ``init`` frame
    wins (a single run emits one).
    """
    return _ordered_unique_names(_claude_init_field(events, "skills"))


def parse_claude_skills(stdout: str) -> SkillTriggers:
    """Extract triggered + offered skills from a ``claude ... stream-json`` run.

    A skill invocation surfaces as a ``tool_use`` content block named
    ``"Skill"`` inside an ``assistant`` message; we read ``input.skill`` in
    first-seen order (never ``input.args`` -- Privacy).

    Under ``--include-partial-messages`` claude emits one ``assistant`` frame
    per *completed content block*, all sharing the message's ``id``: the
    content array is partitioned across those frames (a text block in its own
    frame, the following ``Skill`` block in the next), NOT a cumulative
    snapshot that repeats earlier blocks. A captured real stream confirmed
    this -- the ``usage`` sub-object repeats across the frames (so
    ``parse_claude_usage`` keeps the last per id), but a ``tool_use`` block
    appears in exactly one frame and carries a unique ``id``. So we walk
    *every* assistant frame and de-duplicate triggers by that block ``id``
    rather than taking the last frame per message id: last-frame-wins would
    silently drop a ``Skill`` block followed by a later block of the same
    message, while the per-block framing already means one trigger is never
    double-counted. The ``id`` de-dup additionally stays correct if a future
    stream *does* repeat a block across frames.

    ``available`` is read from the ``system``/``init`` frame's ``skills``
    array (``_claude_offered_skills``); it varies independently of the
    triggered set and is empty when that frame/field is absent.
    """
    events = _iter_events(stdout)
    collector = _ClaudeSkillCollector()
    for event in events:
        collector.add_event(event)
    return _collect(
        collector.names,
        available=_claude_offered_skills(events),
    )


@dataclass
class _ClaudeSkillCollector:
    names: list[str] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)

    def add_event(self, event: dict[str, Any]) -> None:
        if event.get(_TYPE) != _ASSISTANT:
            return
        message = event.get(_MESSAGE)
        if not isinstance(message, dict):
            return
        blocks = message.get(_CONTENT_KEY)
        if not isinstance(blocks, list):
            return
        for block in blocks:
            self._add_block(block)

    def _add_block(self, block: Any) -> None:
        name = _claude_skill_name(block)
        if name is None:
            return
        block_id = block.get(_ID)
        if isinstance(block_id, str) and block_id:
            if block_id in self.seen_ids:
                return
            self.seen_ids.add(block_id)
        self.names.append(name)


# Codex has no dedicated ``Skill`` tool the way claude does -- its skill
# mechanism is file-based. Codex's own instructions tell the agent "After
# deciding to use a skill, open its SKILL.md," so the only trigger observable
# on the ``codex exec --json`` stream is a ``command_execution`` item whose
# shell ``command`` reads a ``skills/<name>/SKILL.md`` path. A captured
# reviewer run pinned this shape: there is NO ``Skill``-named function call
# and NO dedicated ``*skill*`` event.
#
# Only the ``<name>`` path segment is ever captured -- never the surrounding
# command text nor the command's ``aggregated_output`` (which carries the
# file's contents), both of which can echo issue / user content (names-only
# Privacy contract). The pattern is anchored to the literal
# ``skills/<name>/SKILL.md`` path shape and requires ``skills`` to sit on a
# path-component boundary (``(?<!\w)``), so an ordinary ``git`` / ``grep``
# command does not false-positive and ``myskills/...`` is not mistaken for a
# skills root. Nested built-in skills such as ``skills/.system/imagegen/...``
# do not match because their ``SKILL.md`` is not directly under ``skills/``.
_CODEX_SKILL_PATH_RE = re.compile(r"(?<!\w)skills/([^/\s\"']+)/SKILL\.md\b")


def parse_codex_skills(stdout: str) -> SkillTriggers:
    """Extract triggered skills from a ``codex exec --json`` run.

    Codex's skill mechanism is file-based, not a tool call: a real reviewer
    capture confirmed the only observable trigger is a ``command_execution``
    item whose ``command`` opens a skill's ``skills/<name>/SKILL.md`` file. We
    read only the ``<name>`` path segment
    (``_CODEX_SKILL_PATH_RE``) -- never the command text or its
    ``aggregated_output`` (the file's contents) -- honoring the names-only
    Privacy contract.

    Codex emits both an ``item.started`` and an ``item.completed`` for one
    command, each echoing the same ``command``; grouping by the shared
    ``item.id`` and keeping the last occurrence (the same last-frame-wins
    discipline ``parse_codex_usage`` / ``parse_claude_skills`` use) counts a
    single SKILL.md read once rather than twice. Two *separate* reads of the
    same skill (distinct ``item.id``s) still count as two triggers, mirroring
    the claude path.

    A run that opens no SKILL.md -- e.g. a normal usage-only run -- returns an
    empty ``SkillTriggers`` without raising. The signal is heuristic: opening a
    SKILL.md is the trigger codex's own instructions prescribe, but a run that
    reads a SKILL.md for an unrelated reason (e.g. reviewing a PR that edits
    one) would also register; that limitation is inherent to the heuristic.
    """
    collector = _CodexSkillCollector()
    for event in _iter_events(stdout):
        collector.add_event(event)
    return _collect(collector.names())


@dataclass
class _CodexSkillCollector:
    by_id: dict[str, list[str]] = field(default_factory=dict)
    id_order: list[str] = field(default_factory=list)
    anonymous: list[str] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        stream_item = event.get(_ITEM_KEY)
        if not isinstance(stream_item, dict) or stream_item.get(_TYPE) != _COMMAND_EXECUTION:
            return
        command = stream_item.get("command")
        if not isinstance(command, str):
            return
        names = _CODEX_SKILL_PATH_RE.findall(command)
        if not names:
            return
        item_id = stream_item.get(_ID)
        if isinstance(item_id, str) and item_id:
            if item_id not in self.by_id:
                self.id_order.append(item_id)
            self.by_id[item_id] = names
        else:
            self.anonymous.extend(names)

    def names(self) -> list[str]:
        ordered = [
            name for item_id in self.id_order for name in self.by_id[item_id]
        ]
        return ordered + self.anonymous


def parse_agent_skills(backend: str, stdout: str) -> SkillTriggers:
    """Dispatch by backend name; raise on anything other than claude/codex.

    Mirrors ``parse_agent_usage``'s dispatch contract so callers can reuse the
    same backend string they spawned the agent with.
    """
    if backend == _CLAUDE:
        return parse_claude_skills(stdout)
    if backend == _CODEX:
        return parse_codex_skills(stdout)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'"
    )
