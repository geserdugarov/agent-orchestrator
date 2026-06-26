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
        records = [v for _, v in sorted(by_id.items(), key=lambda kv: kv[1][0])]
    else:
        records = []
        for idx, ev in enumerate(events):
            if ev.get("type") != "result":
                continue
            usage = ev.get("usage")
            if not isinstance(usage, dict):
                continue
            records.append(
                (idx, _claude_model_name(ev), _claude_usage_record(usage))
            )

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

    for bucket in per_model.values():
        metrics.input_tokens += bucket["input"]
        metrics.output_tokens += bucket["output"]
        metrics.cache_read_tokens += bucket["cache_read"]
        metrics.cache_write_tokens += (
            bucket["cache_write_5m"] + bucket["cache_write_1h"]
        )
    metrics.models = _dedup_models(model_order)

    reported = _find_last_reported_cost(events)

    estimated: Optional[float] = None
    if per_model:
        parts: list[float] = []
        priced_all = True
        for model, bucket in per_model.items():
            rates = _claude_rates(model)
            if rates is None:
                priced_all = False
                break
            parts.append(
                (
                    bucket["input"] * rates["input"]
                    + bucket["cache_write_5m"] * rates["cache_write_5m"]
                    + bucket["cache_write_1h"] * rates["cache_write_1h"]
                    + bucket["cache_read"] * rates["cache_read"]
                    + bucket["output"] * rates["output"]
                )
                / 1_000_000
            )
        if priced_all:
            estimated = sum(parts)

    if reported is not None:
        metrics.cost_usd = reported
        metrics.cost_source = "reported"
    elif estimated is not None:
        metrics.cost_usd = estimated
        metrics.cost_source = "estimated"
    elif not records:
        metrics.cost_source = "no-usage"
    else:
        metrics.cost_source = "unknown-price"

    num_turns = None
    for ev in events:
        if ev.get("type") == "result":
            nt = ev.get("num_turns")
            if isinstance(nt, (int, float)):
                num_turns = int(nt)
    if num_turns is None and records:
        num_turns = len(records)
    metrics.turns = num_turns
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

    if usage_events:
        last_model, last_usage = usage_events[-1]
    else:
        last_model, last_usage = "unknown", {"input": 0, "cached": 0, "output": 0}

    chosen_model: Optional[str] = _codex_known_model(last_model)
    if chosen_model is None:
        for ev in events:
            for obj in _walk_objects(ev):
                cand = _codex_known_model(obj.get("model"))
                if cand:
                    chosen_model = cand
        if chosen_model is None and fallback_model:
            chosen_model = _codex_known_model(fallback_model)

    model_label = chosen_model or "unknown"

    metrics.input_tokens = last_usage["input"]
    metrics.cached_tokens = last_usage["cached"]
    metrics.output_tokens = last_usage["output"]
    if chosen_model is not None:
        metrics.models = (chosen_model,)

    reported = _find_last_reported_cost(events)

    estimated: Optional[float] = None
    rates = _codex_rates(model_label)
    if rates is not None and (last_usage["input"] + last_usage["output"]) > 0:
        cached = last_usage["cached"]
        # Codex/OpenAI reports input_tokens as the *total* prompt count and
        # cached_input_tokens as the portion of that prompt served from cache.
        # Bill the non-cached remainder at the input rate; bill the cached
        # portion at the cached rate when published, otherwise leave the
        # estimate unknown rather than overcharge.
        uncached = max(last_usage["input"] - cached, 0)
        cached_rate = rates["cached"]
        # Long-context tier: some Codex SKUs (e.g. gpt-5.5) bill the
        # entire session at elevated rates once total input crosses a
        # threshold. The multipliers default to 1.0 (no change) for any
        # rate entry without long-context keys, so flat-priced families
        # are unaffected.
        threshold = rates.get("long_context_threshold")
        input_mult = 1.0
        output_mult = 1.0
        if threshold is not None and last_usage["input"] > threshold:
            input_mult = rates.get("long_context_input_mult") or 1.0
            output_mult = rates.get("long_context_output_mult") or 1.0
        if cached > 0 and cached_rate is None:
            estimated = None
        else:
            cr = cached_rate if cached_rate is not None else rates["input"]
            estimated = (
                uncached * rates["input"] * input_mult
                + cached * cr * input_mult
                + last_usage["output"] * rates["output"] * output_mult
            ) / 1_000_000

    if reported is not None:
        metrics.cost_usd = reported
        metrics.cost_source = "reported"
    elif estimated is not None:
        metrics.cost_usd = estimated
        metrics.cost_source = "estimated"
    elif not usage_events:
        metrics.cost_source = "no-usage"
    else:
        metrics.cost_source = "unknown-price"

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
    metrics.turns = num_turns
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
