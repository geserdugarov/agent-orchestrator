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
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


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


def _find_last_reported_cost(events: list[dict[str, Any]]) -> Optional[float]:
    """Return the final ``total_cost_usd`` observed anywhere in the stream.

    Both backends emit this on the terminal/result frame, but Codex sometimes
    nests it deeper than the top level; walk every object so a deeper path
    still wins over an estimate.
    """
    last: Optional[float] = None
    for ev in events:
        for obj in _walk_objects(ev):
            value = obj.get("total_cost_usd")
            if value is None:
                continue
            if isinstance(value, (int, float)):
                last = float(value)
            elif isinstance(value, str):
                try:
                    last = float(value)
                except ValueError:
                    pass
    return last


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


# --- claude parser ----------------------------------------------------------

def _claude_model_name(event: dict[str, Any]) -> str:
    msg = event.get("message")
    if isinstance(msg, dict):
        m = msg.get("model")
        if isinstance(m, str) and m:
            return m
    nested = event.get("event")
    if isinstance(nested, dict):
        n_msg = nested.get("message")
        if isinstance(n_msg, dict):
            m = n_msg.get("model")
            if isinstance(m, str) and m:
                return m
    m = event.get("model")
    if isinstance(m, str) and m:
        return m
    resp = event.get("response")
    if isinstance(resp, dict):
        m = resp.get("model")
        if isinstance(m, str) and m:
            return m
    return "unknown"


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


def _claude_usage_records(
    events: list[dict[str, Any]],
) -> list[tuple[int, str, dict[str, int]]]:
    """Group claude usage into ``(idx, model, record)`` rows, last frame wins.

    Per-message usage events are keyed by ``message.id`` (falling back to
    ``request_id`` then stream position) and the last occurrence of each id
    overwrites earlier ones -- Claude streams partial usage on intermediate
    frames and the final frame carries the authoritative count. Rows are
    returned sorted by each id's final stored frame index (its last
    occurrence). When no assistant usage events exist we fall back to the
    terminal ``type:"result"`` frame's ``usage`` block.
    """
    by_id: dict[str, tuple[int, str, dict[str, int]]] = {}
    for idx, ev in enumerate(events):
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        msg_id = msg.get("id") or ev.get("request_id") or str(idx)
        by_id[msg_id] = (idx, _claude_model_name(ev), _claude_usage_record(usage))

    if by_id:
        return [v for _, v in sorted(by_id.items(), key=lambda kv: kv[1][0])]

    records: list[tuple[int, str, dict[str, int]]] = []
    for idx, ev in enumerate(events):
        if ev.get("type") != "result":
            continue
        usage = ev.get("usage")
        if not isinstance(usage, dict):
            continue
        records.append(
            (idx, _claude_model_name(ev), _claude_usage_record(usage))
        )
    return records


def _claude_aggregate_by_model(
    records: list[tuple[int, str, dict[str, int]]],
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Sum usage records into per-model token buckets, keeping first-seen order.

    Per-model aggregation (rather than one flat total) keeps each model's
    tokens together so ``_claude_estimate_total`` can price a mixed-model run
    at each model's own rate.
    """
    per_model: dict[str, dict[str, int]] = {}
    model_order: list[str] = []
    for _, model, rec in records:
        bucket = per_model.setdefault(
            model,
            {"input": 0, "cache_write_5m": 0, "cache_write_1h": 0,
             "cache_read": 0, "output": 0},
        )
        if model not in model_order:
            model_order.append(model)
        for k, v in rec.items():
            bucket[k] += v
    return per_model, model_order


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
    records: list[tuple[int, str, dict[str, int]]],
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
    per_model, model_order = _claude_aggregate_by_model(records)

    for bucket in per_model.values():
        metrics.input_tokens += bucket["input"]
        metrics.output_tokens += bucket["output"]
        metrics.cache_read_tokens += bucket["cache_read"]
        metrics.cache_write_tokens += (
            bucket["cache_write_5m"] + bucket["cache_write_1h"]
        )
    metrics.models = _dedup_models(model_order)

    reported = _find_last_reported_cost(events)
    estimated = _claude_estimate_total(per_model)
    metrics.cost_usd, metrics.cost_source = _select_cost(
        reported, estimated, bool(records)
    )

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


def _codex_known_model(value: Any) -> Optional[str]:
    if isinstance(value, str) and value and value != "unknown":
        return value
    return None


_CODEX_MODEL_KEYS: tuple[str, ...] = (
    "model",
)
_CODEX_MODEL_NESTED: tuple[tuple[str, ...], ...] = (
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
    for key in _CODEX_MODEL_KEYS:
        m = _codex_known_model(event.get(key))
        if m:
            return m
    for path in _CODEX_MODEL_NESTED:
        cur: Any = event
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        m = _codex_known_model(cur)
        if m:
            return m
    if usage is not None:
        m = _codex_known_model(usage.get("model"))
        if m:
            return m
    return "unknown"


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
    chosen = _codex_known_model(last_model)
    if chosen is None:
        for ev in events:
            for obj in _walk_objects(ev):
                cand = _codex_known_model(obj.get("model"))
                if cand:
                    chosen = cand
        if chosen is None and fallback_model:
            chosen = _codex_known_model(fallback_model)
    return chosen


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
    cached = usage["cached"]
    # Codex/OpenAI reports input_tokens as the *total* prompt count and
    # cached_input_tokens as the portion of that prompt served from cache.
    # Bill the non-cached remainder at the input rate; bill the cached
    # portion at the cached rate when published, otherwise leave the
    # estimate unknown rather than overcharge.
    uncached = max(usage["input"] - cached, 0)
    cached_rate = rates["cached"]
    # Long-context tier: some Codex SKUs (e.g. gpt-5.5) bill the
    # entire session at elevated rates once total input crosses a
    # threshold. The multipliers default to 1.0 (no change) for any
    # rate entry without long-context keys, so flat-priced families
    # are unaffected.
    threshold = rates.get("long_context_threshold")
    input_mult = 1.0
    output_mult = 1.0
    if threshold is not None and usage["input"] > threshold:
        input_mult = rates.get("long_context_input_mult") or 1.0
        output_mult = rates.get("long_context_output_mult") or 1.0
    if cached > 0 and cached_rate is None:
        return None
    cr = cached_rate if cached_rate is not None else rates["input"]
    return (
        uncached * rates["input"] * input_mult
        + cached * cr * input_mult
        + usage["output"] * rates["output"] * output_mult
    ) / 1_000_000


def _codex_turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    """Turn count for a codex run: a ``num_turns`` reported anywhere in stream.

    Falls back to counting ``turn_complete``-typed events -- codex has no
    per-turn usage frame -- and returns ``None`` when neither signal is present.
    """
    num_turns: Optional[int] = None
    for ev in events:
        for obj in _walk_objects(ev):
            nt = obj.get("num_turns")
            if isinstance(nt, (int, float)):
                num_turns = int(nt)
    if num_turns is None:
        count = 0
        for ev in events:
            t = ev.get("type")
            if isinstance(t, str) and _TURN_COMPLETE_RE.search(t):
                count += 1
        num_turns = count or None
    return num_turns


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

    usage_events = _codex_usage_events(events)
    if usage_events:
        last_model, last_usage = usage_events[-1]
    else:
        last_model, last_usage = "unknown", {"input": 0, "cached": 0, "output": 0}

    chosen_model = _codex_select_model(events, last_model, fallback_model)
    model_label = chosen_model or "unknown"

    metrics.input_tokens = last_usage["input"]
    metrics.cached_tokens = last_usage["cached"]
    metrics.output_tokens = last_usage["output"]
    if chosen_model is not None:
        metrics.models = (chosen_model,)

    reported = _find_last_reported_cost(events)
    estimated = _codex_estimate_cost(model_label, last_usage)
    metrics.cost_usd, metrics.cost_source = _select_cost(
        reported, estimated, bool(usage_events)
    )

    metrics.turns = _codex_turn_count(events)
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
    for ev in events:
        if ev.get("type") != "system" or ev.get("subtype") != "init":
            continue
        skills = ev.get("skills")
        if not isinstance(skills, list):
            return ()
        order: list[str] = []
        seen: set[str] = set()
        for name in skills:
            if isinstance(name, str) and name and name not in seen:
                seen.add(name)
                order.append(name)
        return tuple(order)
    return ()


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
    names: list[str] = []
    seen_ids: set[str] = set()
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            name = _claude_skill_name(block)
            if name is None:
                continue
            block_id = block.get("id")
            if isinstance(block_id, str) and block_id:
                if block_id in seen_ids:
                    continue
                seen_ids.add(block_id)
            names.append(name)
    return _collect(names, available=_claude_offered_skills(events))


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
    by_id: dict[str, list[str]] = {}
    id_order: list[str] = []
    anon: list[str] = []
    for ev in _iter_events(stdout):
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        names = _CODEX_SKILL_PATH_RE.findall(command)
        if not names:
            continue
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            if item_id not in by_id:
                id_order.append(item_id)
            by_id[item_id] = names
        else:
            anon.extend(names)
    flat: list[str] = []
    for item_id in id_order:
        flat.extend(by_id[item_id])
    flat.extend(anon)
    return _collect(flat)


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
    for ev in events:
        if ev.get("type") != "system" or ev.get("subtype") != "init":
            continue
        tools = ev.get("tools")
        if not isinstance(tools, list):
            return ()
        order: list[str] = []
        seen: set[str] = set()
        for name in tools:
            if isinstance(name, str) and name and name not in seen:
                seen.add(name)
                order.append(name)
        return tuple(order)
    return ()


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
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                steps.append(TrajectoryStep(
                    kind="assistant_message",
                    turn=turn,
                    content=text,
                ))
        elif btype == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            bid = block.get("id")
            tool_id = bid if isinstance(bid, str) and bid else ""
            if tool_id:
                if tool_id in seen_calls:
                    continue
                seen_calls.add(tool_id)
            steps.append(TrajectoryStep(
                kind="tool_call",
                name=name,
                tool_id=tool_id,
                turn=turn,
                content=block.get("input"),
            ))
    return steps


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
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                steps.append(TrajectoryStep(
                    kind="user_message",
                    content=text,
                ))
        elif btype == "tool_result":
            rid = block.get("tool_use_id")
            tool_id = rid if isinstance(rid, str) and rid else ""
            if tool_id:
                if tool_id in seen_results:
                    continue
                seen_results.add(tool_id)
            steps.append(TrajectoryStep(
                kind="tool_result",
                tool_id=tool_id,
                content=block.get("content"),
            ))
    return steps


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
    steps: list[TrajectoryStep] = []
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    turn_index: dict[str, int] = {}
    for idx, ev in enumerate(events):
        etype = ev.get("type")
        if etype not in ("assistant", "user"):
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        turn: Optional[int] = None
        if etype == "assistant":
            turn = turn_index.setdefault(
                _claude_turn_key(idx, ev), len(turn_index)
            )
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if etype == "assistant":
            steps.extend(_claude_assistant_steps(content, turn, seen_calls))
        else:
            steps.extend(_claude_user_steps(content, seen_results))
    return tuple(steps)


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
    turn_index: dict[str, int] = {}
    by_key: dict[str, tuple[int, str, dict[str, int]]] = {}
    for idx, ev in enumerate(events):
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        key = _claude_turn_key(idx, ev)
        turn = turn_index.setdefault(key, len(turn_index))
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        by_key[key] = (
            turn, _claude_model_name(ev), _claude_usage_record(usage)
        )
    turns: list[TurnUsage] = []
    for turn, model, rec in sorted(by_key.values(), key=lambda t: t[0]):
        cost = _claude_estimate_cost(model, rec)
        turns.append(TurnUsage(
            turn=turn,
            model=model,
            input_tokens=rec["input"],
            output_tokens=rec["output"],
            cache_read_tokens=rec["cache_read"],
            cache_write_tokens=rec["cache_write_5m"] + rec["cache_write_1h"],
            cost_usd=cost,
            cost_source="estimated" if cost is not None else "unknown-price",
        ))
    return tuple(turns)


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
    order: list[str] = []
    seen: set[str] = set()
    commands: dict[str, str] = {}
    outputs: dict[str, Any] = {}
    messages: dict[str, str] = {}
    anon: list[TrajectoryStep] = []
    for ev in events:
        item = ev.get("item")
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "command_execution":
            command = item.get("command")
            has_output = "aggregated_output" in item
            iid = item.get("id")
            if isinstance(iid, str) and iid:
                if iid not in seen:
                    seen.add(iid)
                    order.append(iid)
                if isinstance(command, str):
                    commands[iid] = command
                if has_output:
                    outputs[iid] = item.get("aggregated_output")
            else:
                if isinstance(command, str):
                    anon.append(TrajectoryStep(
                        kind="tool_call",
                        name="command_execution",
                        content=command,
                    ))
                if has_output:
                    anon.append(TrajectoryStep(
                        kind="tool_result",
                        content=item.get("aggregated_output"),
                    ))
        elif itype == "agent_message":
            text = item.get("text")
            if not isinstance(text, str) or not text:
                continue
            iid = item.get("id")
            if isinstance(iid, str) and iid:
                if iid not in seen:
                    seen.add(iid)
                    order.append(iid)
                messages[iid] = text
            else:
                anon.append(TrajectoryStep(
                    kind="assistant_message",
                    content=text,
                ))
    return _codex_assemble_steps(order, commands, outputs, messages, anon)


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
