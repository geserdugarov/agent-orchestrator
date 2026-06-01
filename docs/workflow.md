# Workflow — agent roles and command specs

This file documents the agent-role side of the workflow: which stage invokes which role, how the role command specs (`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`) are parsed, and how the spec used by an in-flight issue is pinned for the life of its session.

For the full stage-by-stage state machine (label semantics, per-stage handler internals, the per-tick flow), see [`state-machine.md`](state-machine.md). For the higher-level design (multi-repo dispatch, push hardening, agent subprocess shape), see [`architecture.md`](architecture.md). For the audit event log, analytics sink / database, and usage parser, see [`observability.md`](observability.md). For the env-var reference and run modes, see [`configuration.md`](configuration.md). For the user-facing summary, see [`../README.md`](../README.md).

## Roles and the workflow stages that invoke them

The workflow has three agent roles, each spawned by a different set of stage handlers. Roles are independent: each can use `codex` or `claude` and each carries its own optional CLI args.

| Role             | Env var            | Default  | Stage handler(s) that spawn it                                          | Session shape                                 |
| ---------------- | ------------------ | -------- | ----------------------------------------------------------------------- | --------------------------------------------- |
| Decomposer       | `DECOMPOSE_AGENT`  | `claude` | `_handle_decomposing` (and its `awaiting_human` resume); `_handle_question` (and its `awaiting_human` resume) reuses the same spec | Locked per issue after first spawn (decomposing → `decomposer_agent`; question → `question_agent`, a separate pin) |
| Implementer / dev| `DEV_AGENT`        | `claude` | `_handle_implementing`, `_handle_documenting` (docs pass + awaiting_human resume), `_handle_validating` (fix loop and awaiting_human resume), `_handle_fixing` (PR-comment quiet-window resume — `_handle_in_review` routes fresh PR feedback here instead of spawning the dev itself), `_handle_resolving_conflict` (conflict resume and awaiting_human resume) | Locked per issue after first spawn |
| Reviewer         | `REVIEW_AGENT`     | `codex`  | `_handle_validating` (fresh every round)                                | Fresh per round; current config always wins   |

All three first tokens need to be authenticated on the host before the orchestrator starts. The defaults (`claude` decomposes, `claude` implements, `codex` reviews) use both backends.

The stage handlers themselves live under `orchestrator/stages/` after the workflow split — `decomposition.py` owns the decomposing / ready / blocked / umbrella handlers, `implementing.py` owns the dev-session lifecycle (and now relabels to `documenting` after the PR opens), `documenting.py` owns the docs pass that runs before each reviewer round (between `implementing` and `validating`, after any pushed dev fix during `validating`, after any pushed PR-feedback fix from `fixing`, after any pushed in_review drift fix, and after any pushed `resolving_conflict` resolution), `validating.py` owns the reviewer-session lifecycle (any pushed dev fix from CHANGES_REQUESTED, awaiting-human resume, user-content drift, or transient-park recovery is relabeled to `documenting`, not back to `validating`, so the docs pass runs against the new head before the reviewer re-evaluates), `in_review.py` owns the PR-watermark / auto-merge gate (and routes fresh PR feedback to `fixing`; its own user-content drift pushed outcome routes through `documenting` so the docs pass runs against the updated body before the reviewer re-evaluates, while the no-commit ACK outcome bounces directly back to `validating` because no commit landed for the docs pass to react to), `fixing.py` owns the PR-feedback quiet-window + dev-resume + push + route-through-`documenting` cycle (the no-new-feedback bounce still flips directly to `validating` because there is no fix work for the docs pass to react to), `conflicts.py` owns `_handle_resolving_conflict` (every pushed resolution — clean rebase, recovered push, agent-resolved conflicts, awaiting-human resume push, and drift-pushed fix — routes through `documenting`; only a base-up-to-date no-op skips the docs hop), and `question.py` owns `_handle_question`. The `_handle_pickup` entry handler (no label → decomposing / implementing) and the `_process_issue` label dispatcher still live in the facade module `orchestrator/workflow.py`, which re-exports every handler under its original `_handle_*` name; tests and intra-handler calls keep using the `workflow._handle_*` surface unchanged. `_comment_created_at` is also re-exported by the facade so the fixing handler can reach it through `workflow.<name>` for the quiet-window debounce (and test patches against `workflow.<name>` continue to intercept calls from inside the stage module). See [`architecture.md`](architecture.md#top-level-layout) for the full module map.

### Question stage — read-only Q&A on the `question` label

The `question` workflow label is operator-applied: there are no automatic transitions in or out. `_handle_question` runs the configured `DECOMPOSE_AGENT` in the issue's `issue-N` worktree (the same worktree the implementing stage uses, recreated from `origin/<base>` each spawn) with a read-only prompt that forbids modifying, committing, or pushing files. The agent's answer (or its own clarifying follow-up question) is posted as a comment on the issue thread pinging `HITL_MENTIONS`; no PR is opened. Subsequent human replies resume the locked session via `_build_question_followup_prompt`, so a multi-turn Q&A keeps the same backend + args without the orchestrator ever switching to a different CLI.

The handler funnels every park through `_park_question` and stamps `park_reason` with one of:

- `question_answer` — happy path; the agent produced a final answer or a follow-up question and was parked awaiting the human's next reply. The worktree is torn down.
- `question_silent` — the agent produced no `last_message` (usually a poisoned session-resume). The worktree was verified clean and is torn down.
- `question_commits` / `question_dirty` / `question_timeout` — read-only violations or timeouts. The worktree is **preserved** so the operator can inspect what the agent did before resetting; the per-tick base sync is also skipped while the label is `question` so `origin/<base>` is not merged over that inspection state. A no-reply tick on one of these parks keeps the worktree on disk until the operator either replies (resume produces a clean answer → worktree torn down) or closes the issue.
- `question_unsafe_relabel` — set by `_handle_implementing` (not this stage) when an operator relabels a `question`-parked issue to `implementing` while the worktree carries dirty edits OR the local `orchestrator/issue-N` branch carries commits beyond `origin/<base>`. The dev agent refuses to publish that work as a dev implementation; the park comment names the reset to perform before retrying.

Closing the issue is the terminal signal: `list_pollable_issues` sweeps closed-`question` issues into the next tick, and `_handle_question` then stamps `question_closed_at`, flips the label to `done`, and runs `_cleanup_question_worktree` to remove the per-issue worktree and local branch.

### Local verify gate (not an agent)

After the reviewer emits `VERDICT: APPROVED`, `_handle_validating` runs the configured `VERIFY_COMMANDS` directly in the per-issue worktree — these are plain shell commands, not an agent role, so no `*_AGENT` env var applies. The gate runs before the approval comment, the squash, the watermark seeding, and the `in_review` label flip. A clean run advances the issue; any failure parks on `validating` with a typed `park_reason` (`verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`) so the operator can fix the breakage. GitHub CI remains the later auto-merge gate consulted by `_handle_in_review`. See [`configuration.md#local-verification-gate`](configuration.md#local-verification-gate) for the env-var reference.

## Spec format

`config._parse_agent_spec` runs `shlex.split` over each role's env value and yields `(backend, extra_args)`:

- **First token rule** — must match `codex` or `claude` case-insensitively (`_parse_agent_spec` compares `tokens[0].lower()`, so `CODEX`, `Claude`, and `codex` all parse to the same backend). The lowercased form is used only for dispatch (`agents.run_agent` keys off it).

  Pinned state stores the **raw spec string verbatim** with its original casing — `DEV_AGENT=CODEX -m gpt-5.5` is persisted as the literal `"CODEX -m gpt-5.5"`, and the re-lowercase happens again on every resume when `_parse_agent_spec` re-parses the stored value.

  Anything else (full path, alias, typo, empty string, unbalanced quotes) aborts at import with a `SystemExit` so a misconfiguration cannot silently fall back to a default backend on the next restart. `DECOMPOSE_AGENT` is parsed at import even when `DECOMPOSE=off`, so toggling the kill switch back on never surfaces a fresh "that env var was always invalid" failure.
- **Remaining tokens** — forwarded verbatim as backend-CLI args on every spawn for that role. Quoting follows shell rules, so values containing `=`, spaces, or nested quotes survive (e.g. `codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`).

  For codex the args are placed before the `exec` subcommand (they are codex global options); for claude they are placed right after the binary, before the orchestrator's own `-p` / `--dangerously-skip-permissions` / `--output-format` flags. The safety/output flags and the prompt stay where they are so operator args cannot silently displace them.
- **`CODEX_BIN` / `CLAUDE_BIN` interaction** — the first token is only a backend selector. It picks the codex vs. claude runner in `agents.py`; the actual executable launched is `CODEX_BIN` when the first token is `codex` and `CLAUDE_BIN` when it is `claude`. Set those to a full path when the CLI is not on `$PATH`. Writing a full path as the first token of `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` is rejected (it would not match `codex` / `claude`).

### Examples

Both backends accept model selection plus a reasoning-effort flag. Either is a valid value for any of the three role env vars.

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

The parsed spec is persisted to pinned state as the **durable role identity** for an issue.

The point of pinning the full spec (backend AND args, not just the backend) is that the orchestrator can resume mid-flight without losing the model / reasoning-effort the session was started with — a `DEV_AGENT` flip between ticks cannot silently retarget the next resume at a different backend, and it cannot silently drop the args either.

How it works per role:

- **Implementer (`DEV_AGENT`).** `_handle_implementing` writes the current spec verbatim to `dev_agent` in pinned state (e.g. `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`) BEFORE invoking `run_agent`. The write happens unconditionally on every fresh spawn, so a backend hiccup that produces commits without surfacing a session id (empty codex `-o` file, unparseable claude JSONL line) still anchors the role for the next tick.

  On a resume, `_read_dev_session` re-parses `dev_agent` via `config._parse_agent_spec` to recover `(backend, extra_args)` and passes the args through to `run_agent`. `_handle_validating`, `_handle_fixing`, and `_handle_resolving_conflict` all resume the dev session via the same path, so the locked spec applies to every dev-side resume for the lifetime of the issue. `_handle_in_review` no longer resumes the dev itself — fresh PR feedback routes the issue to `fixing` instead.
- **Decomposer (`DECOMPOSE_AGENT`).** Same mechanic in `_handle_decomposing`: the spec is persisted to `decomposer_agent` before the spawn and re-parsed via `_read_decomposer_session` on every resume. The same backend (not the same session) also drives the question stage — `_handle_question` reads `DECOMPOSE_AGENT_SPEC` as the *fallback* on the first-ever question spawn for an issue, then pins the spec it ran under to `question_agent` (a separate key, parsed by `_read_question_session`) so a multi-turn Q&A keeps its own lock independent of any decomposing session that ran on the same issue.
- **Reviewer (`REVIEW_AGENT`).** Spawned **fresh every round** by `_handle_validating`, so changes to `REVIEW_AGENT` take effect on the next validating tick (no migration step needed). The current value is recorded in `review_agent` for traceability only; it is not used for resumes.

**Net effect:** flipping `DEV_AGENT` or `DECOMPOSE_AGENT` in env only affects fresh issues. Any issue with a live session keeps the original backend AND args until it reaches a terminal label (`done` / `rejected`); only then will a config change apply to a follow-up issue. Flipping `REVIEW_AGENT` takes effect on the next round of any issue in `validating`.

### Backward compatibility

- Legacy bare-backend values written before the spec rewrite (`"codex"` / `"claude"` in `dev_agent` / `decomposer_agent`) round-trip to `(backend, ())` — no args, matching what those deployments had at the time. Persisting them again is a no-op rewrite.
- The pre-spec key `codex_session_id` (written before `dev_agent` existed) is still honored on read and yields `spec="codex"`. A config flip to claude cannot strand that session — it stays on codex with no args.

## Quick reference

- The spec format is parsed once at import (`config._parse_agent_spec`) and again at resume time from pinned state, so the same validation rules apply to both paths.
- `CODEX_BIN` / `CLAUDE_BIN` are the only knobs for the executable path; the spec's first token is a backend selector, not a path.
- The reviewer is fresh per round; the implementer and decomposer are pinned for the life of the issue session.
- For the per-stage handler internals (worktree management, prompt construction, post-spawn branching) see [`state-machine.md`](state-machine.md#stage-handlers) and the [`Agent command specs`](architecture.md#agent-command-specs) section in `architecture.md`.
