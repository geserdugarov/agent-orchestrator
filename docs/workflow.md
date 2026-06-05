# Workflow — agent roles and command specs

This file documents the agent-role side of the workflow: which stage invokes which role, how the role command specs (`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`) are parsed, and how the spec used by an in-flight issue is pinned for the life of its session.

For the full stage-by-stage state machine (label semantics, per-stage handler internals, per-tick flow), see [`state-machine.md`](state-machine.md). For the higher-level design (multi-repo dispatch, push hardening, agent subprocess shape), see [`architecture.md`](architecture.md). For the audit event log, analytics sink, and usage parser, see [`observability.md`](observability.md). For env vars and run modes, see [`configuration.md`](configuration.md). For the user-facing summary, see [`../README.md`](../README.md).

## Roles and the workflow stages that invoke them

The workflow has three agent roles, each spawned by a different set of stage handlers. Roles are independent: each can use `codex` or `claude` and each carries its own optional CLI args.

| Role             | Env var            | Default  | Stage handler(s) that spawn it                                          | Session shape                                 |
| ---------------- | ------------------ | -------- | ----------------------------------------------------------------------- | --------------------------------------------- |
| Decomposer       | `DECOMPOSE_AGENT`  | `claude` | `_handle_decomposing` (and its `awaiting_human` resume); `_handle_question` (and its `awaiting_human` resume) reuses the same backend | Locked per issue after first spawn (decomposing → `decomposer_agent`; question → `question_agent`, a separate pin) |
| Implementer / dev| `DEV_AGENT`        | `claude` | `_handle_implementing`, `_handle_documenting`, `_handle_validating` (fix loop + awaiting-human resume), `_handle_fixing` (PR-comment quiet-window resume), `_handle_resolving_conflict` (conflict resume + awaiting-human resume) | Locked per issue after first spawn |
| Reviewer         | `REVIEW_AGENT`     | `codex`  | `_handle_validating` (fresh every round)                                | Fresh per round; current config always wins   |

The defaults (`claude` decomposes, `claude` implements, `codex` reviews) use both backends; both CLIs need to be authenticated on the host before the orchestrator starts.

The stage handlers themselves live under `orchestrator/stages/` (see the module map in [`architecture.md#top-level-layout`](architecture.md#top-level-layout)). The per-stage handler internals — entry checks, drift handling, post-agent dispositions, label transitions — are documented in [`state-machine.md#stage-handlers`](state-machine.md#stage-handlers). What follows is the role-specific glue.

- **Dev session reuse.** The implementer session is spawned once in `_handle_implementing` and then resumed by `_handle_documenting`, `_handle_validating`, `_handle_fixing`, and `_handle_resolving_conflict` whenever they need the dev to make a change. The locked `(backend, args)` spec is re-parsed on every resume from pinned `dev_agent` so a config flip mid-flight cannot retarget the session.
- **Reviewer freshness.** `_handle_validating` spawns a fresh reviewer subprocess every round with no resume, so `REVIEW_AGENT` changes take effect on the next validating tick. The current value is recorded in `review_agent` for traceability only.
- **Decomposer reuse.** `_handle_decomposing` spawns the decomposer once and resumes it on every awaiting-human reply. The `question` stage reads `DECOMPOSE_AGENT` only as the *fallback* on the first-ever question spawn, then pins what it ran under to `question_agent` (a separate key) so a multi-turn Q&A keeps its own lock independent of any decomposing session on the same issue.

### Question stage — read-only Q&A on the `question` label

The `question` workflow label is operator-applied: there are no automatic transitions in or out. `_handle_question` runs the configured `DECOMPOSE_AGENT` backend in the issue's per-issue worktree (recreated from `<remote>/<base>` each spawn) with a read-only prompt that forbids modifying, committing, or pushing files. The agent's answer (or its own clarifying follow-up question) is posted as a comment pinging `HITL_HANDLE`; no PR is opened. Subsequent human replies resume the locked session, so a multi-turn Q&A keeps the same backend + args.

A read-only violation (commits, dirty tree, timeout) parks awaiting human AND preserves the worktree for operator inspection; the per-tick base sync is skipped while the label is `question` so `<remote>/<base>` is not merged over that inspection state. Closing the issue is the terminal signal: the closed-`question` sweep flips the issue to `done` and tears down the worktree.

For the per-`park_reason` semantics and the implementing-side relabel guard (`question_unsafe_relabel`), see [`state-machine.md#_handle_question-label-question`](state-machine.md#_handle_question-label-question).

### Local verify gate (not an agent)

After the reviewer emits `VERDICT: APPROVED`, `_handle_validating` runs the configured `VERIFY_COMMANDS` directly in the per-issue worktree — these are plain shell commands, not an agent role, so no `*_AGENT` env var applies. The gate runs before the approval comment, the squash, the watermark seeding, and the `documenting` (final-docs) label flip. A clean run advances the issue; any failure parks on `validating` with a typed `park_reason` (`verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`). See [`configuration.md#local-verification-gate`](configuration.md#local-verification-gate) for the env-var reference.

## Spec format

`config._parse_agent_spec` runs `shlex.split` over each role's env value and yields `(backend, extra_args)`:

- **First token rule** — must match `codex` or `claude` case-insensitively (`_parse_agent_spec` compares `tokens[0].lower()`, so `CODEX`, `Claude`, and `codex` all parse to the same backend). The lowercased form is used only for dispatch (`agents.run_agent` keys off it).

  Pinned state stores the **raw spec string verbatim** with its original casing — `DEV_AGENT=CODEX -m gpt-5.5` is persisted as the literal `"CODEX -m gpt-5.5"`, and the re-lowercase happens again on every resume when `_parse_agent_spec` re-parses the stored value.

  Anything else (full path, alias, typo, empty string, unbalanced quotes) aborts at import with a `SystemExit` so a misconfiguration cannot silently fall back to a default backend on the next restart. `DECOMPOSE_AGENT` is parsed at import even when `DECOMPOSE=off`, so toggling the kill switch back on never surfaces a fresh "that env var was always invalid" failure.
- **Remaining tokens** — forwarded verbatim as backend-CLI args on every spawn for that role. Quoting follows shell rules, so values containing `=`, spaces, or nested quotes survive (e.g. `codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`).

  For codex the args are placed before the `exec` subcommand (they are codex global options); for claude they are placed right after the binary, before the orchestrator's own `-p` / `--dangerously-skip-permissions` / `--output-format` flags. The safety/output flags and the prompt stay where they are so operator args cannot silently displace them.
- **`CODEX_BIN` / `CLAUDE_BIN` interaction** — the first token is only a backend selector. It picks the codex vs. claude runner in `agents.py`; the actual executable launched is `CODEX_BIN` when the first token is `codex` and `CLAUDE_BIN` when it is `claude`. Set those to a full path when the CLI is not on `$PATH`. Writing a full path as the first token of `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` is rejected (it would not match `codex` / `claude`).

### Examples

Both backends accept model selection plus a reasoning-effort flag. Any of the lines below is a valid value for any of the three role env vars.

```dotenv
# bare backends (defaults)
DEV_AGENT=claude
REVIEW_AGENT=codex
DECOMPOSE_AGENT=claude

# claude with model selection
DEV_AGENT=claude --model claude-opus-4-7
REVIEW_AGENT=claude --model claude-sonnet-4-6

# claude with model + effort
DEV_AGENT=claude --model claude-opus-4-7 --effort high
DECOMPOSE_AGENT=claude --model claude-opus-4-7 --effort medium

# codex with model + reasoning effort
DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'
REVIEW_AGENT=codex -m gpt-5.5-codex -c 'model_reasoning_effort="high"'
```

## In-flight session lock — pinned full spec until the session ends

The parsed spec is persisted to pinned state as the **durable role identity** for an issue. The point of pinning the full spec (backend AND args, not just the backend) is that the orchestrator can resume mid-flight without losing the model / reasoning-effort the session was started with — a `DEV_AGENT` flip between ticks cannot silently retarget the next resume at a different backend, and it cannot silently drop the args either.

How it works per role:

- **Implementer (`DEV_AGENT`).** `_handle_implementing` writes the current spec verbatim to `dev_agent` (e.g. `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`) BEFORE invoking `run_agent`. The write happens unconditionally on every fresh spawn, so a backend hiccup that produces commits without surfacing a session id (empty codex `-o` file, unparseable claude JSONL line) still anchors the role for the next tick.

  On a resume, `_read_dev_session` re-parses `dev_agent` via `config._parse_agent_spec` to recover `(backend, extra_args)` and passes the args through to `run_agent`. `_handle_documenting`, `_handle_validating`, `_handle_fixing`, and `_handle_resolving_conflict` all resume the dev session via the same path, so the locked spec applies to every dev-side resume for the lifetime of the issue. `_handle_in_review` does not resume the dev itself — fresh PR feedback routes the issue to `fixing` instead.
- **Decomposer (`DECOMPOSE_AGENT`).** Same mechanic in `_handle_decomposing`: the spec is persisted to `decomposer_agent` before the spawn and re-parsed via `_read_decomposer_session` on every resume. The same backend (not the same session) also drives the question stage — `_handle_question` reads `DECOMPOSE_AGENT_SPEC` as the *fallback* on the first-ever question spawn, then pins what it ran under to `question_agent` (a separate key, parsed by `_read_question_session`).
- **Reviewer (`REVIEW_AGENT`).** Spawned **fresh every round** by `_handle_validating`, so changes to `REVIEW_AGENT` take effect on the next validating tick (no migration step needed). The current value is recorded in `review_agent` for traceability only; it is not used for resumes.

**Net effect:** flipping `DEV_AGENT` or `DECOMPOSE_AGENT` in env only affects fresh issues. Any issue with a live session keeps the original backend AND args until it reaches a terminal label (`done` / `rejected`); only then will a config change apply to a follow-up issue. Flipping `REVIEW_AGENT` takes effect on the next round of any issue in `validating`.

### Backward compatibility

- Legacy bare-backend values written before the spec rewrite (`"codex"` / `"claude"` in `dev_agent` / `decomposer_agent`) round-trip to `(backend, ())` — no args, matching what those deployments had at the time. Persisting them again is a no-op rewrite.
- The pre-spec key `codex_session_id` (written before `dev_agent` existed) is still honored on read and yields `spec="codex"`. A config flip to claude cannot strand that session — it stays on codex with no args.

## Quick reference

- The spec format is parsed once at import (`config._parse_agent_spec`) and again at resume time from pinned state, so the same validation rules apply to both paths.
- `CODEX_BIN` / `CLAUDE_BIN` are the only knobs for the executable path; the spec's first token is a backend selector, not a path.
- The reviewer is fresh per round; the implementer and decomposer are pinned for the life of the issue session.
- For per-stage handler internals (worktree management, prompt construction, post-spawn branching) see [`state-machine.md#stage-handlers`](state-machine.md#stage-handlers).
