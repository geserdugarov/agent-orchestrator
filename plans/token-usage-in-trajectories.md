# Token usage in trajectory monitoring — design

## Context

The trajectory viewer (`orchestrator/trajectory_dashboard.py`, backed by the
pure reader `orchestrator/trajectory_reader.py`) renders one *run* — the
ordered timeline of an agent invocation: the orchestrator prompt, then the
interleaved assistant / user text turns and tool calls / results, then the
final output. Records come from the opt-in `agent_trajectory` sink written by
`analytics._maybe_record_trajectory` and classified from the agent's JSONL
stdout by `usage.parse_agent_trajectory`.

Issue [#574](../README.md) asks the viewer to also show, near each step:

1. **how many tokens it spent**;
2. **whether the cache was used**;
3. **which model ran, and an estimated cost.**

The usage/cost machinery to answer all three already exists — it just does not
reach the trajectory surface today. This document designs how to connect them.
**Only the design is in scope for this issue; no code lands here.**

## What the data actually allows

The token/model/cost decoder is `orchestrator/usage.py`. Everything the issue
asks for is derivable from the *same* stdout the trajectory is already parsed
from — but the granularity and per-backend availability need to be stated
honestly, because they drive the whole design.

### Tokens are billed per LLM turn, not per timeline step

A trajectory `step` is fine-grained: `tool_call`, `tool_result`,
`assistant_message`, `user_message`. Tokens, however, are billed **per LLM
request** — i.e. per *assistant turn*. In claude's `--output-format
stream-json`:

- One assistant turn = one `message.id`. Its `message.usage` block carries
  `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and
  `cache_creation_input_tokens` (the exact fields `_claude_usage_record`
  already decodes), and `message.model` names the model (`_claude_model_name`).
- **One turn emits several steps**: a `text` block (→ `assistant_message`) plus
  N `tool_use` blocks (→ N `tool_call`s), all sharing that one `message.id` and
  that one usage record.
- A `tool_result` / `user_message` step does **not** independently spend
  tokens. Its cost shows up as `input_tokens` / `cache_read_input_tokens` on
  the *next* assistant turn (the prompt that turn re-reads).

So "tokens near each step" must mean **usage attached to the assistant turn that
produced the step(s)** — rendered once, at the turn boundary, next to the
assistant output that incurred it. Attaching a copy to every step of a turn
would visually triple-count a turn that fired three tools. This nuance is the
single most important design decision and must be surfaced in the UI copy.

### Per-backend availability

| Signal | claude | codex |
|---|---|---|
| Per-turn input/output tokens | ✅ per `message.id` (`_claude_usage_record`) | ❌ usage frames are **cumulative** across the session; the parser already takes only the *last* total |
| Per-turn cache read / write | ✅ `cache_read_input_tokens` / `cache_creation_input_tokens` | ❌ cumulative `cached_input_tokens` only |
| Per-turn model | ✅ `message.model` | ⚠️ usually one model for the whole session |
| Per-turn cost estimate | ✅ reuse `_CLAUDE_RATES` | ❌ (no per-turn tokens to price) |
| **Run-level** tokens / cache / model / cost | ✅ | ✅ (already computed in `agent_exit`) |

This asymmetry mirrors one the codebase already lives with: `tools` and
`skills_available` are populated for claude and best-effort-empty for codex.
The design follows the same rule — **per-turn detail is claude-only today;
codex gets the run-level summary and a one-line "per-turn not available for this
backend" note** — rather than faking codex per-step numbers by diffing
cumulative counters (fragile, and unconfirmed against a real codex stream).

### Cost is estimated per turn; reported is authoritative per run

`UsageMetrics.cost_source` already distinguishes `reported` (the agent emitted
`total_cost_usd`), `estimated` (from the first-party price table),
`unknown-price`, and `no-usage`. `total_cost_usd` is a **run-level, terminal**
figure — there is no per-turn reported cost. Therefore:

- **Per-turn cost is always an estimate** (`estimated` / `unknown-price`),
  computed from `_CLAUDE_RATES`.
- **The run total stays authoritative** and is shown as the headline number;
  per-turn estimates are labelled "est." and **need not sum to it** (the
  reported total includes server-side rounding the family-rate estimate can't
  reproduce). The UI must say this so the numbers reading slightly differently
  is not read as a bug.

## Where usage lives today vs. where it must go

- Run-level usage/cost/model is already parsed by `usage.parse_agent_usage`
  and written to the **`agent_exit`** analytics record (JSONL → Postgres,
  `orchestrator/analytics/__init__.py:record_agent_exit`).
- The **`agent_trajectory`** record carries the step timeline but *no* usage,
  and the trajectory sink is deliberately kept out of the numeric analytics
  rollup / Postgres (see `docs/observability.md`).
- The viewer reads **only the trajectory JSONL file** — no database, no
  `analytics.sync`. That is a load-bearing property (browse trajectories with
  nothing but the file on disk).

**Decision:** attach usage to the `agent_trajectory` record itself. The numbers
are small integers / floats / a model-name string — not free text — so this
does not reintroduce free-text bodies into a numeric sink, and it keeps the
viewer file-only. Two additions, described next: a small **run-level summary**
(denormalized from the `UsageMetrics` `record_agent_exit` *already* has in
hand) and, for claude, a **per-turn breakdown**.

## Design

Four touch points, in dependency order. Each preserves the existing resilience
contract: a missing / renamed / absent field yields a smaller result, never an
exception, and an old record with no usage renders exactly as it does today.

### 1. `orchestrator/usage.py` — classify per-turn usage

The trajectory classifier already walks every assistant frame; it just drops
the usage. Extend the classifier data model so usage rides alongside the steps
it already produces:

- New frozen dataclass `TurnUsage`: `turn` (0-based index), `model`,
  `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
  `cost_usd` (`Optional[float]`), `cost_source` (`"estimated"` /
  `"unknown-price"`).
- `TrajectoryStep` gains `turn: Optional[int] = None`.
- `AgentTrajectory` gains `turns: tuple[TurnUsage, ...] = ()` (parallel to the
  existing `tools` / `skills` best-effort sections).

`_claude_trajectory_steps` groups by `message.id` in first-seen order to assign
each turn a 0-based index, and stamps that index onto every `tool_call` /
`assistant_message` step it emits from that frame. `tool_result` /
`user_message` steps keep `turn = None` (they are turn *inputs*, not billed
output). A sibling builder computes one `TurnUsage` per `message.id` by reusing
`_claude_usage_record` and `_claude_model_name` (last-record-per-id wins — the
same discipline `parse_claude_usage` already uses for partial-message frames).

**Reuse the price path, don't fork it.** Factor the per-model estimation loop
currently inline in `parse_claude_usage` into a small
`_claude_estimate_cost(model, bucket) -> Optional[float]` and call it from both
the run aggregate and the per-turn builder, so a single price table
(`_CLAUDE_RATES`) stays authoritative and a rate edit can never drift between
the run total and the per-turn numbers.

`parse_codex_trajectory` leaves `turns = ()` and every `step.turn = None` — the
run-level summary (below) is codex's only usage surface for now.

### 2. `orchestrator/analytics/__init__.py` — serialize onto the record

`record_agent_exit` already computes `metrics: UsageMetrics` before it calls
`_maybe_record_trajectory`. Thread that object through
(`_maybe_record_trajectory` → `_build_trajectory_record`) so the trajectory
record can carry a **run-level summary** without re-parsing:

```jsonc
// additions to an `agent_trajectory` record
"run_usage": {                        // denormalized from UsageMetrics
  "models": ["claude-opus-4-8"],
  "input_tokens": 41230,
  "output_tokens": 5120,
  "cached_tokens": 0,                 // codex-style; 0 on claude
  "cache_read_tokens": 812440,
  "cache_write_tokens": 20110,
  "turns": 9,
  "cost_usd": 0.83,
  "cost_source": "reported"           // authoritative run total
},
"turns": [                            // claude only; omitted/[] on codex
  {
    "turn": 0,
    "model": "claude-opus-4-8",
    "input_tokens": 12,
    "output_tokens": 340,
    "cache_read_tokens": 18240,
    "cache_write_tokens": 512,
    "cost_usd": 0.0123,               // estimated
    "cost_source": "estimated"
  }
],
"steps": [
  { "kind": "assistant_message", "turn": 0, "content": "..." },
  { "kind": "tool_call", "name": "Edit", "tool_id": "...", "turn": 0, "content": "..." },
  { "kind": "tool_result", "tool_id": "...", "content": "..." }   // no "turn"
]
```

`run_usage` is essentially `UsageMetrics.to_dict()` minus `backend` (already on
the record). `build_record` drops `None` extras, so on an old code path or a
codex run the `turns` key simply stays absent — the record shape is unchanged
for anyone who does not opt into this.

**Contract notes for this layer:**

- **Redaction:** token counts, cost, `cost_source`, and the model name are
  *not* secret-shaped and need no redaction (the model already ships in
  `models` on `agent_exit`). Only the existing free-text fields keep going
  through `_redact_and_truncate`. No change to the redaction/truncation caps.
- **Record budget:** the per-step usage adds a handful of small numeric fields
  (~80–120 serialized bytes/step). `_build_trajectory_record` already charges
  each step its *full serialized* size against `_TRAJECTORY_RECORD_BUDGET`
  (200 KB), so the budget still bounds the record — a pathological run just
  hits `truncated: true` a few steps sooner. The `turns[]` array is bounded by
  the turn count (≪ step count) and is added to the running total the same way.
- **Fail-open:** everything stays inside the existing
  `_maybe_record_trajectory` guard, so a usage-classification bug logs and is
  swallowed, never touching the baseline `agent_exit` usage/cost record.

### 3. `orchestrator/trajectory_reader.py` — expose usage to the page

Purely additive, all optional, all defensively coerced (the reader must not
crash on a hand-edited or pre-usage line):

- `TrajectoryStepView` gains `turn: Optional[int]` (coerced via `_coerce_int`).
- New `RunUsageView` / `TurnUsageView` frozen views mirroring the record.
- `TrajectoryRun` gains `run_usage: Optional[RunUsageView]` and
  `turns: tuple[TurnUsageView, ...]`, plus convenience properties for the
  viewer: `cost_usd`, `cost_source`, `model` (first of `run_usage.models`),
  `total_tokens`, and a `usage_for_turn(idx)` lookup so a timeline entry can
  find its turn's usage in O(1).
- `TimelineEntry` gains `turn: Optional[int]` so the page can render the
  per-turn line at the boundary while walking the normalized timeline.
- `TrajectorySummary` gains `total_cost_usd: float` (sum over runs that have a
  cost) for a new KPI tile, computed in `summarize`.

`parse_record` reads the new keys with the same tolerant coercions already used
(`_coerce_int`, `_coerce_str`, `_coerce_str_tuple`); a record without them
yields `run_usage=None`, `turns=()`, and `step.turn=None`, and every existing
property (`tool_calls`, `timeline`, `is_fixture`, filters) is unchanged.

### 4. `orchestrator/trajectory_dashboard.py` — render it

Three additions, all reusing the existing `dashboard_theme` chrome
(`fmt_num`, KPI markup, badge styles):

1. **Per-turn usage line at each turn boundary.** While walking `run.timeline`,
   when an entry's `turn` differs from the previous entry's, render a compact
   inline usage strip *above* it: `model · in N tok · out N tok · cache-read N
   · cache-write N · est. $X.XX`. Show a cache indicator (e.g. a "cache hit"
   chip) when `cache_read_tokens > 0`, directly answering "was the cache used".
   `tool_result` / `user_message` entries (`turn = None`) carry no strip — with
   a short legend explaining they are turn inputs, billed on the next turn.
2. **Run-level usage in the detail card.** A small row near the existing
   meta/tools/skills block: total tokens, cache read/write totals, model(s),
   turns, and the authoritative run cost with its `cost_source` (e.g.
   "reported $0.83" vs "estimated $0.79"). This is the codex surface too.
3. **A "Total cost" KPI tile** in the four-/five-tile strip, from
   `summary.total_cost_usd`, so the filtered run set has an at-a-glance spend
   figure — read entirely from the file, no Postgres.

UI copy must state the two honesty points from above: per-turn figures are
**estimates** that may not sum to the reported run total, and per-turn detail is
**claude-only** today (codex shows the run summary and a note).

## Testing

- `tests/test_usage.py` — per-turn extraction: turn indexing across
  multi-tool-use messages and partial-message frames; cache-read/write split;
  per-turn model; `estimated` vs `unknown-price`; codex leaves `turns=()`;
  the refactored `_claude_estimate_cost` still produces the identical run
  aggregate (regression guard on the extraction).
- `tests/test_analytics.py` — `run_usage` denormalization from the passed
  `metrics`; `turns[]` and `step.turn` serialization; `None`/codex records drop
  the keys; the record budget still trips `truncated` with the added fields;
  the fail-open guard still protects the baseline `agent_exit` record.
- `tests/test_trajectory_reader.py` — tolerant parsing of the new fields;
  `usage_for_turn`, `cost_usd`, `total_tokens`, `summary.total_cost_usd`;
  a pre-usage record parses with `run_usage=None` and renders unchanged.
- `tests/test_trajectory_dashboard.py` — the per-turn strip renders only at
  boundaries and never on `turn=None` entries; the run-usage row and cost KPI
  appear; codex path shows the summary + note; keep the lazy-Streamlit-import
  and `sys.path` guards intact.

## Phasing

1. **usage.py** per-turn classification + the `_claude_estimate_cost` refactor
   (self-contained, unit-testable in isolation).
2. **analytics** serialization (`run_usage` + `turns[]` + `step.turn`).
3. **reader** field exposure + `summarize` cost total.
4. **viewer** rendering.

Each step is independently shippable and back-compatible; the viewer degrades
to today's behavior for any record written before step 2.

## Non-goals / open questions

- **No new dependency and no Postgres/DDL change.** Usage stays in the file-only
  trajectory sink; the analytics rollup and dashboard are untouched.
- **Codex per-turn usage** is deferred until a real `codex exec --json` capture
  confirms whether per-turn (non-cumulative) usage can be recovered by diffing
  successive cumulative frames and aligning them to items. Until then, codex is
  run-level only — matching how `tools` / `skills_available` already behave.
- **Docs:** on implementation, update `docs/observability.md` (the
  `agent_trajectory` record shape and the viewer description) in the same
  change, per the developer skill's documentation-drift rule. No docs change
  lands with this design-only issue.
- **Retention/rotation** of the trajectory file is unchanged — the added fields
  do not alter the append/prune discipline.
