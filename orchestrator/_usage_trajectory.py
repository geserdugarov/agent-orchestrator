# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct agent run trajectories from their JSONL stdout.

A trajectory classifier (``parse_claude_trajectory`` /
``parse_codex_trajectory`` / ``parse_agent_trajectory``) reuses
``_usage_metrics``'s shared event iterator and resilience contract to
reconstruct a run's *trajectory*: the offered tools, triggered skills, the
ordered timeline of ``tool_call`` / ``tool_result`` steps interleaved with
``assistant_message`` / ``user_message`` text turns, and the final output. For
claude it also emits per-turn token usage (``TurnUsage``, one per assistant
``message.id``) and stamps each step with the ``turn`` index that produced it;
codex leaves the per-turn section empty (its usage frames are cumulative, not
per-turn). It classifies raw stream data only -- it neither writes files nor
redacts/truncates content; a downstream writer owns that. ``system_prompt``
and ``tools`` stay best-effort and empty when a backend's stream does not
expose them.

This module is the private home of the trajectory parsing. Its public surface
-- the ``TrajectoryStep`` / ``TurnUsage`` / ``AgentTrajectory`` dataclasses and
the ``parse_claude_trajectory`` / ``parse_codex_trajectory`` /
``parse_agent_trajectory`` trio -- is re-exported from ``orchestrator.usage``
for callers (``analytics``). It reuses ``_usage_metrics``'s shared event
iterator, token decoders, and ``_claude_estimate_cost`` price path plus
``_usage_skills``'s offered-set init-frame helpers (``_claude_init_field`` /
``_ordered_unique_names``) and shared skill/trajectory JSONL vocabulary
(``_CONTENT_KEY`` / ``_COMMAND_EXECUTION``), so the resilience contract, cost
precedence, and init-frame parsing stay defined once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Shared event iterator, token decoders, price path, and the per-call token
# bucket alias the trajectory classifier below reuses (keeping the resilience
# contract and cost path defined once, in ``_usage_metrics``).
from orchestrator._usage_metrics import (
    _TokenBucket,
    _claude_estimate_cost,
    _claude_model_name,
    _claude_usage_record,
    _iter_events,
)
# Shared JSONL protocol field, message-type, and backend vocabulary.
from orchestrator._usage_metrics import (
    _ASSISTANT,
    _CACHE_READ,
    _CACHE_WRITE_FIVE_MIN,
    _CACHE_WRITE_ONE_HOUR,
    _CLAUDE,
    _CODEX,
)
from orchestrator._usage_metrics import (
    _ID,
    _INPUT,
    _INPUT_TOKENS,
    _ITEM_KEY,
    _MESSAGE,
    _MODEL,
)
from orchestrator._usage_metrics import (
    _OUTPUT,
    _OUTPUT_TOKENS,
    _RESULT_KEY,
    _TYPE,
    _USAGE,
)
# The trajectory classifier folds each backend's names-only ``SkillTriggers``
# into its output, so it drives the skill-trigger extractor directly rather
# than through the ``orchestrator.usage`` facade (which imports this module).
from orchestrator._usage_skills import (
    SkillTriggers,
    parse_claude_skills,
    parse_codex_skills,
)
# Offered-set init-frame helpers and the shared skill/trajectory JSONL
# vocabulary the trajectory classifier below reuses (keeping the init-frame
# parsing defined once, in ``_usage_skills``).
from orchestrator._usage_skills import (
    _COMMAND_EXECUTION,
    _CONTENT_KEY,
    _claude_init_field,
    _ordered_unique_names,
)


# Trajectory-only JSONL protocol field and sentinel values; ``_CONTENT_KEY`` /
# ``_COMMAND_EXECUTION`` come from ``_usage_skills``, which reads them too.
_TEXT = "text"
_TOOL_RESULT = "tool_result"
# Distinguishes an absent per-id entry from one whose recorded value is None
# (a codex item can carry `aggregated_output: null`).
_MISSING = object()

# One built claude turn's row: its 0-based turn index, model name, and decoded
# token bucket -- the shape ``_ClaudeTurnUsageBuilder`` keys by turn key before
# folding each row into a ``TurnUsage`` record.
_TurnUsageRow = tuple[int, str, _TokenBucket]


# --- trajectory classifier --------------------------------------------------


@dataclass(frozen=True)
class TrajectoryStep:
    """One ordered step in an agent run: a tool call, its result, or a
    free-text message turn.

    ``kind`` is ``"tool_call"``, ``"tool_result"``, ``"assistant_message"``
    (an assistant text turn -- claude's ``text`` block / codex's
    ``agent_message``) or ``"user_message"`` (a claude ``user`` text turn).
    ``name`` carries the tool name on a call (claude's ``tool_use.name``;
    codex's synthetic ``"command_execution"``) and is empty on results and
    message turns -- a result joins back to its call through ``tool_id``
    (claude's ``tool_use`` ``id`` / ``tool_use_id``; codex's ``item.id``),
    which is ``""`` when the stream omits it and on message turns.
    ``turn`` is the 0-based index of the assistant turn that produced the step
    (claude only): the ``assistant_message`` and ``tool_call`` steps of one
    ``message.id`` share it, matching the ``TurnUsage`` that billed them.
    ``tool_result`` / ``user_message`` steps are turn *inputs*, not billed
    output, so ``turn`` is ``None``; it is also ``None`` for every codex step.
    ``content`` is the raw, un-redacted payload exactly as the stream carried
    it: a call's input (claude input dict / codex command string), a result's
    output (claude result content / codex ``aggregated_output``), or a message
    turn's text string. Redaction and truncation are a downstream writer's
    job, not this classifier's.
    """

    kind: str
    name: str = ""
    tool_id: str = ""
    turn: Optional[int] = None
    content: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "tool_id": self.tool_id,
            "turn": self.turn,
            _CONTENT_KEY: self.content,
        }


@dataclass(frozen=True)
class TurnUsage:
    """Per-turn token usage for one claude assistant turn (one ``message.id``).

    A turn is one LLM request: its ``text`` block plus any ``tool_use`` blocks
    share a single ``message.usage`` record, so usage is attached once at the
    turn boundary rather than copied onto every step the turn emitted. ``turn``
    is the same 0-based index the sibling steps carry in
    ``TrajectoryStep.turn``; ``cache_write_tokens`` sums the 5m and 1h cache-
    creation buckets. ``cost_usd`` is always an *estimate* from the shared
    price path -- ``total_cost_usd`` is a run-level terminal figure with no
    per-turn breakdown -- so ``cost_source`` is only ever ``"estimated"`` or
    ``"unknown-price"`` (the latter with ``cost_usd = None``). Codex has no
    per-turn usage (its frames are cumulative), so it produces none of these.
    """

    turn: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "estimated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            _MODEL: self.model,
            _INPUT_TOKENS: self.input_tokens,
            _OUTPUT_TOKENS: self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
        }


@dataclass(frozen=True)
class AgentTrajectory:
    """Structured trajectory reconstructed from one agent run's JSONL stdout.

    ``steps`` is the ordered timeline -- ``tool_call`` / ``tool_result``
    steps interleaved with ``assistant_message`` / ``user_message`` text
    turns, in stream order; ``final_output`` is the run's terminal answer
    (claude's ``result`` frame ``result`` string / codex's last
    ``agent_message`` ``text``), ``None`` when the stream carries none. ``skills`` is the same names-only
    ``SkillTriggers`` the skill extractor produces. ``tools`` is the offered-
    tools set when a backend exposes one in its stream (claude's
    ``system``/``init`` ``tools`` array) and empty otherwise -- codex exposes
    none, so a downstream writer backfills it out-of-band; ``system_prompt``
    stays ``None`` until a backend's stream is confirmed to carry it -- both
    are best-effort and empty when the stream shape is unknown rather than an
    error.

    ``turns`` is the per-turn token-usage breakdown -- one ``TurnUsage`` per
    assistant turn, parallel to the ``tools`` / ``skills`` best-effort
    sections. It is claude-only (codex's usage frames are cumulative, so it
    stays empty), and every step's ``turn`` index refers into it.

    This is a *classifier*: it records raw stream data verbatim and never
    writes files or redacts/truncates. A missing or renamed field yields an
    empty section, never an exception -- the same resilience contract the
    usage and skill parsers honor.
    """

    backend: str
    system_prompt: Optional[str] = None
    tools: tuple[str, ...] = ()
    skills: SkillTriggers = field(default_factory=SkillTriggers)
    steps: tuple[TrajectoryStep, ...] = ()
    final_output: Optional[str] = None
    turns: tuple[TurnUsage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "system_prompt": self.system_prompt,
            "tools": list(self.tools),
            "skills": {
                "triggered": list(self.skills.triggered),
                "trigger_counts": dict(self.skills.trigger_counts),
                "available": list(self.skills.available),
            },
            "steps": [step.to_dict() for step in self.steps],
            "final_output": self.final_output,
            "turns": [turn_usage.to_dict() for turn_usage in self.turns],
        }


# --- claude trajectory ------------------------------------------------------

def _claude_offered_tools(events: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Read the offered-tools set from claude's ``system``/``init`` frame.

    The headless ``--output-format stream-json`` init frame carries a
    top-level ``tools`` array -- the tool names on offer to the session.
    Read defensively, mirroring ``_claude_offered_skills``: a missing or
    renamed field, or a non-string entry, filters out rather than raising,
    and names de-duplicate in first-seen order. The first ``init`` frame wins.
    """
    return _ordered_unique_names(_claude_init_field(events, "tools"))


def _claude_final_output(events: Iterable[dict[str, Any]]) -> Optional[str]:
    """Return the ``result`` frame's final answer string, else ``None``.

    Claude's terminal ``type:"result"`` frame carries the run's final text in
    its ``result`` field. The last such frame wins (a run emits one); a
    missing or non-string field yields ``None`` rather than raising.
    """
    final: Optional[str] = None
    for ev in events:
        if ev.get(_TYPE) != _RESULT_KEY:
            continue
        result_text = ev.get(_RESULT_KEY)
        if isinstance(result_text, str):
            final = result_text
    return final


def _claude_turn_key(idx: int, event: dict[str, Any]) -> str:
    """Turn-grouping key for an assistant frame: ``message.id``, else fallback.

    Mirrors ``parse_claude_usage``'s per-id grouping so a run's turns line up
    with its aggregate: partial-message frames sharing a ``message.id`` are one
    turn, and a frame with neither ``message.id`` nor ``request_id`` falls back
    to its stream position (each such frame its own turn). Computed identically
    in ``_claude_trajectory_steps`` and ``_claude_turn_usage`` so a step's
    ``turn`` index and its ``TurnUsage`` always agree.
    """
    msg = event.get(_MESSAGE)
    mid = msg.get(_ID) if isinstance(msg, dict) else None
    if isinstance(mid, str) and mid:
        return mid
    rid = event.get("request_id")
    if isinstance(rid, str) and rid:
        return rid
    return str(idx)


def _claude_assistant_steps(
    blocks: list[Any], turn: Optional[int], seen_calls: set[str],
) -> list[TrajectoryStep]:
    """Steps from one assistant frame's content: ``text`` + ``tool_use`` blocks.

    ``text`` blocks become ``assistant_message`` turns; ``tool_use`` blocks
    become ``tool_call`` steps de-duplicated by the block ``id`` (defensive
    against ``--include-partial-messages`` re-emitting a block across frames --
    ``seen_calls`` carries that state across the whole stream). Every step
    carries the frame's ``turn`` index. The raw ``input`` / ``text`` payload
    rides along verbatim (no redaction here).
    """
    steps: list[TrajectoryStep] = []
    for block in blocks:
        step = _claude_assistant_step(block, turn, seen_calls)
        if step is not None:
            steps.append(step)
    return steps


def _claude_assistant_step(
    block: Any, turn: Optional[int], seen_calls: set[str],
) -> Optional[TrajectoryStep]:
    if not isinstance(block, dict):
        return None
    if block.get(_TYPE) == _TEXT:
        return _claude_message_step(block, "assistant_message", turn=turn)
    if block.get(_TYPE) == "tool_use":
        return _claude_tool_call_step(block, turn, seen_calls)
    return None


def _claude_message_step(
    block: dict[str, Any], kind: str, *, turn: Optional[int] = None,
) -> Optional[TrajectoryStep]:
    message = block.get(_TEXT)
    if not isinstance(message, str) or not message:
        return None
    return TrajectoryStep(kind=kind, turn=turn, content=message)


def _claude_tool_call_step(
    block: dict[str, Any], turn: Optional[int], seen_calls: set[str],
) -> Optional[TrajectoryStep]:
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return None
    block_id = block.get(_ID)
    tool_id = block_id if isinstance(block_id, str) and block_id else ""
    if tool_id in seen_calls:
        return None
    if tool_id:
        seen_calls.add(tool_id)
    return TrajectoryStep(
        kind="tool_call",
        name=name,
        tool_id=tool_id,
        turn=turn,
        content=block.get(_INPUT),
    )


def _claude_user_steps(
    blocks: list[Any], seen_results: set[str],
) -> list[TrajectoryStep]:
    """Steps from one user frame's content: ``text`` + ``tool_result`` blocks.

    ``text`` blocks become ``user_message`` turns; ``tool_result`` blocks
    become ``tool_result`` steps de-duplicated by ``tool_use_id`` (which also
    joins each back to its call). These are turn *inputs*, not billed output,
    so they carry no ``turn`` index. The raw ``content`` / ``text`` payload
    rides along verbatim (no redaction here).
    """
    steps: list[TrajectoryStep] = []
    for block in blocks:
        step = _claude_user_step(block, seen_results)
        if step is not None:
            steps.append(step)
    return steps


def _claude_user_step(
    block: Any, seen_results: set[str],
) -> Optional[TrajectoryStep]:
    if not isinstance(block, dict):
        return None
    if block.get(_TYPE) == _TEXT:
        return _claude_message_step(block, "user_message")
    if block.get(_TYPE) == _TOOL_RESULT:
        return _claude_tool_result_step(block, seen_results)
    return None


def _claude_tool_result_step(
    block: dict[str, Any], seen_results: set[str],
) -> Optional[TrajectoryStep]:
    result_id = block.get("tool_use_id")
    tool_id = result_id if isinstance(result_id, str) and result_id else ""
    if tool_id in seen_results:
        return None
    if tool_id:
        seen_results.add(tool_id)
    return TrajectoryStep(
        kind=_TOOL_RESULT, tool_id=tool_id, content=block.get(_CONTENT_KEY),
    )


@dataclass
class _ClaudeTrajectoryBuilder:
    steps: list[TrajectoryStep] = field(default_factory=list)
    seen_calls: set[str] = field(default_factory=set)
    seen_results: set[str] = field(default_factory=set)
    turn_index: dict[str, int] = field(default_factory=dict)

    def add_event(self, idx: int, event: dict[str, Any]) -> None:
        event_type = event.get(_TYPE)
        if event_type not in (_ASSISTANT, "user"):
            return
        message = event.get(_MESSAGE)
        if not isinstance(message, dict):
            return
        turn = self._turn(idx, event) if event_type == _ASSISTANT else None
        blocks = message.get(_CONTENT_KEY)
        if not isinstance(blocks, list):
            return
        if event_type == _ASSISTANT:
            self.steps.extend(
                _claude_assistant_steps(blocks, turn, self.seen_calls)
            )
        else:
            self.steps.extend(_claude_user_steps(blocks, self.seen_results))

    def _turn(self, idx: int, event: dict[str, Any]) -> int:
        return self.turn_index.setdefault(
            _claude_turn_key(idx, event), len(self.turn_index),
        )


def _claude_trajectory_steps(
    events: Iterable[dict[str, Any]],
) -> tuple[TrajectoryStep, ...]:
    """Reconstruct the ordered timeline from a claude stream.

    An ``assistant`` message contributes its ``text`` blocks as
    ``assistant_message`` turns and its ``tool_use`` blocks as ``tool_call``
    steps; a ``user`` message contributes its ``text`` blocks as
    ``user_message`` turns and its ``tool_result`` blocks as ``tool_result``
    steps, joined to their call by ``tool_use_id``. Steps follow stream order.
    Calls de-duplicate by the ``tool_use`` block ``id`` and results by
    ``tool_use_id`` -- defensive against ``--include-partial-messages``
    re-emitting a block across frames, the same discipline
    ``parse_claude_skills`` uses. Text blocks carry no block ``id``; claude's
    per-completed-block framing (the same capture ``parse_claude_skills``
    relies on) emits each text block in exactly one frame, so they append in
    order without an id de-dup. The raw ``input`` / ``content`` / ``text``
    payload rides along verbatim (no redaction here).

    Each assistant frame is assigned a 0-based ``turn`` index by first-seen
    ``message.id`` (``_claude_turn_key``); that index is stamped onto every
    ``assistant_message`` / ``tool_call`` step the frame emits so it lines up
    with the matching ``TurnUsage``. ``tool_result`` / ``user_message`` steps
    are turn inputs, not billed output, and keep ``turn = None``. The index is
    assigned right after the ``message`` check -- before the ``content`` check
    and independent of usage presence -- so it stays in lock-step with
    ``_claude_turn_usage``.
    """
    builder = _ClaudeTrajectoryBuilder()
    for idx, event in enumerate(events):
        builder.add_event(idx, event)
    return tuple(builder.steps)


@dataclass
class _ClaudeTurnUsageBuilder:
    turn_index: dict[str, int] = field(default_factory=dict)
    by_key: dict[str, _TurnUsageRow] = field(default_factory=dict)

    def add_event(self, idx: int, event: dict[str, Any]) -> None:
        if event.get(_TYPE) != _ASSISTANT:
            return
        message = event.get(_MESSAGE)
        if not isinstance(message, dict):
            return
        key = _claude_turn_key(idx, event)
        turn = self.turn_index.setdefault(key, len(self.turn_index))
        usage = message.get(_USAGE)
        if isinstance(usage, dict):
            self.by_key[key] = (
                turn, _claude_model_name(event), _claude_usage_record(usage),
            )

    def build(self) -> tuple[TurnUsage, ...]:
        return tuple(
            _turn_usage_from_row(row)
            for row in sorted(self.by_key.values(), key=lambda usage_row: usage_row[0])
        )


def _turn_usage_from_row(row: _TurnUsageRow) -> TurnUsage:
    turn, model, record = row
    cost = _claude_estimate_cost(model, record)
    return TurnUsage(
        turn=turn,
        model=model,
        input_tokens=record[_INPUT],
        output_tokens=record[_OUTPUT],
        cache_read_tokens=record[_CACHE_READ],
        cache_write_tokens=record[_CACHE_WRITE_FIVE_MIN] + record[_CACHE_WRITE_ONE_HOUR],
        cost_usd=cost,
        cost_source="unknown-price" if cost is None else "estimated",
    )


def _claude_turn_usage(
    events: Iterable[dict[str, Any]],
) -> tuple[TurnUsage, ...]:
    """Build one ``TurnUsage`` per assistant ``message.id``, first-seen order.

    Groups assistant frames by ``_claude_turn_key`` -- the same keying and
    order ``_claude_trajectory_steps`` uses -- so each turn's 0-based index
    matches the ``turn`` its steps carry. The last usage record per id wins
    (claude streams partial usage on intermediate frames; the final frame is
    authoritative), the same discipline ``parse_claude_usage`` applies. Per-
    turn cost is always an estimate from the shared ``_claude_estimate_cost``
    path -- ``estimated`` when the model is priced, ``unknown-price`` (with
    ``cost_usd = None``) otherwise; ``total_cost_usd`` is run-level only and
    never reaches a turn.
    """
    builder = _ClaudeTurnUsageBuilder()
    for idx, event in enumerate(events):
        builder.add_event(idx, event)
    return builder.build()


def parse_claude_trajectory(stdout: str) -> AgentTrajectory:
    """Classify a ``claude -p --output-format stream-json`` run's trajectory.

    Reuses ``_iter_events`` and ``parse_claude_skills`` (names-only) and
    reconstructs the offered tools, the ordered timeline (tool_call /
    tool_result steps interleaved with assistant_message / user_message text
    turns), per-turn token usage, and final output. ``system_prompt`` stays
    ``None`` -- the stream-json shape does not expose it -- and every section
    is empty rather than an error when its source frame/field is absent.
    """
    events = _iter_events(stdout)
    return AgentTrajectory(
        backend=_CLAUDE,
        tools=_claude_offered_tools(events),
        skills=parse_claude_skills(stdout),
        steps=_claude_trajectory_steps(events),
        final_output=_claude_final_output(events),
        turns=_claude_turn_usage(events),
    )


# --- codex trajectory -------------------------------------------------------

def _codex_final_output(events: Iterable[dict[str, Any]]) -> Optional[str]:
    """Return the last ``agent_message`` item's ``text``, else ``None``.

    Codex emits the run's answer as an ``agent_message`` item; the last one's
    ``text`` is the final output. A missing item or non-string ``text`` yields
    ``None`` rather than raising.
    """
    final: Optional[str] = None
    for ev in events:
        stream_item = ev.get(_ITEM_KEY)
        if not isinstance(stream_item, dict) or stream_item.get(_TYPE) != "agent_message":
            continue
        text = stream_item.get(_TEXT)
        if isinstance(text, str):
            final = text
    return final


def _codex_trajectory_steps(
    events: Iterable[dict[str, Any]],
) -> tuple[TrajectoryStep, ...]:
    """Reconstruct the ordered timeline from a codex stream.

    Codex's tool surface is the shell: each ``command_execution`` item is one
    call (its ``command``) and, once it completes, one result (its
    ``aggregated_output``); each ``agent_message`` item is one
    ``assistant_message`` text turn (its ``text``) -- the same items
    ``_codex_final_output`` reads, here also preserved in stream order. Codex
    emits an ``item.started`` then an ``item.completed`` for the same item
    sharing an ``item.id``; grouping by that id keeps a single item one step
    (or one call + one result) rather than two, the same last-frame-wins
    discipline the usage / skill parsers use. Items are emitted in first-seen
    id order; an item without an id falls back to inline emission. Raw command
    / output / message text rides along verbatim (no redaction here).
    """
    builder = _CodexTrajectoryBuilder()
    for event in events:
        builder.add_event(event)
    return builder.build()


@dataclass
class _CodexTrajectoryBuilder:
    order: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    commands: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    anonymous: list[TrajectoryStep] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        stream_item = event.get(_ITEM_KEY)
        if not isinstance(stream_item, dict):
            return
        item_id = self._item_id(stream_item)
        if stream_item.get(_TYPE) == _COMMAND_EXECUTION:
            self._add_command(stream_item, item_id)
        elif stream_item.get(_TYPE) == "agent_message":
            self._add_message(stream_item, item_id)

    def build(self) -> tuple[TrajectoryStep, ...]:
        return _codex_assemble_steps(
            self.order,
            self.commands,
            self.outputs,
            self.messages,
            self.anonymous,
        )

    def _item_id(self, stream_item: dict[str, Any]) -> str:
        raw_id = stream_item.get(_ID)
        item_id = raw_id if isinstance(raw_id, str) and raw_id else ""
        if item_id and item_id not in self.seen:
            self.seen.add(item_id)
            self.order.append(item_id)
        return item_id

    def _add_command(self, stream_item: dict[str, Any], item_id: str) -> None:
        command = stream_item.get("command")
        has_output = "aggregated_output" in stream_item
        if item_id:
            if isinstance(command, str):
                self.commands[item_id] = command
            if has_output:
                self.outputs[item_id] = stream_item.get("aggregated_output")
            return
        if isinstance(command, str):
            self.anonymous.append(TrajectoryStep(
                kind="tool_call", name=_COMMAND_EXECUTION, content=command,
            ))
        if has_output:
            self.anonymous.append(TrajectoryStep(
                kind=_TOOL_RESULT, content=stream_item.get("aggregated_output"),
            ))

    def _add_message(self, stream_item: dict[str, Any], item_id: str) -> None:
        message = stream_item.get(_TEXT)
        if not isinstance(message, str) or not message:
            return
        if item_id:
            self.messages[item_id] = message
        else:
            self.anonymous.append(TrajectoryStep(
                kind="assistant_message", content=message,
            ))


def _codex_assemble_steps(
    order: list[str],
    commands: dict[str, str],
    outputs: dict[str, Any],
    messages: dict[str, str],
    anon: list[TrajectoryStep],
) -> tuple[TrajectoryStep, ...]:
    """Emit steps in first-seen ``item.id`` order, then the inline anon steps.

    Each id yields up to its call (``command``), result (``aggregated_output``)
    and message (``text``) in that fixed order -- collapsing the ``item.started``
    / ``item.completed`` pair into one step (or one call + one result). The anon
    steps -- items that carried no id -- follow in the order they were seen.
    """
    steps: list[TrajectoryStep] = []
    for iid in order:
        command = commands.get(iid, _MISSING)
        if command is not _MISSING:
            steps.append(TrajectoryStep(
                kind="tool_call",
                name=_COMMAND_EXECUTION,
                tool_id=iid,
                content=command,
            ))
        output = outputs.get(iid, _MISSING)
        if output is not _MISSING:
            steps.append(TrajectoryStep(
                kind=_TOOL_RESULT,
                tool_id=iid,
                content=output,
            ))
        message = messages.get(iid, _MISSING)
        if message is not _MISSING:
            steps.append(TrajectoryStep(
                kind="assistant_message",
                content=message,
            ))
    steps.extend(anon)
    return tuple(steps)


def parse_codex_trajectory(stdout: str) -> AgentTrajectory:
    """Classify a ``codex exec --json`` run's trajectory.

    Reuses ``_iter_events`` and ``parse_codex_skills`` (names-only) and
    reconstructs the ordered timeline (command tool_call / tool_result steps
    interleaved with agent_message assistant_message turns) and final output;
    the last ``agent_message`` is still the ``final_output``. ``tools`` and
    ``system_prompt`` stay empty / ``None`` here -- codex's stream exposes no
    offered-tools or system-prompt frame -- so a downstream writer backfills
    ``tools`` out-of-band (see ``analytics._maybe_record_trajectory`` /
    ``skill_catalog.discover_codex_tools``); this stdout-only classifier does
    not. ``turns`` stays empty with every ``step.turn = None`` (codex usage
    frames are cumulative, not per-turn). Every section is empty rather than an
    error when its source is absent.
    """
    events = _iter_events(stdout)
    return AgentTrajectory(
        backend=_CODEX,
        skills=parse_codex_skills(stdout),
        steps=_codex_trajectory_steps(events),
        final_output=_codex_final_output(events),
    )


def parse_agent_trajectory(backend: str, stdout: str) -> AgentTrajectory:
    """Dispatch by backend name; raise on anything other than claude/codex.

    Mirrors ``parse_agent_usage`` / ``parse_agent_skills`` dispatch so callers
    reuse the same backend string they spawned the agent with.
    """
    if backend == _CLAUDE:
        return parse_claude_trajectory(stdout)
    if backend == _CODEX:
        return parse_codex_trajectory(stdout)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'"
    )
