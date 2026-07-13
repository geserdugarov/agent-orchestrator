# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Parse usage metrics from agent JSONL stdout (claude / codex).

Inputs are the raw stdout strings that `agents.AgentResult.stdout` carries,
which are the same event streams `agent-develop-review-loop`'s shell helpers
consume via jq. We extract per-call totals (input / output / cached /
cache-read / cache-write tokens), the model(s) involved, the number of turns,
and a `cost_usd` figure with a `cost_source` tag that records how it was
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

A sibling extractor (``parse_claude_skills`` / ``parse_codex_skills`` /
``parse_agent_skills``) reuses the same event iterator and resilience
contract to record which agent *skills* a run triggered. It reads only the
skill name -- never the ``Skill`` tool's ``args`` -- and is observation-only.

A further sibling classifier (``parse_claude_trajectory`` /
``parse_codex_trajectory`` / ``parse_agent_trajectory``) reuses the same
event iterator and resilience contract to reconstruct a run's *trajectory*:
the offered tools, triggered skills, the ordered timeline of ``tool_call`` /
``tool_result`` steps interleaved with ``assistant_message`` /
``user_message`` text turns, and the final output. For claude it also emits
per-turn token usage (``TurnUsage``, one per assistant ``message.id``) and
stamps each step with the ``turn`` index that produced it; codex leaves the
per-turn section empty (its usage frames are cumulative, not per-turn). It
classifies raw stream data only -- it neither writes files nor
redacts/truncates content; a downstream writer owns that. ``system_prompt``
and ``tools`` stay best-effort and empty when a backend's stream does not
expose them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


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
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
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
_CLAUDE_RATES: tuple[tuple[re.Pattern[str], dict[str, float]], ...] = (
    (
        re.compile(r"opus.*4([._-]?[567]|\.[567])"),
        {"input": 5, "cache_write_5m": 6.25, "cache_write_1h": 10,
         "cache_read": 0.50, "output": 25},
    ),
    (
        re.compile(r"opus.*4"),
        {"input": 15, "cache_write_5m": 18.75, "cache_write_1h": 30,
         "cache_read": 1.50, "output": 75},
    ),
    (
        re.compile(r"sonnet"),
        {"input": 3, "cache_write_5m": 3.75, "cache_write_1h": 6,
         "cache_read": 0.30, "output": 15},
    ),
    (
        re.compile(r"haiku.*3([._-]?5|\.5)"),
        {"input": 0.80, "cache_write_5m": 1, "cache_write_1h": 1.60,
         "cache_read": 0.08, "output": 4},
    ),
    (
        re.compile(r"haiku"),
        {"input": 1, "cache_write_5m": 1.25, "cache_write_1h": 2,
         "cache_read": 0.10, "output": 5},
    ),
)


def _claude_rates(model: str) -> Optional[dict[str, float]]:
    if not model or model == "unknown":
        return None
    m = model.lower()
    for pat, rates in _CLAUDE_RATES:
        if pat.search(m):
            return rates
    return None


def _claude_estimate_cost(
    model: str, bucket: dict[str, int]
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
        bucket["input"] * rates["input"]
        + bucket["cache_write_5m"] * rates["cache_write_5m"]
        + bucket["cache_write_1h"] * rates["cache_write_1h"]
        + bucket["cache_read"] * rates["cache_read"]
        + bucket["output"] * rates["output"]
    ) / 1_000_000


# OpenAI rates are USD per 1M tokens for input / cached / output. ``cached``
# may be None if Codex/OpenAI does not publish a cached rate for that family;
# in that case we will not produce an estimated cost when the run reports any
# cached tokens (rather than billing them at the input rate and being wrong).
_CODEX_RATES: tuple[tuple[str, dict[str, Optional[float]]], ...] = (
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
    ("gpt-5.5-pro",        {"input": 30,   "cached": None,  "output": 180}),
    ("gpt-5.5",            {"input": 5,    "cached": 0.50,  "output": 30,
                            "long_context_threshold": 272_000,
                            "long_context_input_mult": 2.0,
                            "long_context_output_mult": 1.5}),
    ("gpt-5.4-pro",        {"input": 30,   "cached": None,  "output": 180,
                            "long_context_threshold": 272_000,
                            "long_context_input_mult": 2.0,
                            "long_context_output_mult": 1.5}),
    ("gpt-5.4-mini",       {"input": 0.75, "cached": 0.075, "output": 4.50}),
    ("gpt-5.4-nano",       {"input": 0.20, "cached": 0.02,  "output": 1.25}),
    ("gpt-5.4",            {"input": 2.50, "cached": 0.25,  "output": 15,
                            "long_context_threshold": 272_000,
                            "long_context_input_mult": 2.0,
                            "long_context_output_mult": 1.5}),
    ("gpt-5.3-codex",      {"input": 1.75, "cached": 0.175, "output": 14}),
    ("gpt-5.3",            {"input": 1.75, "cached": 0.175, "output": 14}),
    # `*-pro` SKUs publish their own input / output rates and no
    # cached discount; explicit entries before the base prefix keep
    # prefix-match from falling through to the cheaper standard
    # family (which would silently undercount) and the `cached=None`
    # keeps cache-using pro runs at `unknown-price` rather than
    # billing them at the standard input rate.
    ("gpt-5.2-pro",        {"input": 21,   "cached": None,  "output": 168}),
    ("gpt-5.2",            {"input": 1.75, "cached": 0.175, "output": 14}),
    ("gpt-5.1-codex-mini", {"input": 0.25, "cached": 0.025, "output": 2}),
    ("gpt-5.1-codex",      {"input": 1.25, "cached": 0.125, "output": 10}),
    ("gpt-5.1",            {"input": 1.25, "cached": 0.125, "output": 10}),
    ("gpt-5-pro",          {"input": 15,   "cached": None,  "output": 120}),
    ("gpt-5-mini",         {"input": 0.25, "cached": 0.025, "output": 2}),
    ("gpt-5-nano",         {"input": 0.05, "cached": 0.005, "output": 0.40}),
    ("gpt-5-codex",        {"input": 1.25, "cached": 0.125, "output": 10}),
    ("gpt-5",              {"input": 1.25, "cached": 0.125, "output": 10}),
    ("codex-mini-latest",  {"input": 1.50, "cached": 0.375, "output": 6}),
)


def _codex_rates(model: str) -> Optional[dict[str, Optional[float]]]:
    if not model or model == "unknown":
        return None
    m = model.lower()
    for prefix, rates in _CODEX_RATES:
        if m.startswith(prefix):
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
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _num(value: Any) -> int:
    """Coerce a usage-field value to a non-negative int.

    Both backends sometimes report counts as floats or strings; the shell
    reference uses ``tonumber?`` for the same reason. Anything we cannot
    coerce becomes 0 rather than blowing up the whole parse.
    """
    number = 0
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(float(value))
        except ValueError:
            pass
    return number


def _walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    """Yield every dict reachable from ``value`` (depth-first).

    Codex buries ``total_cost_usd`` and model fields at varied nesting; this
    matches the ``.. | objects`` recursion in the shell reference without
    forcing the parser to enumerate every known path.
    """
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk_objects(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_objects(v)


def _coerce_reported_cost(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return float(value)
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
    for m in models:
        if m and m != "unknown" and m not in seen:
            seen[m] = None
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


def _known_model(value: Any) -> Optional[str]:
    if isinstance(value, str) and value and value != "unknown":
        return value
    return None


def _nonempty_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
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
        value = _nonempty_string(_nested_value(event, path))
        if value is not None:
            return value
    return None


# --- claude parser ----------------------------------------------------------

_CLAUDE_MODEL_PATHS: tuple[_ModelPath, ...] = (
    ("message", "model"),
    ("event", "message", "model"),
    ("model",),
    ("response", "model"),
)


def _claude_model_name(event: dict[str, Any]) -> str:
    return _first_string_at_paths(event, _CLAUDE_MODEL_PATHS) or "unknown"


def _claude_usage_record(usage: dict[str, Any]) -> dict[str, int]:
    """Decode one claude usage dict into the canonical counter shape.

    Claude reports either a flat ``cache_creation_input_tokens`` or the
    structured ``cache_creation.ephemeral_{5m,1h}_input_tokens`` form. When
    the flat form is present we credit the whole bucket to the 5m TTL,
    matching what the shell helper does -- mixing them would double-count.
    """
    flat = usage.get("cache_creation_input_tokens")
    if flat is not None:
        cw5 = _num(flat)
        cw1 = 0
    else:
        cc = usage.get("cache_creation") if isinstance(
            usage.get("cache_creation"), dict
        ) else None
        cw5 = _num(
            (cc.get("ephemeral_5m_input_tokens") if cc else None)
            or usage.get("ephemeral_5m_input_tokens")
        )
        cw1 = _num(
            (cc.get("ephemeral_1h_input_tokens") if cc else None)
            or usage.get("ephemeral_1h_input_tokens")
        )
    return {
        "input": _num(
            usage.get("input_tokens") or usage.get("prompt_tokens")
        ),
        "cache_write_5m": cw5,
        "cache_write_1h": cw1,
        "cache_read": _num(
            usage.get("cache_read_input_tokens")
            or usage.get("cached_input_tokens")
            or usage.get("cache_read_tokens")
        ),
        "output": _num(
            usage.get("output_tokens") or usage.get("completion_tokens")
        ),
    }


_ClaudeUsageRow = tuple[int, str, dict[str, int]]


def _claude_assistant_usage_row(
    idx: int, event: dict[str, Any],
) -> Optional[tuple[str, _ClaudeUsageRow]]:
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    message_id = message.get("id") or event.get("request_id") or str(idx)
    return (
        str(message_id),
        (idx, _claude_model_name(event), _claude_usage_record(usage)),
    )


def _claude_result_usage_row(
    idx: int, event: dict[str, Any],
) -> Optional[_ClaudeUsageRow]:
    if event.get("type") != "result":
        return None
    usage = event.get("usage")
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

    per_model: dict[str, dict[str, int]] = field(default_factory=dict)
    model_order: list[str] = field(default_factory=list)

    def add(self, model: str, record: dict[str, int]) -> None:
        bucket = self.per_model.setdefault(
            model,
            {"input": 0, "cache_write_5m": 0, "cache_write_1h": 0,
             "cache_read": 0, "output": 0},
        )
        if model not in self.model_order:
            self.model_order.append(model)
        for key, value in record.items():
            bucket[key] += value

    def apply_tokens(self, metrics: UsageMetrics) -> None:
        for bucket in self.per_model.values():
            metrics.input_tokens += bucket["input"]
            metrics.output_tokens += bucket["output"]
            metrics.cache_read_tokens += bucket["cache_read"]
            metrics.cache_write_tokens += (
                bucket["cache_write_5m"] + bucket["cache_write_1h"]
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
    per_model: dict[str, dict[str, int]],
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
        if ev.get("type") == "result":
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
    metrics = UsageMetrics(backend="claude")

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
    ("usage",),
    ("token_usage",),
    ("total_token_usage",),
    ("info", "total_token_usage"),
    ("info", "usage"),
    ("payload", "usage"),
    ("payload", "token_usage"),
    ("payload", "total_token_usage"),
    ("payload", "info", "total_token_usage"),
    ("payload", "info", "usage"),
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
    ("model",),
    ("response", "model"),
    ("item", "model"),
    ("event", "model"),
    ("payload", "model"),
    ("payload", "settings", "model"),
    ("payload", "collaboration_mode", "settings", "model"),
    ("info", "model"),
    ("payload", "info", "model"),
)


def _codex_model_name(
    event: dict[str, Any], usage: Optional[dict[str, Any]]
) -> str:
    event_model = _first_model_at_paths(event, _CODEX_MODEL_PATHS)
    if event_model is not None:
        return event_model
    usage_model = _known_model(usage.get("model")) if usage else None
    return usage_model or "unknown"


def _codex_usage_record(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = _num(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("total_input_tokens")
    )
    cached = _num(
        usage.get("cached_input_tokens")
        or usage.get("cached_tokens")
        or (
            usage.get("input_tokens_details", {}).get("cached_tokens")
            if isinstance(usage.get("input_tokens_details"), dict)
            else None
        )
        or (
            usage.get("prompt_tokens_details", {}).get("cached_tokens")
            if isinstance(usage.get("prompt_tokens_details"), dict)
            else None
        )
    )
    output_tokens = _num(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("total_output_tokens")
    )
    return {"input": input_tokens, "cached": cached, "output": output_tokens}


_TURN_COMPLETE_RE = re.compile(r"turn[_ -]?complete|turncomplete", re.IGNORECASE)


def _codex_usage_events(
    events: list[dict[str, Any]],
) -> list[tuple[str, dict[str, int]]]:
    """Collect non-empty codex usage records as ``(model, record)`` in order.

    Frames whose input + cached + output sum to zero are dropped so the
    last-wins pick in ``parse_codex_usage`` lands on a frame that actually
    carried tokens rather than a trailing empty one.
    """
    usage_events: list[tuple[str, dict[str, int]]] = []
    for ev in events:
        usage = _codex_usage_block(ev)
        if usage is None:
            continue
        rec = _codex_usage_record(usage)
        if (rec["input"] + rec["cached"] + rec["output"]) == 0:
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
            model = _known_model(payload.get("model"))
            if model is not None:
                last_model = model
    return last_model


def _codex_estimate_cost(
    model: str, usage: dict[str, int]
) -> Optional[float]:
    """Price a codex usage record, honoring the long-context tier.

    ``None`` when the model has no known rate, when the run carried no
    input/output tokens, or when it used cached tokens the family publishes no
    cached rate for (billing those at the input rate would overcharge).
    """
    rates = _codex_rates(model)
    if rates is None or (usage["input"] + usage["output"]) <= 0:
        return None
    return _CodexPrice(rates, usage).estimate()


@dataclass(frozen=True)
class _CodexPrice:
    """Inputs needed to price one Codex cumulative usage frame."""

    rates: dict[str, Optional[float]]
    usage: dict[str, int]

    def _multipliers(self) -> tuple[float, float]:
        threshold = self.rates.get("long_context_threshold")
        if threshold is None or self.usage["input"] <= threshold:
            return 1.0, 1.0
        return (
            self.rates.get("long_context_input_mult") or 1.0,
            self.rates.get("long_context_output_mult") or 1.0,
        )

    def estimate(self) -> Optional[float]:
        input_mult, output_mult = self._multipliers()
        input_cost = self._input_cost(input_mult)
        if input_cost is None:
            return None
        return (
            input_cost
            + self.usage["output"] * self.rates["output"] * output_mult
        ) / 1_000_000

    def _input_cost(self, multiplier: float) -> Optional[float]:
        # Codex reports cached input as a subset of total input. Price only
        # the uncached remainder at the full input rate; an unpublished cached
        # rate makes the estimate unknown instead of silently overcharging.
        cached = self.usage["cached"]
        cached_rate = self.rates["cached"]
        if cached > 0 and cached_rate is None:
            return None
        uncached = max(self.usage["input"] - cached, 0)
        effective_cached_rate = (
            cached_rate if cached_rate is not None else self.rates["input"]
        )
        return (
            uncached * self.rates["input"]
            + cached * effective_cached_rate
        ) * multiplier


def _reported_codex_turn_count(
    events: list[dict[str, Any]],
) -> Optional[int]:
    reported: Optional[int] = None
    for event in events:
        for obj in _walk_objects(event):
            value = obj.get("num_turns")
            if isinstance(value, (int, float)):
                reported = int(value)
    return reported


def _completed_codex_turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    count = sum(
        1 for event in events
        if isinstance(event.get("type"), str)
        and _TURN_COMPLETE_RE.search(event["type"])
    )
    return count or None


def _last_codex_usage(
    usage_events: list[tuple[str, dict[str, int]]],
) -> tuple[str, dict[str, int]]:
    if usage_events:
        return usage_events[-1]
    return "unknown", {"input": 0, "cached": 0, "output": 0}


@dataclass(frozen=True)
class _CodexUsageSummary:
    """Authoritative cumulative usage frame and selected model for one run."""

    events: list[dict[str, Any]]
    usage_events: list[tuple[str, dict[str, int]]]
    usage: dict[str, int]
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
        metrics.input_tokens = self.usage["input"]
        metrics.cached_tokens = self.usage["cached"]
        metrics.output_tokens = self.usage["output"]
        if self.model is not None:
            metrics.models = (self.model,)
        selected_cost = _select_cost(
            _find_last_reported_cost(self.events),
            _codex_estimate_cost(self.model or "unknown", self.usage),
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
    metrics = UsageMetrics(backend="codex")
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
    if backend == "claude":
        return parse_claude_usage(stdout)
    if backend == "codex":
        return parse_codex_usage(stdout, fallback_model=fallback_model)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'"
    )


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
    contract the usage parsers above honor.
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
    if block.get("type") != "tool_use" or block.get("name") != "Skill":
        return None
    inp = block.get("input")
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
        if event.get("type") != "system":
            continue
        if event.get("subtype") != "init":
            continue
        return event.get(field_name)
    return None


def _ordered_unique_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    ordered_names: list[str] = []
    seen_names: set[str] = set()
    for name in value:
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
        if event.get("type") != "assistant":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            self._add_block(block)

    def _add_block(self, block: Any) -> None:
        name = _claude_skill_name(block)
        if name is None:
            return
        block_id = block.get("id")
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
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            return
        command = item.get("command")
        if not isinstance(command, str):
            return
        names = _CODEX_SKILL_PATH_RE.findall(command)
        if not names:
            return
        item_id = item.get("id")
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
    if backend == "claude":
        return parse_claude_skills(stdout)
    if backend == "codex":
        return parse_codex_skills(stdout)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'"
    )


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
            "content": self.content,
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
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
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
            "steps": [s.to_dict() for s in self.steps],
            "final_output": self.final_output,
            "turns": [t.to_dict() for t in self.turns],
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
        if ev.get("type") != "result":
            continue
        value = ev.get("result")
        if isinstance(value, str):
            final = value
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
    msg = event.get("message")
    mid = msg.get("id") if isinstance(msg, dict) else None
    if isinstance(mid, str) and mid:
        return mid
    rid = event.get("request_id")
    if isinstance(rid, str) and rid:
        return rid
    return str(idx)


def _claude_assistant_steps(
    content: list[Any], turn: Optional[int], seen_calls: set[str],
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
    for block in content:
        step = _claude_assistant_step(block, turn, seen_calls)
        if step is not None:
            steps.append(step)
    return steps


def _claude_assistant_step(
    block: Any, turn: Optional[int], seen_calls: set[str],
) -> Optional[TrajectoryStep]:
    if not isinstance(block, dict):
        return None
    if block.get("type") == "text":
        return _claude_message_step(block, "assistant_message", turn=turn)
    if block.get("type") == "tool_use":
        return _claude_tool_call_step(block, turn, seen_calls)
    return None


def _claude_message_step(
    block: dict[str, Any], kind: str, *, turn: Optional[int] = None,
) -> Optional[TrajectoryStep]:
    message = block.get("text")
    if not isinstance(message, str) or not message:
        return None
    return TrajectoryStep(kind=kind, turn=turn, content=message)


def _claude_tool_call_step(
    block: dict[str, Any], turn: Optional[int], seen_calls: set[str],
) -> Optional[TrajectoryStep]:
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return None
    block_id = block.get("id")
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
        content=block.get("input"),
    )


def _claude_user_steps(
    content: list[Any], seen_results: set[str],
) -> list[TrajectoryStep]:
    """Steps from one user frame's content: ``text`` + ``tool_result`` blocks.

    ``text`` blocks become ``user_message`` turns; ``tool_result`` blocks
    become ``tool_result`` steps de-duplicated by ``tool_use_id`` (which also
    joins each back to its call). These are turn *inputs*, not billed output,
    so they carry no ``turn`` index. The raw ``content`` / ``text`` payload
    rides along verbatim (no redaction here).
    """
    steps: list[TrajectoryStep] = []
    for block in content:
        step = _claude_user_step(block, seen_results)
        if step is not None:
            steps.append(step)
    return steps


def _claude_user_step(
    block: Any, seen_results: set[str],
) -> Optional[TrajectoryStep]:
    if not isinstance(block, dict):
        return None
    if block.get("type") == "text":
        return _claude_message_step(block, "user_message")
    if block.get("type") == "tool_result":
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
        kind="tool_result", tool_id=tool_id, content=block.get("content"),
    )


@dataclass
class _ClaudeTrajectoryBuilder:
    steps: list[TrajectoryStep] = field(default_factory=list)
    seen_calls: set[str] = field(default_factory=set)
    seen_results: set[str] = field(default_factory=set)
    turn_index: dict[str, int] = field(default_factory=dict)

    def add_event(self, idx: int, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type not in ("assistant", "user"):
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        turn = self._turn(idx, event) if event_type == "assistant" else None
        content = message.get("content")
        if not isinstance(content, list):
            return
        if event_type == "assistant":
            self.steps.extend(
                _claude_assistant_steps(content, turn, self.seen_calls)
            )
        else:
            self.steps.extend(_claude_user_steps(content, self.seen_results))

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
    by_key: dict[str, tuple[int, str, dict[str, int]]] = field(
        default_factory=dict,
    )

    def add_event(self, idx: int, event: dict[str, Any]) -> None:
        if event.get("type") != "assistant":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        key = _claude_turn_key(idx, event)
        turn = self.turn_index.setdefault(key, len(self.turn_index))
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.by_key[key] = (
                turn, _claude_model_name(event), _claude_usage_record(usage),
            )

    def build(self) -> tuple[TurnUsage, ...]:
        return tuple(
            _turn_usage_from_row(row)
            for row in sorted(self.by_key.values(), key=lambda item: item[0])
        )


def _turn_usage_from_row(
    row: tuple[int, str, dict[str, int]],
) -> TurnUsage:
    turn, model, record = row
    cost = _claude_estimate_cost(model, record)
    return TurnUsage(
        turn=turn,
        model=model,
        input_tokens=record["input"],
        output_tokens=record["output"],
        cache_read_tokens=record["cache_read"],
        cache_write_tokens=record["cache_write_5m"] + record["cache_write_1h"],
        cost_usd=cost,
        cost_source="estimated" if cost is not None else "unknown-price",
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
        backend="claude",
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
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
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
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_id = self._item_id(item)
        if item.get("type") == "command_execution":
            self._add_command(item, item_id)
        elif item.get("type") == "agent_message":
            self._add_message(item, item_id)

    def _item_id(self, item: dict[str, Any]) -> str:
        raw_id = item.get("id")
        item_id = raw_id if isinstance(raw_id, str) and raw_id else ""
        if item_id and item_id not in self.seen:
            self.seen.add(item_id)
            self.order.append(item_id)
        return item_id

    def _add_command(self, item: dict[str, Any], item_id: str) -> None:
        command = item.get("command")
        has_output = "aggregated_output" in item
        if item_id:
            if isinstance(command, str):
                self.commands[item_id] = command
            if has_output:
                self.outputs[item_id] = item.get("aggregated_output")
            return
        if isinstance(command, str):
            self.anonymous.append(TrajectoryStep(
                kind="tool_call", name="command_execution", content=command,
            ))
        if has_output:
            self.anonymous.append(TrajectoryStep(
                kind="tool_result", content=item.get("aggregated_output"),
            ))

    def _add_message(self, item: dict[str, Any], item_id: str) -> None:
        message = item.get("text")
        if not isinstance(message, str) or not message:
            return
        if item_id:
            self.messages[item_id] = message
        else:
            self.anonymous.append(TrajectoryStep(
                kind="assistant_message", content=message,
            ))

    def build(self) -> tuple[TrajectoryStep, ...]:
        return _codex_assemble_steps(
            self.order,
            self.commands,
            self.outputs,
            self.messages,
            self.anonymous,
        )


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
        if iid in commands:
            steps.append(TrajectoryStep(
                kind="tool_call",
                name="command_execution",
                tool_id=iid,
                content=commands[iid],
            ))
        if iid in outputs:
            steps.append(TrajectoryStep(
                kind="tool_result",
                tool_id=iid,
                content=outputs[iid],
            ))
        if iid in messages:
            steps.append(TrajectoryStep(
                kind="assistant_message",
                content=messages[iid],
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
        backend="codex",
        skills=parse_codex_skills(stdout),
        steps=_codex_trajectory_steps(events),
        final_output=_codex_final_output(events),
    )


def parse_agent_trajectory(backend: str, stdout: str) -> AgentTrajectory:
    """Dispatch by backend name; raise on anything other than claude/codex.

    Mirrors ``parse_agent_usage`` / ``parse_agent_skills`` dispatch so callers
    reuse the same backend string they spawned the agent with.
    """
    if backend == "claude":
        return parse_claude_trajectory(stdout)
    if backend == "codex":
        return parse_codex_trajectory(stdout)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'"
    )
