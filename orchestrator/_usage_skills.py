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
``orchestrator.usage`` for callers (``analytics``). The sibling
``_usage_trajectory`` classifier reuses the offered-set init-frame helpers
(``_claude_init_field`` / ``_ordered_unique_names``) and the shared
skill/trajectory JSONL vocabulary (``_CONTENT_KEY`` / ``_COMMAND_EXECUTION``)
defined here, so the init-frame parsing and resilience contract stay defined
once.
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


_FoldedCounts = tuple[list[str], dict[str, int]]
_CommandSkillNames = tuple[list[str], list[str]]
_CodexTokenClassification = tuple[list[str], list[str], bool]


# Skill/trajectory JSONL protocol field and message-type values this module
# reads on top of the shared usage vocabulary re-used from ``_usage_metrics``.
# The sibling ``_usage_trajectory`` classifier re-imports both.
_CONTENT_KEY = "content"
_COMMAND_EXECUTION = "command_execution"


# Evidence tiers for a recorded skill observation. A claude ``Skill`` tool call
# is a *confirmed* load (the firm signal); a codex command that directly reads a
# skill's ``SKILL.md`` is an *inferred* load (codex's file-based mechanism, a
# heuristic). Both become triggers and carry their tier in
# ``SkillTriggers.evidence``. A path-only inspection / search reference (a
# ``git diff`` / ``git status`` / ``rg`` that merely names a ``SKILL.md``) is
# *incidental* -- it never becomes a trigger, so it lives in the separate
# ``incidental`` bucket and carries no ``evidence`` entry.
_EVIDENCE_CONFIRMED = "confirmed"
_EVIDENCE_INFERRED = "inferred"


# --- skill-trigger extractor ------------------------------------------------


@dataclass(frozen=True)
class SkillTriggers:
    """Which agent skills a single run loaded or incidentally referenced.

    ``triggered`` lists the distinct *loaded* skill names in first-seen order;
    ``trigger_counts`` maps each name to how many times it fired, so a run
    that pulls ``develop`` in twice records ``{"develop": 2}`` while
    ``triggered`` still carries it once. ``evidence`` records why each
    triggered name counts as a load: ``"confirmed"`` for a claude ``Skill``
    tool call (the firm signal) or ``"inferred"`` for a codex command that
    directly reads the skill's ``SKILL.md`` (codex's file-based mechanism, a
    heuristic). A single run is homogeneous -- claude only confirms, codex only
    infers -- so every entry shares one tier.

    ``incidental`` / ``incidental_counts`` are the *path-only* references a
    codex run makes to a ``SKILL.md`` without reading it -- an inspection or
    search command such as ``git diff`` / ``git status`` / ``rg`` that names
    the path as an argument, or any other non-reader command. They are
    deliberately excluded from ``triggered`` / ``trigger_counts`` (and thus
    from the ``skill_triggered`` audit events) so a bystander mention is never
    miscounted as a load, but are still recorded and counted in their own
    bucket -- independently of the loads, so a skill that was both read and
    inspected keeps both its trigger and its incidental count. Claude produces
    none -- its loads come through the ``Skill`` tool, not the shell.

    ``available`` is the *offered*-skills set: on claude it is read from the
    dedicated ``skills`` array in the ``system``/``init`` frame, confirmed
    against a captured real stream; on codex it stays best-effort and empty
    until that stream's field is confirmed. It varies independently of
    ``triggered`` and is empty -- never an error -- when the frame or field is
    absent.

    Only the skill *name* is ever read: the ``Skill`` tool's ``input`` can
    carry an ``args`` string, and a codex ``command`` can echo issue or user
    content, and neither the args nor the surrounding command text (nor the
    command's ``aggregated_output``, the file's contents) is ever touched
    (Privacy, same doc). A missing or renamed field yields an empty result,
    never an exception -- the same resilience contract the usage parsers in
    ``_usage_metrics`` honor.
    """

    triggered: tuple[str, ...] = ()
    trigger_counts: dict[str, int] = field(default_factory=dict)
    available: tuple[str, ...] = ()
    evidence: dict[str, str] = field(default_factory=dict)
    incidental: tuple[str, ...] = ()
    incidental_counts: dict[str, int] = field(default_factory=dict)


def _fold_counts(names: Iterable[str]) -> _FoldedCounts:
    """Fold first-seen names into (order, name -> count)."""
    order: list[str] = []
    counts: dict[str, int] = {}
    for name in names:
        if name not in counts:
            order.append(name)
            counts[name] = 0
        counts[name] += 1
    return order, counts


def _collect(
    names: Iterable[str],
    *,
    evidence_tier: str,
    available: Iterable[str] = (),
    incidental_names: Iterable[str] = (),
) -> SkillTriggers:
    """Fold first-seen skill names into the de-duplicated / counted shape.

    ``names`` are the run's loaded skills; ``evidence_tier`` is the single
    tier that classifies them all (a run is homogeneous -- see
    ``SkillTriggers``) and is recorded per name in ``evidence``.
    ``incidental_names`` are path-only references folded into the separate
    ``incidental`` / ``incidental_counts`` buckets. They are recorded
    independently of the loads -- a skill that was both loaded *and* inspected
    keeps its incidental count -- so the only exclusion is structural: an
    incidental reference never enters ``triggered`` / ``trigger_counts`` (and
    thus never fires a ``skill_triggered`` audit event). ``available`` is
    passed through verbatim (already de-duplicated by the caller) so the
    offered set rides the same constructor; codex callers omit it and it
    defaults to empty.
    """
    order, counts = _fold_counts(names)
    incidental_order, incidental_counts = _fold_counts(incidental_names)
    return SkillTriggers(
        triggered=tuple(order),
        trigger_counts=counts,
        available=tuple(available),
        evidence={name: evidence_tier for name in order},
        incidental=tuple(incidental_order),
        incidental_counts=incidental_counts,
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
    first-seen order (never ``input.args`` -- Privacy). Every such call is a
    *confirmed* load (the firm signal), so the whole triggered set carries
    ``"confirmed"`` evidence and claude produces no incidental references.

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
        evidence_tier=_EVIDENCE_CONFIRMED,
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
# deciding to use a skill, open its SKILL.md," so the only load observable on
# the ``codex exec --json`` stream is a ``command_execution`` item whose shell
# ``command`` reads a ``skills/<name>/SKILL.md`` path. A captured reviewer run
# pinned this shape: there is NO ``Skill``-named function call and NO dedicated
# ``*skill*`` event.
#
# Only the ``<name>`` path segment is ever captured -- never the surrounding
# command text nor the command's ``aggregated_output`` (which carries the
# file's contents), both of which can echo issue / user content (names-only
# Privacy contract). The pattern is anchored to the literal
# ``skills/<name>/SKILL.md`` path shape and requires ``skills`` to sit on a
# path-component boundary (``(?<!\w)``), so ``myskills/...`` is not mistaken for
# a skills root. Nested built-in skills such as ``skills/.system/imagegen/...``
# do not match because their ``SKILL.md`` is not directly under ``skills/``. A
# command that merely names the path without reading it (``git diff``/``rg``, an
# output-redirect target, ``sed -i``) still matches the shape, so
# ``_classify_codex_command`` routes each match to an inferred load only when
# its sub-command is a direct read of a plain-argument path, and to an
# incidental reference otherwise.
_CODEX_SKILL_PATH_RE = re.compile(r"(?<!\w)skills/([^/\s\"']+)/SKILL\.md\b")

# The shell wrapper a codex command is spawned through
# (``/bin/bash -lc "<script>"``, ``sh -c '<script>'``). The match ends right
# before the quoted ``-lc`` argument so ``_unwrap_codex_command`` can *decode*
# that quoted string back to the inner script bash actually runs -- preserving
# the script's own quoting rather than blindly stripping the outer quotes.
_CODEX_SHELL_WRAPPER_RE = re.compile(
    r"^\s*(?:\S*/)?(?:ba)?sh\s+-[a-z]*c\s+",
)

# A leading ``NAME=value`` environment assignment (``GIT_PAGER=cat git diff``);
# skipped when reading a sub-command's verb so the verb is the program that
# runs, not an env prefix that would masquerade as a reader.
_CODEX_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A leading output redirection on a token (``>`` / ``>>`` / ``>|`` / ``2>`` /
# ``&>``). A ``SKILL.md`` that is a redirect *target* is being written, not
# read, so it is an incidental reference regardless of the command's verb.
_CODEX_OUTPUT_REDIRECT_RE = re.compile(r"^(?:\d+|&)?>>?\|?")

# ``sed``'s in-place edit flag (``-i`` / ``-i.bak`` / ``--in-place``): the
# reader writes the file rather than dumping it, so it is not a load.
_CODEX_SED_INPLACE_RE = re.compile(r"^(?:-[a-zA-Z]*i|--in-place)")

# Verbs established as *direct readers* -- they dump a file's contents to
# stdout, which is how codex's "open its SKILL.md" instruction surfaces. A
# ``SKILL.md`` reference owned by one of these is an inferred load; every other
# verb -- an inspection / search (``git diff``/``rg``/``grep``/``ls``), an
# env-prefixed inspection, or a generic path-only command (``echo``) -- is only
# an incidental reference. Defaulting non-readers to incidental keeps the
# inferred signal conservative: an unrecognized command that merely names the
# path never fabricates a load or a ``skill_triggered`` audit event. Editors
# and other write-capable programs are deliberately excluded -- they can modify
# the file rather than load it, the same reason the redirect / ``sed -i``
# guards demote a reader whose SKILL.md is a write target.
_CODEX_READER_VERBS = frozenset((
    "cat", "sed", "head", "tail", "less", "more", "bat", "nl",
))


def _read_quoted(text: str) -> str:
    r"""Decode the leading shell-quoted string of ``text``, dropping the quotes.

    ``text`` starts at the opening quote. A single-quoted string is literal up
    to the next ``'``; a double-quoted string decodes the escapes bash honors
    there (``\\"`` -> ``"``, ``\\\\`` -> ``\\``, ``\\$`` -> ``$``, and an escaped
    backtick), so an escaped quote does not close it. Returns the decoded
    content -- the inner script ``bash -lc`` actually runs -- and, when the
    quote is never closed, the remainder as-is (fail-open on a truncated
    stream).
    """
    quote = text[0]
    decoded: list[str] = []
    index = 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == quote:
            return "".join(decoded)
        if quote == '"' and char == "\\" and index + 1 < length \
                and text[index + 1] in ('"', "\\", "$", "`"):
            decoded.append(text[index + 1])
            index += 2
            continue
        decoded.append(char)
        index += 1
    return "".join(decoded)


def _unwrap_codex_command(command: str) -> str:
    r"""Peel a ``bash -lc "<script>"`` wrapper, returning the inner script.

    A codex ``command`` is usually a shell wrapper around the real script; the
    outer quote spans the whole script, so classifying the raw command would
    treat the inner operators / quotes wrongly (an escaped inner quote such as
    ``\\"`` would look like plain text). When the ``-lc`` argument is a quoted
    string, ``_read_quoted`` decodes it back to the script bash runs -- with its
    own quoting intact -- so ``_split_codex_segments`` sees the real operators.
    An unquoted payload, or a command with no recognizable wrapper, is returned
    unchanged (already one bare command).
    """
    match = _CODEX_SHELL_WRAPPER_RE.match(command)
    if match is None:
        return command
    rest = command[match.end():]
    if rest[:1] in ("'", '"'):
        return _read_quoted(rest)
    return command


@dataclass
class _ShellSegmentScanner:
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
        if self._skip_escaped_char():
            return
        char = self.script[self.index]
        if self.quote:
            if char == self.quote:
                self.quote = ""
            self.index += 1
            return
        if char in ("'", '"'):
            self.quote = char
            self.index += 1
            return
        operator_width = self._operator_width()
        if operator_width:
            self.segments.append(self.script[self.segment_start:self.index])
            self.index += operator_width
            self.segment_start = self.index
            return
        self.index += 1

    def _skip_escaped_char(self) -> bool:
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


def _split_codex_segments(script: str) -> list[str]:
    r"""Split a shell script into sub-commands on *unquoted* control operators.

    Walks the string tracking single / double quote state so a shell
    metacharacter inside a quoted argument (``rg 'foo|bar' path``) does not
    split the command -- only an unquoted ``&&`` / ``||`` / ``;`` / ``|`` /
    newline separates one sub-command from the next. A backslash escapes the
    next character outside single quotes (shell rules), so an escaped operator
    (``rg foo\\|cat path``) is a literal argument char and does not split, and a
    ``\\"`` inside a double-quoted string does not close the quote; single
    quotes take no escapes. Runs on the inner script (after
    ``_unwrap_codex_command``) so the wrapper's own quotes never swallow the
    operators.
    """
    return _ShellSegmentScanner(script).split()


def _codex_reads(tokens: list[str]) -> bool:
    """Whether a sub-command's leading verb is a direct read of its arguments.

    Skips leading ``NAME=value`` env assignments (so ``GIT_PAGER=cat git diff``
    reads as ``git``, not ``cat``), then checks the verb's basename
    (``/bin/sed`` -> ``sed``) against ``_CODEX_READER_VERBS``. A reader in a
    non-reading mode -- ``sed -i`` / ``--in-place`` edits the file rather than
    dumping it -- is not a read. Used only to route a ``SKILL.md`` reference;
    nothing is stored.
    """
    for index, token in enumerate(tokens):
        if _CODEX_ENV_ASSIGNMENT_RE.match(token):
            continue
        verb = token.rsplit("/", 1)[-1]
        if verb not in _CODEX_READER_VERBS:
            return False
        args = tokens[index + 1:]
        if verb == "sed" and any(_CODEX_SED_INPLACE_RE.match(arg) for arg in args):
            return False
        return True
    return False


def _classify_codex_segment(
    segment: str, inferred: list[str], incidental: list[str],
) -> None:
    """Route one sub-command's ``SKILL.md`` matches into the two buckets.

    A match is an inferred load only when the sub-command reads its arguments
    (``_codex_reads``) *and* the match is a plain argument -- a match that is an
    output-redirect *target* (``cat t > .agents/skills/x/SKILL.md``) is being
    written, so it is incidental regardless of the verb. Everything else -- an
    inspection / search, an env-prefixed inspection, a generic path-only
    command -- is an incidental reference. Names keep first-seen order.
    """
    _extend_codex_classification(segment.split(), inferred, incidental)


def _extend_codex_classification(
    tokens: list[str], inferred: list[str], incidental: list[str],
) -> None:
    reads = _codex_reads(tokens)
    previous_redirect = False
    for token in tokens:
        loaded, referenced, previous_redirect = _classify_codex_token(
            token, reads, previous_redirect,
        )
        inferred.extend(loaded)
        incidental.extend(referenced)


def _classify_codex_token(
    token: str, reads: bool, previous_redirect: bool,
) -> _CodexTokenClassification:
    redirect_match = _CODEX_OUTPUT_REDIRECT_RE.match(token)
    redirect_end = -1
    if redirect_match:
        redirect_end = redirect_match.end()
    names = _CODEX_SKILL_PATH_RE.findall(token)
    is_target = previous_redirect or 0 <= redirect_end < len(token)
    next_is_redirect = redirect_end == len(token)
    if is_target:
        return [], names, next_is_redirect
    if reads:
        return names, [], next_is_redirect
    return [], names, next_is_redirect


def _classify_codex_command(command: str) -> tuple[list[str], list[str]]:
    """Split a codex ``command`` into (inferred, incidental) skill names.

    The command is unwrapped (decoding the ``bash -lc "..."`` shell back to its
    inner script) and split into sub-commands on unquoted operators; each
    ``skills/<name>/SKILL.md`` match is then routed by
    ``_classify_codex_segment`` -- an inferred load only for a direct read (an
    established reader verb, not a write mode) of a plain-argument path, an
    incidental reference for an inspection / search, an output-redirect target,
    an env-prefixed inspection, or any generic path-only command. Only the
    ``<name>`` segment and the routing verb / operators are read -- never the
    surrounding command text (names-only Privacy).
    """
    inferred: list[str] = []
    incidental: list[str] = []
    for segment in _split_codex_segments(_unwrap_codex_command(command)):
        _classify_codex_segment(segment, inferred, incidental)
    return inferred, incidental


def parse_codex_skills(stdout: str) -> SkillTriggers:
    """Extract triggered + incidental skills from a ``codex exec --json`` run.

    Codex's skill mechanism is file-based, not a tool call: a real reviewer
    capture confirmed the only observable load is a ``command_execution`` item
    whose ``command`` opens a skill's ``skills/<name>/SKILL.md`` file. We read
    only the ``<name>`` path segment (``_CODEX_SKILL_PATH_RE``) -- never the
    command text or its ``aggregated_output`` (the file's contents) -- honoring
    the names-only Privacy contract.

    A command that *directly reads* the file with an established reader verb
    (``cat``/``sed``/...) is an inferred load and lands in ``triggered``; every
    other command that merely names the path -- an inspection / search
    (``git diff``/``git status``/``rg``), an env-prefixed inspection, or a
    generic path-only command (``echo``) -- is an incidental reference and
    lands in ``incidental`` instead (``_classify_codex_command``), excluded
    from the trigger set so a bystander mention -- e.g. reviewing a PR that
    changes a ``SKILL.md`` -- is not miscounted as a load. The command is
    unwrapped and split quote-aware, so a metacharacter inside a quoted
    argument (``rg 'foo|bar' path``) does not fabricate a spurious segment.

    Codex emits both an ``item.started`` and an ``item.completed`` for one
    command, each echoing the same ``command``; grouping by the shared
    ``item.id`` and keeping the last occurrence (the same last-frame-wins
    discipline ``parse_codex_usage`` / ``parse_claude_skills`` use) counts a
    single SKILL.md read once rather than twice, for inferred loads and
    incidental references alike. Two *separate* reads of the same skill
    (distinct ``item.id``s) still count as two, mirroring the claude path.

    A run that opens no SKILL.md -- e.g. a normal usage-only run -- returns an
    empty ``SkillTriggers`` without raising. The inferred signal stays
    heuristic within the reader allowlist: reading a SKILL.md is the load
    codex's own instructions prescribe, but a reader that opens one for an
    unrelated reason (e.g. ``cat``-ing a SKILL.md a PR edits) would still
    register as inferred.
    """
    collector = _CodexSkillCollector()
    for event in _iter_events(stdout):
        collector.add_event(event)
    return _collect(
        collector.inferred_names(),
        evidence_tier=_EVIDENCE_INFERRED,
        incidental_names=collector.incidental_names(),
    )


@dataclass
class _CodexSkillCollector:
    # item.id -> (inferred names, incidental names) for that command; the last
    # frame per id wins (started/completed echo the same command).
    by_id: dict[str, _CommandSkillNames] = field(default_factory=dict)
    id_order: list[str] = field(default_factory=list)
    anon_inferred: list[str] = field(default_factory=list)
    anon_incidental: list[str] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        stream_item = event.get(_ITEM_KEY)
        if not isinstance(stream_item, dict) or stream_item.get(_TYPE) != _COMMAND_EXECUTION:
            return
        command = stream_item.get("command")
        if not isinstance(command, str):
            return
        inferred, incidental = _classify_codex_command(command)
        if not inferred and not incidental:
            return
        item_id = stream_item.get(_ID)
        if isinstance(item_id, str) and item_id:
            if item_id not in self.by_id:
                self.id_order.append(item_id)
            self.by_id[item_id] = (inferred, incidental)
        else:
            self.anon_inferred.extend(inferred)
            self.anon_incidental.extend(incidental)

    def inferred_names(self) -> list[str]:
        return self._ordered(0) + self.anon_inferred

    def incidental_names(self) -> list[str]:
        return self._ordered(1) + self.anon_incidental

    def _ordered(self, bucket: int) -> list[str]:
        return [
            name for item_id in self.id_order for name in self.by_id[item_id][bucket]
        ]


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
