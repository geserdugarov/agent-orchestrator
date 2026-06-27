# Context Monitoring — Sketch

## Context

Observability today is **counts, not content**. The analytics `agent_exit`
record carries token usage (input / output / cached / cache-read /
cache-write) per model and cost, and — opt-in — the *names* of skills a
run triggered ([`../docs/observability.md`](../docs/observability.md#agent_exit-records)).
What it deliberately does **not** carry is the resolving *trajectory*: the actual
text an agent saw and produced. The current sink is explicit about this —
"Prompts, raw stdout / stderr, secrets, and worktree contents are
deliberately NOT stored — the sink is a usage / cost surface, not a
debugging mirror."

This issue asks for the missing surface: a way to inspect *how* a run
reasoned through an issue, broken into six elements —

1. **user input**,
2. **skill**,
3. **system prompt**,
4. **tools metadata**,
5. **tools call results**,
6. **output**.

This document is a sketch of how that could be captured. **Scope of the
picked-up issue is this design note only** — no implementation lands here.

## Where each element already lives

The good news: the data for five of the six elements already flows through
`agents.AgentResult.stdout` — the raw JSONL event stream the agent CLI
emits (`claude --output-format stream-json --include-partial-messages`, or
`codex … --json`; [`../orchestrator/agents.py`](../orchestrator/agents.py)).
`usage.py` already decodes that stream line-by-line via `_iter_events` for
tokens, cost, and skill names. But "in the stream" is not the same as
"readable today" for all five:

- **Confident, available today** — *skill*, *tools call results*, and
  *output* sit in frames the parser already reads (`Skill` / `tool_use` /
  `tool_result` blocks and the final assistant message), so they are
  confirmed current data.
- **Best-effort capture tasks** — *system prompt* and *tools metadata* are
  also in the stream but at field paths **not yet confirmed** against a
  captured run, and they differ per backend. They are not confirmed
  current data; pinning their exact location is a prerequisite (see Open
  questions).

The sixth element (*user input*) is the only one not in the stream at all:
it is the prompt the orchestrator itself builds and passes in, which it
holds in hand at the call site.

| # | Element | Source | Status |
|---|---|---|---|
| 1 | **user input** | the orchestrator-built prompt string (`workflow_messages._build_implement_prompt` / `_build_review_prompt` / `_build_fix_prompt` / …), which embeds the issue body, recent comments, and reviewer feedback; plus the `resume_session_id` on a resumed run | held in memory at the `run_agent` call site — **not** in the agent stream |
| 2 | **skill** | `Skill`-named `tool_use` blocks in the `assistant` stream | parsed today (names only) by `usage.parse_claude_skills` when `TRACK_SKILL_TRIGGERS` is on |
| 3 | **system prompt** | the CLI harness's `system` / `init` frame — best-effort: the headless docs frame it as model / tools / MCP / plugins, not a guaranteed verbatim system prompt; the orchestrator sets no custom system prompt itself | **best-effort capture task** — in the stream, but the exact field path is unconfirmed (see Open questions); not confirmed current data |
| 4 | **tools metadata** | the `tools` array in that same `init` frame (offered tool names / definitions) | **best-effort capture task** — in the stream, but the exact field path is unconfirmed (see Open questions); not confirmed current data |
| 5 | **tools call results** | `tool_use` (the call + its `input`) and `tool_result` (the returned content) blocks across the `assistant` / `user` frames | in the stream, parsed-over today |
| 6 | **output** | the final assistant message — already extracted as `AgentResult.last_message` (`agents._claude_last_message`) | captured today (used for verdict parsing), not persisted as a trajectory |

So the work is **not** a new subsystem or a new agent invocation: it is one
more structured pass over a stdout string already in memory, joined to the
prompt the orchestrator already built, written to its own sink. The
stream-schema knowledge stays in one file (`usage.py`), exactly as the
skill extractor does.

## Goal / non-goals

**Goal.** A new, opt-in, observation-only **trajectory sink** that records
one structured record per tracked agent run, capturing the six elements
above so an operator can reconstruct how a run resolved an issue. Defaults
**off**; a default install's on-disk surfaces are byte-for-byte unchanged.

**Non-goals.**

- *No enforcement / no feedback loop.* The sink observes; the polling tick
  never reads it back. The
  [observation-only invariant](../docs/observability.md) — every dispatch
  decision keys off the pinned `<!--orchestrator-state …-->` comment, and
  every sink is safe to truncate or delete — is load-bearing and preserved.
- *No new agent run and no prompt changes.* We capture what already flows
  through `run_agent`; we do not re-run, re-prompt, or alter agent
  behavior.
- *Not the usage sink.* Trajectories are large free text; they do **not**
  belong in `ANALYTICS_LOG_PATH` next to the numeric usage rollup (that
  sink's "no prompts / no stdout" contract stays intact). This is a
  separate surface with its own switch, path, and retention.
- *Not a verbatim secret mirror.* Captured text is redacted (see Privacy).

## Design sketch

### 1. A separate trajectory sink

A new path-gated JSONL sink — call it `TRAJECTORY_LOG_PATH` — parsed in the
analytics package beside its siblings, mirroring the `EVENT_LOG_PATH` /
`ANALYTICS_LOG_PATH` shape (reopen-append per record, `mkdir -p` parent,
`OSError` downgraded to `log.warning`, empty / `off` / `disabled` / `none`
disables). One **record per tracked agent run**, keyed to the same context
the `agent_exit` record carries — `ts`, `repo`, `issue`, `stage`,
`agent_role`, `backend`, `review_round`, `retry_count`, `session_id` — so
a trajectory joins back to its usage / cost row by `session_id`.

Because trajectory bodies can be large, large records are kept out of the
analytics file and database entirely; the trajectory file is its own
rotation / retention domain (operator-managed `logrotate`, plus an optional
`TRAJECTORY_RETENTION_DAYS` pruner reusing the analytics prune shape).

### 2. The trajectory record

A `parse_agent_trajectory(backend, stdout) -> Trajectory` extractor next to
`parse_agent_usage` / `parse_agent_skills`, reusing `_iter_events` and the
same resilience contract (malformed JSONL lines skipped; a missing / renamed
field yields an empty section, never an exception). It classifies stream
frames into the structured shape:

```jsonc
{
  "ts": "…", "repo": "owner/name", "issue": 483, "stage": "implementing",
  "agent_role": "developer", "backend": "claude", "session_id": "…",
  "user_input":    "<orchestrator-built prompt, redacted>",
  "system_prompt": "<from system/init frame, best-effort>",
  "tools":         ["Read", "Edit", "Bash", "Skill", …],
  "skills":        ["develop"],
  "steps": [
    {"type": "tool_call",   "name": "Bash",  "input": "…redacted…"},
    {"type": "tool_result", "name": "Bash",  "content": "…redacted, truncated…"},
    …
  ],
  "output": "<final assistant message>"
}
```

- `user_input` is supplied by the caller (the prompt is not in the stream).
- `system_prompt` and `tools` come from the `init` frame and are
  **best-effort**: their exact field paths are unconfirmed against a
  captured stream and differ per backend (codex's event shape is a capture
  task), so they degrade to empty rather than block the record. The
  *skills* sub-array of that same claude `init` frame is already captured
  and confirmed — it backs the shipped `skills_available` field
  ([`../docs/observability.md`](../docs/observability.md#agent_exit-records)) —
  but the system-prompt text and full tools list are separate fields that
  still need a captured stream.
- `steps` is the ordered `tool_use` → `tool_result` interleave (elements 4
  and 5). `skills` is the existing names-only list (element 2). `output` is
  `AgentResult.last_message` (element 6).

### 3. Integration point

The same boundary the skill extractor uses:
`analytics.record_agent_exit`
([`../orchestrator/analytics/__init__.py`](../orchestrator/analytics/__init__.py)),
called once per tracked run from `workflow._run_agent_tracked`. It already
holds `result.stdout` and the run context; add the prompt to the data it
receives so `user_input` can be attached. **Fail-open, in its own inner
try/except** — exactly as the skill parse does — so a trajectory-parser or
sink-IO failure logs via `log.exception` and never drops the baseline
usage / cost `agent_exit` record or stalls the per-issue tick.

### 4. Configuration

One path switch (`TRAJECTORY_LOG_PATH`, default unset = off) plus an
optional `TRAJECTORY_RETENTION_DAYS`, documented in
[`../docs/configuration.md`](../docs/configuration.md) and
[`../docs/observability.md`](../docs/observability.md) in the shipping PR.
Off means the extractor never runs and no file is opened — zero extra parse
cost, zero new bytes.

## Privacy / trust

This sink **inverts** the usage sink's "no prompts / no stdout" stance, so
its trust story must be explicit and the default must be off.

- **Redaction is mandatory.** Every captured string passes through
  `workflow_messages._redact_secrets` before it is written, the same pass
  that already scrubs provider keys and `GITHUB_TOKEN` from agent stderr
  before it reaches GitHub. Redaction runs on `user_input`, `system_prompt`,
  every `tool_call.input`, every `tool_result.content`, and `output`.
- **It still captures issue / repo content.** Even redacted, prompts and
  tool results echo issue text and source files. That is the point of the
  surface, but it is why it is opt-in, default off, local-filesystem only
  (never posted to GitHub, never synced to Postgres), and safe to delete.
- **Tool-result truncation.** A single `Bash`/`Read` result can be huge;
  cap per-step content (and total record size) with a documented head /
  tail truncation marker so one pathological step cannot bloat the file.

## Cost

- **Zero new dependencies** — stdlib `json`, reusing the `usage.py` event
  iterator and the existing redaction helper.
- **One extra walk** over a stdout string already in memory, plus one
  append, only when the switch is on. Inert otherwise.
- **Disk, not CPU, is the budget.** Bounded by the truncation caps and
  `TRAJECTORY_RETENTION_DAYS`; the file is the only growth surface.

## Considered but rejected

- **Folding trajectories into the analytics sink / Postgres.** Rejected:
  bloats the numeric usage surface, breaks its "no prompts / no stdout"
  contract, and forces large free text through the dedup / rollup pipeline
  that exists for metrics.
- **Storing raw stdout verbatim.** Rejected: unredacted secret-exfiltration
  surface and unbounded size. The structured + redacted + truncated record
  keeps the six elements legible without the raw blob.
- **Capturing worktree file contents or the full git diff.** Rejected:
  out of scope — the PR diff is already the durable record of what changed;
  this surface is about the *reasoning* trajectory, not the artifact.
- **Reading it back into the tick** (e.g. feeding prior trajectories into a
  resume prompt). Rejected here: that is a context-memory feature
  (cf. the roadmap's "Repo memory across issues"), not observability, and
  would break the observation-only invariant.

## Open questions

- **`init`-frame field paths (capture task).** The exact stream location of
  the system-prompt text and offered-tools list is unconfirmed per backend.
  The claude `init` frame's *skills* sub-array is already captured (it backs
  the shipped `skills_available` field —
  [`../docs/observability.md`](../docs/observability.md#agent_exit-records)),
  but the system prompt and full tools list are separate, still-unconfirmed
  fields. Ship `user_input` / `skills` / `steps` / `output` (the confident
  elements) first; fill `system_prompt` / `tools` once a real stream pins
  the fields.
- **Codex event shape (capture task).** Codex's `--json` step / tool-result
  frames need a captured sample, exactly as the codex skill extractor does;
  codex stays best-effort until then.
- **Truncation limits.** Per-step and per-record caps need tuning against
  real runs so trajectories stay legible without unbounded files.
- **Retention vs. forensics.** A short default `TRAJECTORY_RETENTION_DAYS`
  bounds disk and PII exposure but shortens the debugging window; the
  trade-off is operator-chosen.

## Sequencing

0. **Stream capture (prerequisite).** Capture real skill-bearing claude and
   codex streams to pin the `init`-frame and tool-result event shapes
   (shared with the skill-tracking capture task).
1. **Extractor + trajectory sink + opt-in switch.** One logical commit:
   `parse_agent_trajectory` in `usage.py`, the `record_agent_exit` wiring
   (fail-open inner try/except, prompt passed in for `user_input`,
   redaction + truncation applied), the `TRAJECTORY_LOG_PATH` /
   `TRAJECTORY_RETENTION_DAYS` knobs, unit tests over captured streams, and
   the `docs/observability.md` / `docs/configuration.md` updates.
2. **(Optional) Trajectory viewer.** A read-only CLI or dashboard tab that
   renders one run's six elements top-to-bottom — a pure read-side addition
   once trajectories accumulate.

Step 1 stands alone and is fully reversible by clearing the switch.
