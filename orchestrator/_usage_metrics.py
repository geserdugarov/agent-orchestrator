# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Parse usage metrics from agent JSONL stdout (claude / codex).

Inputs are the raw stdout strings that ``agents.AgentResult.stdout`` carries,
which are the same event streams ``agent-develop-review-loop``'s shell helpers
consume via jq. We extract per-call totals (input / output / cached /
cache-read / cache-write tokens), the model(s) involved, the number of turns,
and a ``cost_usd`` figure with a ``cost_source`` tag that records how it was
obtained:

  * ``reported``      - the agent itself emitted ``total_cost_usd``
  * ``estimated``     - computed from a first-party price table
  * ``unknown-price`` - usage was present but no rates known for the model
  * ``no-usage``      - the stream carried no usage records at all

The price tables match the rates in the shell-script reference and are
intentionally restricted to first-party Anthropic / OpenAI models -- an
unknown model name yields ``unknown-price`` rather than a guess, so a
silently-wrong cost cannot end up in analytics records.

Malformed JSONL lines (truncation, partial flushes, banner text) are
skipped silently; usage events buried inside otherwise-broken streams are
still picked up.

This module is the private home of the usage-metric parsing. Its public
surface -- ``UsageMetrics``, ``parse_claude_usage``, ``parse_codex_usage``,
and the ``parse_agent_usage`` dispatcher -- is re-exported from
``orchestrator.usage`` for callers. ``usage`` also reuses the shared event
iterator, token decoders, and price path defined here for its sibling
skill-trigger and trajectory extractors, so the resilience contract and the
cost precedence stay defined in one place.
"""
from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# Claude/Codex usage-JSONL protocol field names, message-type and backend
# values, and cost-config keys this parser reads.
_TYPE = "type"
_MESSAGE = "message"
_USAGE = "usage"
_ID = "id"
_MODEL = "model"
_INPUT = "input"
_OUTPUT = "output"
_INPUT_TOKENS = "input_tokens"
_OUTPUT_TOKENS = "output_tokens"
_CACHED = "cached"
_CACHED_TOKENS = "cached_tokens"
_CACHE_READ = "cache_read"
_CACHE_WRITE_FIVE_MIN = "cache_write_5m"
_CACHE_WRITE_ONE_HOUR = "cache_write_1h"
_TOTAL_TOKEN_USAGE = "total_token_usage"
_PAYLOAD = "payload"
_INFO_KEY = "info"
_ITEM_KEY = "item"
_RESULT_KEY = "result"
_ASSISTANT = "assistant"
_UNKNOWN = "unknown"
_CLAUDE = "claude"
_CODEX = "codex"
_LONG_CONTEXT_THRESHOLD = "long_context_threshold"
_LONG_CONTEXT_INPUT_MULT = "long_context_input_mult"
_LONG_CONTEXT_OUTPUT_MULT = "long_context_output_mult"
# Model rates are quoted in USD per 1,000,000 tokens.
_TOKENS_PER_MILLION = 1_000_000

# Canonical per-call token bucket: the input / cache / output counters a usage
# record decodes to, keyed by the protocol field names above.
_TokenBucket = dict[str, int]

# One Anthropic price row: a family-name regex and its USD-per-1M rate map.
_ClaudeRateMap = dict[str, float]
_ClaudeRateRow = tuple[re.Pattern[str], _ClaudeRateMap]

# One OpenAI price row: a model-name prefix and its USD-per-1M rate map. A
# family may publish no cached rate, so the map's values are Optional.
_CodexRateMap = dict[str, Optional[float]]
_CodexRateRow = tuple[str, _CodexRateMap]

# One codex cumulative usage frame: its model name and decoded token bucket.
_CodexUsageEvent = tuple[str, _TokenBucket]


@dataclass
class UsageMetrics:
    """Structured usage extracted from one agent run's JSONL stdout.

    ``cached_tokens`` is the codex-style "portion of input that was cached"
    counter; ``cache_read_tokens`` / ``cache_write_tokens`` are the claude
    cache-read and (5m+1h) cache-create totals. Fields irrelevant to a given
    backend stay at 0 so downstream aggregation can treat the shape
    uniformly.

    ``cost_usd`` is ``None`` when ``cost_source`` is ``no-usage`` or
    ``unknown-price``. ``models`` lists the distinct model strings observed
    in the stream, in first-seen order; ``turns`` is ``None`` when no turn
    count could be derived.
    """

    backend: str
    models: tuple[str, ...] = ()
    turns: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "no-usage"

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "models": list(self.models),
            "turns": self.turns,
            _INPUT_TOKENS: self.input_tokens,
            _OUTPUT_TOKENS: self.output_tokens,
            _CACHED_TOKENS: self.cached_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
        }


# --- price tables -----------------------------------------------------------

# Anthropic rates are USD per 1M tokens for input / cache-write (5m and 1h
# variants) / cache-read / output. Patterns intentionally match by family name
# rather than full SKU so newly-released point releases inherit the family
# rate by default; a SKU we cannot confidently price returns None.
_CLAUDE_RATES: tuple[_ClaudeRateRow, ...] = (
    (
        re.compile(r"opus.*4([._-]?[567]|\.[567])"),
        {_INPUT: 5, _CACHE_WRITE_FIVE_MIN: 6.25, _CACHE_WRITE_ONE_HOUR: 10,
         _CACHE_READ: 0.5, _OUTPUT: 25},
    ),
    (
        re.compile(r"opus.*4"),
        {_INPUT: 15, _CACHE_WRITE_FIVE_MIN: 18.75, _CACHE_WRITE_ONE_HOUR: 30,
         _CACHE_READ: 1.5, _OUTPUT: 75},
    ),
    (
        re.compile(r"sonnet"),
        {_INPUT: 3, _CACHE_WRITE_FIVE_MIN: 3.75, _CACHE_WRITE_ONE_HOUR: 6,
         _CACHE_READ: 0.3, _OUTPUT: 15},
    ),
    (
        re.compile(r"haiku.*3([._-]?5|\.5)"),
        {_INPUT: 0.8, _CACHE_WRITE_FIVE_MIN: 1, _CACHE_WRITE_ONE_HOUR: 1.6,
         _CACHE_READ: 0.08, _OUTPUT: 4},
    ),
    (
        re.compile(r"haiku"),
        {_INPUT: 1, _CACHE_WRITE_FIVE_MIN: 1.25, _CACHE_WRITE_ONE_HOUR: 2,
         _CACHE_READ: 0.1, _OUTPUT: 5},
    ),
)


def _claude_rates(model: str) -> Optional[_ClaudeRateMap]:
    if not model or model == _UNKNOWN:
        return None
    lowered = model.lower()
    for pat, rates in _CLAUDE_RATES:
        if pat.search(lowered):
            return rates
    return None


def _claude_estimate_cost(
    model: str, bucket: _TokenBucket
) -> Optional[float]:
    """Price one model's token bucket from the shared ``_CLAUDE_RATES`` table.

    ``bucket`` is a ``_claude_usage_record``-shaped dict (``input`` /
    ``cache_write_5m`` / ``cache_write_1h`` / ``cache_read`` / ``output``).
    Returns ``None`` when the model has no known rate so a caller can tell
    ``estimated`` from ``unknown-price``. Both the run aggregate
    (``parse_claude_usage``) and the per-turn builder (``_claude_turn_usage``)
    price through here, so a rate edit can never drift the run total apart from
    the per-turn numbers.
    """
    rates = _claude_rates(model)
    if rates is None:
        return None
    return (
        bucket[_INPUT] * rates[_INPUT]
        + bucket[_CACHE_WRITE_FIVE_MIN] * rates[_CACHE_WRITE_FIVE_MIN]
        + bucket[_CACHE_WRITE_ONE_HOUR] * rates[_CACHE_WRITE_ONE_HOUR]
        + bucket[_CACHE_READ] * rates[_CACHE_READ]
        + bucket[_OUTPUT] * rates[_OUTPUT]
    ) / _TOKENS_PER_MILLION


# OpenAI rates are USD per 1M tokens for input / cached / output. ``cached``
# may be None if Codex/OpenAI does not publish a cached rate for that family;
# in that case we will not produce an estimated cost when the run reports any
# cached tokens (rather than billing them at the input rate and being wrong).
_CODEX_RATES: tuple[_CodexRateRow, ...] = (
    # GPT-5.5, GPT-5.4, and GPT-5.4-pro bill the entire session at
    # 2x the input rate and 1.5x the output rate once total input
    # exceeds 272K tokens (per OpenAI's published long-context
    # pricing on each model's docs page). Cached tokens move at the
    # same multiplier as the uncached input remainder -- they are
    # still input billing, just discounted. A session at or under
    # the threshold uses the base rates verbatim. The reported
    # `total_cost_usd` always wins over this estimate, so a CLI-
    # reported value remains authoritative. The `-mini` / `-nano`
    # family members and `gpt-5.5-pro` are NOT on long-context
    # tiering today -- the official `gpt-5.5-pro` page lists flat
    # `$30 / $180` with no >272K multiplier and no cached discount,
    # so it stays flat-priced (see the negative-guard test).
    ("gpt-5.5-pro",        {_INPUT: 30,   _CACHED: None,  _OUTPUT: 180}),
    ("gpt-5.5",            {_INPUT: 5,    _CACHED: 0.5,   _OUTPUT: 30,
                            _LONG_CONTEXT_THRESHOLD: 272_000,
                            _LONG_CONTEXT_INPUT_MULT: 2.0,
                            _LONG_CONTEXT_OUTPUT_MULT: 1.5}),
    ("gpt-5.4-pro",        {_INPUT: 30,   _CACHED: None,  _OUTPUT: 180,
                            _LONG_CONTEXT_THRESHOLD: 272_000,
                            _LONG_CONTEXT_INPUT_MULT: 2.0,
                            _LONG_CONTEXT_OUTPUT_MULT: 1.5}),
    ("gpt-5.4-mini",       {_INPUT: 0.75, _CACHED: 0.075, _OUTPUT: 4.5}),
    ("gpt-5.4-nano",       {_INPUT: 0.2,  _CACHED: 0.02,  _OUTPUT: 1.25}),
    ("gpt-5.4",            {_INPUT: 2.5,  _CACHED: 0.25,  _OUTPUT: 15,
                            _LONG_CONTEXT_THRESHOLD: 272_000,
                            _LONG_CONTEXT_INPUT_MULT: 2.0,
                            _LONG_CONTEXT_OUTPUT_MULT: 1.5}),
    ("gpt-5.3-codex",      {_INPUT: 1.75, _CACHED: 0.175, _OUTPUT: 14}),
    ("gpt-5.3",            {_INPUT: 1.75, _CACHED: 0.175, _OUTPUT: 14}),
    # `*-pro` SKUs publish their own input / output rates and no
    # cached discount; explicit entries before the base prefix keep
    # prefix-match from falling through to the cheaper standard
    # family (which would silently undercount) and the `cached=None`
    # keeps cache-using pro runs at `unknown-price` rather than
    # billing them at the standard input rate.
    ("gpt-5.2-pro",        {_INPUT: 21,   _CACHED: None,  _OUTPUT: 168}),
    ("gpt-5.2",            {_INPUT: 1.75, _CACHED: 0.175, _OUTPUT: 14}),
    ("gpt-5.1-codex-mini", {_INPUT: 0.25, _CACHED: 0.025, _OUTPUT: 2}),
    ("gpt-5.1-codex",      {_INPUT: 1.25, _CACHED: 0.125, _OUTPUT: 10}),
    ("gpt-5.1",            {_INPUT: 1.25, _CACHED: 0.125, _OUTPUT: 10}),
    ("gpt-5-pro",          {_INPUT: 15,   _CACHED: None,  _OUTPUT: 120}),
    ("gpt-5-mini",         {_INPUT: 0.25, _CACHED: 0.025, _OUTPUT: 2}),
    ("gpt-5-nano",         {_INPUT: 0.05, _CACHED: 0.005, _OUTPUT: 0.4}),
    ("gpt-5-codex",        {_INPUT: 1.25, _CACHED: 0.125, _OUTPUT: 10}),
    ("gpt-5",              {_INPUT: 1.25, _CACHED: 0.125, _OUTPUT: 10}),
    ("codex-mini-latest",  {_INPUT: 1.5,  _CACHED: 0.375, _OUTPUT: 6}),
)


def _codex_rates(model: str) -> Optional[_CodexRateMap]:
    if not model or model == _UNKNOWN:
        return None
    lowered = model.lower()
    for prefix, rates in _CODEX_RATES:
        if lowered.startswith(prefix):
            return rates
    return None


# --- common helpers ---------------------------------------------------------

def _iter_events(stdout: str) -> list[dict[str, Any]]:
    """Parse the stdout as JSONL, dropping any lines we cannot decode.

    Both agent CLIs occasionally emit a banner line, partial flush, or trace
    string before / between proper JSON events. The shell reference handles
    this with ``fromjson?``; the Python side mirrors that by silently
    swallowing JSONDecodeError so a single bad line does not invalidate the
    whole stream.
    """
    events: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    return events


def _num(raw_count: Any) -> int:
    """Coerce a usage-field count to a non-negative int.

    Both backends sometimes report counts as floats or strings; the shell
    reference uses ``tonumber?`` for the same reason. Anything we cannot
    coerce becomes 0 rather than blowing up the whole parse.
    """
    number = 0
    if isinstance(raw_count, bool):
        number = int(raw_count)
    elif isinstance(raw_count, (int, float)):
        number = int(raw_count)
    elif isinstance(raw_count, str):
        with contextlib.suppress(ValueError):
            number = int(float(raw_count))
    return number


def _walk_objects(node: Any) -> Iterable[dict[str, Any]]:
    """Yield every dict reachable from ``node`` (depth-first).

    Codex buries ``total_cost_usd`` and model fields at varied nesting; this
    matches the ``.. | objects`` recursion in the shell reference without
    forcing the parser to enumerate every known path.
    """
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from _walk_objects(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_objects(child)


def _coerce_reported_cost(raw_cost: Any) -> Optional[float]:
    if isinstance(raw_cost, (int, float)):
        return float(raw_cost)
    if not isinstance(raw_cost, str):
        return None
    try:
        return float(raw_cost)
    except ValueError:
        return None


def _find_last_reported_cost(events: list[dict[str, Any]]) -> Optional[float]:
    """Return the final ``total_cost_usd`` observed anywhere in the stream.

    Both backends emit this on the terminal/result frame, but Codex sometimes
    nests it deeper than the top level; walk every object so a deeper path
    still wins over an estimate.
    """
    last_cost: Optional[float] = None
    for event in events:
        for payload in _walk_objects(event):
            reported_cost = _coerce_reported_cost(
                payload.get("total_cost_usd")
            )
            if reported_cost is not None:
                last_cost = reported_cost
    return last_cost


def _dedup_models(models: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for model in models:
        if model and model != _UNKNOWN and model not in seen:
            seen[model] = None
    return tuple(seen)


def _select_cost(
    reported: Optional[float],
    estimated: Optional[float],
    has_usage: bool,
) -> tuple[Optional[float], str]:
    """Choose the authoritative cost and the ``cost_source`` tag for it.

    A CLI-reported ``total_cost_usd`` always wins over a first-party estimate,
    and an estimate wins over nothing. With neither, ``no-usage`` marks a stream
    that carried no usage records at all, distinct from ``unknown-price`` (usage
    was present but the model has no known rate). Shared by both parsers so the
    precedence cannot drift between backends.
    """
    if reported is not None:
        return reported, "reported"
    if estimated is not None:
        return estimated, "estimated"
    if not has_usage:
        return None, "no-usage"
    return None, "unknown-price"


_ModelPath = tuple[str, ...]


def _nested_value(payload: dict[str, Any], path: _ModelPath) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _known_model(candidate: Any) -> Optional[str]:
    if isinstance(candidate, str) and candidate and candidate != _UNKNOWN:
        return candidate
    return None


def _nonempty_string(candidate: Any) -> Optional[str]:
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def _first_model_at_paths(
    event: dict[str, Any], paths: tuple[_ModelPath, ...],
) -> Optional[str]:
    for path in paths:
        model = _known_model(_nested_value(event, path))
        if model is not None:
            return model
    return None


def _first_string_at_paths(
    event: dict[str, Any], paths: tuple[_ModelPath, ...],
) -> Optional[str]:
    for path in paths:
        text = _nonempty_string(_nested_value(event, path))
        if text is not None:
            return text
    return None


# --- claude parser ----------------------------------------------------------

_CLAUDE_MODEL_PATHS: tuple[_ModelPath, ...] = (
    (_MESSAGE, _MODEL),
    ("event", _MESSAGE, _MODEL),
    (_MODEL,),
    ("response", _MODEL),
)


def _claude_model_name(event: dict[str, Any]) -> str:
    return _first_string_at_paths(event, _CLAUDE_MODEL_PATHS) or _UNKNOWN


def _claude_usage_record(usage: dict[str, Any]) -> _TokenBucket:
    """Decode one claude usage dict into the canonical counter shape.

    Claude reports either a flat ``cache_creation_input_tokens`` or the
    structured ``cache_creation.ephemeral_{5m,1h}_input_tokens`` form. When
    the flat form is present we credit the whole bucket to the 5m TTL,
    matching what the shell helper does -- mixing them would double-count.
    """
    flat = usage.get("cache_creation_input_tokens")
    if flat is None:
        cc = usage.get("cache_creation") if isinstance(
            usage.get("cache_creation"), dict
        ) else None
        cc_map = cc or {}
        cw5 = _num(
            cc_map.get("ephemeral_5m_input_tokens")
            or usage.get("ephemeral_5m_input_tokens")
        )
        cw1 = _num(
            cc_map.get("ephemeral_1h_input_tokens")
            or usage.get("ephemeral_1h_input_tokens")
        )
    else:
        cw5 = _num(flat)
        cw1 = 0
    return {
        _INPUT: _num(
            usage.get(_INPUT_TOKENS) or usage.get("prompt_tokens")
        ),
        _CACHE_WRITE_FIVE_MIN: cw5,
        _CACHE_WRITE_ONE_HOUR: cw1,
        _CACHE_READ: _num(
            usage.get("cache_read_input_tokens")
            or usage.get("cached_input_tokens")
            or usage.get("cache_read_tokens")
        ),
        _OUTPUT: _num(
            usage.get(_OUTPUT_TOKENS) or usage.get("completion_tokens")
        ),
    }


_ClaudeUsageRow = tuple[int, str, _TokenBucket]


def _claude_assistant_usage_row(
    idx: int, event: dict[str, Any],
) -> Optional[tuple[str, _ClaudeUsageRow]]:
    if event.get(_TYPE) != _ASSISTANT:
        return None
    message = event.get(_MESSAGE)
    if not isinstance(message, dict):
        return None
    usage = message.get(_USAGE)
    if not isinstance(usage, dict):
        return None
    message_id = message.get(_ID) or event.get("request_id") or str(idx)
    return (
        str(message_id),
        (idx, _claude_model_name(event), _claude_usage_record(usage)),
    )


def _claude_result_usage_row(
    idx: int, event: dict[str, Any],
) -> Optional[_ClaudeUsageRow]:
    if event.get(_TYPE) != _RESULT_KEY:
        return None
    usage = event.get(_USAGE)
    if not isinstance(usage, dict):
        return None
    return idx, _claude_model_name(event), _claude_usage_record(usage)


def _claude_usage_records(
    events: list[dict[str, Any]],
) -> list[_ClaudeUsageRow]:
    """Group claude usage into ``(idx, model, record)`` rows, last frame wins.

    Per-message usage events are keyed by ``message.id`` (falling back to
    ``request_id`` then stream position) and the last occurrence of each id
    overwrites earlier ones -- Claude streams partial usage on intermediate
    frames and the final frame carries the authoritative count. Rows are
    returned sorted by each id's final stored frame index (its last
    occurrence). When no assistant usage events exist we fall back to the
    terminal ``type:"result"`` frame's ``usage`` block.
    """
    by_id: dict[str, _ClaudeUsageRow] = {}
    for idx, event in enumerate(events):
        identified = _claude_assistant_usage_row(idx, event)
        if identified is not None:
            by_id[identified[0]] = identified[1]

    if by_id:
        return _sorted_claude_usage_rows(by_id)
    return _claude_result_usage_records(events)


def _sorted_claude_usage_rows(
    by_id: dict[str, _ClaudeUsageRow],
) -> list[_ClaudeUsageRow]:
    usage_rows = list(by_id.values())
    usage_rows.sort(key=_claude_usage_row_index)
    return usage_rows


def _claude_usage_row_index(usage_row: _ClaudeUsageRow) -> int:
    return usage_row[0]


def _claude_result_usage_records(
    events: list[dict[str, Any]],
) -> list[_ClaudeUsageRow]:
    return [
        row for idx, event in enumerate(events)
        if (row := _claude_result_usage_row(idx, event)) is not None
    ]


@dataclass
class _ClaudeUsageAggregate:
    """Per-model token buckets with stable first-seen model order."""

    per_model: dict[str, _TokenBucket] = field(default_factory=dict)
    model_order: list[str] = field(default_factory=list)

    def add(self, model: str, record: _TokenBucket) -> None:
        bucket = self.per_model.setdefault(
            model,
            {_INPUT: 0, _CACHE_WRITE_FIVE_MIN: 0, _CACHE_WRITE_ONE_HOUR: 0,
             _CACHE_READ: 0, _OUTPUT: 0},
        )
        if model not in self.model_order:
            self.model_order.append(model)
        for key, count in record.items():
            bucket[key] += count

    def apply_tokens(self, metrics: UsageMetrics) -> None:
        for bucket in self.per_model.values():
            metrics.input_tokens += bucket[_INPUT]
            metrics.output_tokens += bucket[_OUTPUT]
            metrics.cache_read_tokens += bucket[_CACHE_READ]
            metrics.cache_write_tokens += (
                bucket[_CACHE_WRITE_FIVE_MIN] + bucket[_CACHE_WRITE_ONE_HOUR]
            )
        metrics.models = _dedup_models(self.model_order)


def _claude_aggregate_by_model(
    records: list[_ClaudeUsageRow],
) -> _ClaudeUsageAggregate:
    """Sum usage records into per-model token buckets, keeping first-seen order.

    Per-model aggregation (rather than one flat total) keeps each model's
    tokens together so ``_claude_estimate_total`` can price a mixed-model run
    at each model's own rate.
    """
    aggregate = _ClaudeUsageAggregate()
    for _, model, record in records:
        aggregate.add(model, record)
    return aggregate


def _claude_estimate_total(
    per_model: dict[str, _TokenBucket],
) -> Optional[float]:
    """Sum each model's estimated cost, or ``None`` if any model is unpriced.

    Returns ``None`` (never a partial sum) when the price table has no rate for
    some model in the run, so the caller can tell ``estimated`` from
    ``unknown-price``; also ``None`` when there are no usage records at all.
    """
    if not per_model:
        return None
    parts: list[float] = []
    for model, bucket in per_model.items():
        part = _claude_estimate_cost(model, bucket)
        if part is None:
            return None
        parts.append(part)
    return sum(parts)


def _claude_turn_count(
    events: list[dict[str, Any]],
    records: list[_ClaudeUsageRow],
) -> Optional[int]:
    """Turn count for a claude run: the ``result`` frame's ``num_turns``.

    Falls back to the number of per-message usage records when no ``result``
    frame reports ``num_turns``; ``None`` when neither is available.
    """
    num_turns: Optional[int] = None
    for ev in events:
        if ev.get(_TYPE) == _RESULT_KEY:
            nt = ev.get("num_turns")
            if isinstance(nt, (int, float)):
                num_turns = int(nt)
    if num_turns is None and records:
        num_turns = len(records)
    return num_turns


def parse_claude_usage(stdout: str) -> UsageMetrics:
    """Extract usage / cost from a ``claude -p --output-format stream-json`` run.

    Per-message usage events are grouped by ``message.id`` and the last
    occurrence of each id wins; Claude streams partial usage on intermediate
    frames and the final frame carries the authoritative count. When no
    assistant usage events exist we fall back to the terminal
    ``type:"result"`` frame's ``usage`` block.
    """
    events = _iter_events(stdout)
    metrics = UsageMetrics(backend=_CLAUDE)

    records = _claude_usage_records(events)
    aggregate = _claude_aggregate_by_model(records)
    aggregate.apply_tokens(metrics)
    selected_cost = _select_cost(
        _find_last_reported_cost(events),
        _claude_estimate_total(aggregate.per_model),
        bool(records),
    )
    metrics.cost_usd = selected_cost[0]
    metrics.cost_source = selected_cost[1]
    metrics.turns = _claude_turn_count(events, records)
    return metrics


# --- codex parser -----------------------------------------------------------

_CODEX_USAGE_PATHS: tuple[tuple[str, ...], ...] = (
    (_USAGE,),
    ("token_usage",),
    (_TOTAL_TOKEN_USAGE,),
    (_INFO_KEY, _TOTAL_TOKEN_USAGE),
    (_INFO_KEY, _USAGE),
    (_PAYLOAD, _USAGE),
    (_PAYLOAD, "token_usage"),
    (_PAYLOAD, _TOTAL_TOKEN_USAGE),
    (_PAYLOAD, _INFO_KEY, _TOTAL_TOKEN_USAGE),
    (_PAYLOAD, _INFO_KEY, _USAGE),
)


def _codex_usage_block(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    for path in _CODEX_USAGE_PATHS:
        cur: Any = event
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        if isinstance(cur, dict):
            return cur
    return None


_CODEX_MODEL_PATHS: tuple[_ModelPath, ...] = (
    (_MODEL,),
    ("response", _MODEL),
    (_ITEM_KEY, _MODEL),
    ("event", _MODEL),
    (_PAYLOAD, _MODEL),
    (_PAYLOAD, "settings", _MODEL),
    (_PAYLOAD, "collaboration_mode", "settings", _MODEL),
    (_INFO_KEY, _MODEL),
    (_PAYLOAD, _INFO_KEY, _MODEL),
)


def _codex_model_name(
    event: dict[str, Any], usage: Optional[dict[str, Any]]
) -> str:
    event_model = _first_model_at_paths(event, _CODEX_MODEL_PATHS)
    if event_model is not None:
        return event_model
    usage_model = _known_model(usage.get(_MODEL)) if usage else None
    return usage_model or _UNKNOWN


def _nested_usage_field(
    usage: dict[str, Any], outer_key: str, inner_key: str,
) -> Any:
    """Return `usage[outer_key][inner_key]` when the outer value is a dict,
    else None (a non-dict outer value has no nested field to read)."""
    outer = usage.get(outer_key)
    return outer.get(inner_key) if isinstance(outer, dict) else None


def _codex_usage_record(usage: dict[str, Any]) -> _TokenBucket:
    input_tokens = _num(
        usage.get(_INPUT_TOKENS)
        or usage.get("prompt_tokens")
        or usage.get("total_input_tokens")
    )
    cached = _num(
        usage.get("cached_input_tokens")
        or usage.get(_CACHED_TOKENS)
        or _nested_usage_field(usage, "input_tokens_details", _CACHED_TOKENS)
        or _nested_usage_field(
            usage, "prompt_tokens_details", _CACHED_TOKENS,
        )
    )
    output_tokens = _num(
        usage.get(_OUTPUT_TOKENS)
        or usage.get("completion_tokens")
        or usage.get("total_output_tokens")
    )
    return {_INPUT: input_tokens, _CACHED: cached, _OUTPUT: output_tokens}


_TURN_COMPLETE_RE = re.compile(r"turn[_ -]?complete|turncomplete", re.IGNORECASE)


def _codex_usage_events(
    events: list[dict[str, Any]],
) -> list[_CodexUsageEvent]:
    """Collect non-empty codex usage records as ``(model, record)`` in order.

    Frames whose input + cached + output sum to zero are dropped so the
    last-wins pick in ``parse_codex_usage`` lands on a frame that actually
    carried tokens rather than a trailing empty one.
    """
    usage_events: list[_CodexUsageEvent] = []
    for ev in events:
        usage = _codex_usage_block(ev)
        if usage is None:
            continue
        rec = _codex_usage_record(usage)
        if (rec[_INPUT] + rec[_CACHED] + rec[_OUTPUT]) == 0:
            continue
        model = _codex_model_name(ev, usage)
        usage_events.append((model, rec))
    return usage_events


def _codex_select_model(
    events: list[dict[str, Any]],
    last_model: str,
    fallback_model: Optional[str],
) -> Optional[str]:
    """Pick the run's model: the last usage frame's, else a stream-wide scan.

    Prefers the model named on the authoritative (last) usage frame. Failing
    that, the last ``model`` field seen anywhere in the stream wins, then the
    caller-supplied ``fallback_model``. ``None`` when nothing names a model.
    """
    chosen_model = _known_model(last_model)
    if chosen_model is not None:
        return chosen_model
    stream_model = _last_stream_model(events)
    if stream_model is not None:
        return stream_model
    return _known_model(fallback_model)


def _last_stream_model(
    events: list[dict[str, Any]],
) -> Optional[str]:
    last_model: Optional[str] = None
    for event in events:
        for payload in _walk_objects(event):
            model = _known_model(payload.get(_MODEL))
            if model is not None:
                last_model = model
    return last_model


def _codex_estimate_cost(
    model: str, usage: _TokenBucket
) -> Optional[float]:
    """Price a codex usage record, honoring the long-context tier.

    ``None`` when the model has no known rate, when the run carried no
    input/output tokens, or when it used cached tokens the family publishes no
    cached rate for (billing those at the input rate would overcharge).
    """
    rates = _codex_rates(model)
    if rates is None or (usage[_INPUT] + usage[_OUTPUT]) <= 0:
        return None
    return _CodexPrice(rates, usage).estimate()


@dataclass(frozen=True)
class _CodexPrice:
    """Inputs needed to price one Codex cumulative usage frame."""

    rates: _CodexRateMap
    usage: _TokenBucket

    def estimate(self) -> Optional[float]:
        input_mult, output_mult = self._multipliers()
        input_cost = self._input_cost(input_mult)
        if input_cost is None:
            return None
        return (
            input_cost
            + self.usage[_OUTPUT] * self.rates[_OUTPUT] * output_mult
        ) / _TOKENS_PER_MILLION

    def _multipliers(self) -> tuple[float, float]:
        threshold = self.rates.get(_LONG_CONTEXT_THRESHOLD)
        if threshold is None or self.usage[_INPUT] <= threshold:
            return 1.0, 1.0
        return (
            self.rates.get(_LONG_CONTEXT_INPUT_MULT) or 1.0,
            self.rates.get(_LONG_CONTEXT_OUTPUT_MULT) or 1.0,
        )

    def _input_cost(self, multiplier: float) -> Optional[float]:
        # Codex reports cached input as a subset of total input. Price only
        # the uncached remainder at the full input rate; an unpublished cached
        # rate makes the estimate unknown instead of silently overcharging.
        cached = self.usage[_CACHED]
        cached_rate = self.rates[_CACHED]
        if cached > 0 and cached_rate is None:
            return None
        uncached = max(self.usage[_INPUT] - cached, 0)
        effective_cached_rate = (
            self.rates[_INPUT] if cached_rate is None else cached_rate
        )
        return (
            uncached * self.rates[_INPUT]
            + cached * effective_cached_rate
        ) * multiplier


def _reported_codex_turn_count(
    events: list[dict[str, Any]],
) -> Optional[int]:
    reported: Optional[int] = None
    for event in events:
        for payload in _walk_objects(event):
            turns_value = payload.get("num_turns")
            if isinstance(turns_value, (int, float)):
                reported = int(turns_value)
    return reported


def _completed_codex_turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    count = sum(
        1 for event in events
        if isinstance(event.get(_TYPE), str)
        and _TURN_COMPLETE_RE.search(event[_TYPE])
    )
    return count or None


def _last_codex_usage(
    usage_events: list[_CodexUsageEvent],
) -> _CodexUsageEvent:
    if usage_events:
        return usage_events[-1]
    return _UNKNOWN, {_INPUT: 0, _CACHED: 0, _OUTPUT: 0}


@dataclass(frozen=True)
class _CodexUsageSummary:
    """Authoritative cumulative usage frame and selected model for one run."""

    events: list[dict[str, Any]]
    usage_events: list[_CodexUsageEvent]
    usage: _TokenBucket
    model: Optional[str]

    @classmethod
    def build(
        cls,
        events: list[dict[str, Any]],
        fallback_model: Optional[str],
    ) -> _CodexUsageSummary:
        usage_events = _codex_usage_events(events)
        last_model, usage = _last_codex_usage(usage_events)
        return cls(
            events=events,
            usage_events=usage_events,
            usage=usage,
            model=_codex_select_model(events, last_model, fallback_model),
        )

    def apply(self, metrics: UsageMetrics) -> None:
        metrics.input_tokens = self.usage[_INPUT]
        metrics.cached_tokens = self.usage[_CACHED]
        metrics.output_tokens = self.usage[_OUTPUT]
        if self.model is not None:
            metrics.models = (self.model,)
        selected_cost = _select_cost(
            _find_last_reported_cost(self.events),
            _codex_estimate_cost(self.model or _UNKNOWN, self.usage),
            bool(self.usage_events),
        )
        metrics.cost_usd = selected_cost[0]
        metrics.cost_source = selected_cost[1]
        metrics.turns = _codex_turn_count(self.events)


def _codex_turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    """Turn count from a reported total, else completed-turn event count."""
    reported = _reported_codex_turn_count(events)
    if reported is not None:
        return reported
    return _completed_codex_turn_count(events)


def parse_codex_usage(
    stdout: str, fallback_model: Optional[str] = None
) -> UsageMetrics:
    """Extract usage / cost from a ``codex exec --json`` run.

    Codex usage events are cumulative across the session; the shell
    reference takes the *last* non-zero usage record as the authoritative
    total rather than summing per-event deltas. We do the same here.
    """
    events = _iter_events(stdout)
    metrics = UsageMetrics(backend=_CODEX)
    _CodexUsageSummary.build(events, fallback_model).apply(metrics)
    return metrics


def parse_agent_usage(
    backend: str,
    stdout: str,
    *,
    fallback_model: Optional[str] = None,
) -> UsageMetrics:
    """Dispatch by backend name; raise on anything other than claude/codex.

    Mirrors ``agents.run_agent``'s contract so callers can pass through the
    same backend string they used to spawn the agent.
    """
    if backend == _CLAUDE:
        return parse_claude_usage(stdout)
    if backend == _CODEX:
        return parse_codex_usage(stdout, fallback_model=fallback_model)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'"
    )
