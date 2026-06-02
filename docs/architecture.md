# Architecture of the Current Implementation

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating the actual coding work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees.

The dev/review/decompose roles are picked independently via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (default: claude decomposes, claude implements, codex reviews). Each value is a shell-like command spec: the first token must be `codex` or `claude` and selects the runner (which then launches `CODEX_BIN` or `CLAUDE_BIN`); any remaining tokens are forwarded verbatim as backend-CLI args (model, reasoning effort, etc.) on every spawn for that role. All three are parsed and validated at config load — see [Agent command specs](#agent-command-specs) below.

New unlabeled issues route through a `decomposing` stage that asks the decomposer agent for a structured manifest: `decision=single` flips the issue to `ready` and the implementer takes over; `decision=split` creates child issues, persists the dep graph, and parks the parent on `blocked` (or `umbrella` when the manifest's `umbrella` flag is true — a parent with no implementation of its own that `_handle_umbrella` closes to `done` once every child resolves) until the matching handler walks the children. Decomposition can be disabled with `DECOMPOSE=off`, which reverts to the legacy direct-to-`implementing` pickup.

Once the reviewer agent approves (squash, final-docs hop, hand-off to `in_review`) and the PR is mergeable, approved (real GitHub APPROVED review on the current head), and free of standing human `CHANGES_REQUESTED`, the orchestrator posts a one-shot HITL ping per head SHA on the issue thread so a human can click Merge — the orchestrator is permanently manual-merge-only and never calls `gh.merge_pr` from `in_review`. An unmergeable PR parks awaiting human attention; the `resolving_conflict` stage that rebases onto `origin/<base>` (capped by `MAX_CONFLICT_ROUNDS`) is reached via an operator relabel or the per-tick base-sync detour, not from `_handle_in_review`. Every `resolving_conflict` exit — pushed (clean rebase, recovered push, agent-resolved conflicts, human-reply resume, user-content drift pushed fixes) or the base-up-to-date no-op — hands straight back to `validating`; the single docs pass runs after the reviewer's final approval (via the `documenting` handoff in `_handle_validating`). An external human merge marks the issue `done`; a PR closed without merge lands on `rejected`.

## Design constraints

GitHub Issues are the orchestrator's task tracker and durable state surface. The process intentionally avoids an internal database: workflow labels expose the current stage, and the pinned JSON comment holds the per-issue state that the next tick needs. This keeps progress visible to humans on github.com and lets the process restart without reconstructing hidden local state.

The orchestrator is not meant to be fully autonomous. When a stage hits uncertainty, an unsafe repository state, a malformed agent response, or an exhausted retry cap, it parks with `awaiting_human` and mentions `HITL_HANDLE`; a later human issue comment is the resume signal for the parked agent session.

The workflow is deliberately fixed instead of planner-selected: decomposition, implementation, validation, and acceptance are mandatory phases. A future dynamic planner could select extra stages or skip some phases for trivial tasks, but the current implementation keeps routing explicit and label-driven.

Agents run on the host as CLI subprocesses with broad local permissions (`codex --dangerously-bypass-approvals-and-sandbox`, `claude --dangerously-skip-permissions`). The host, container, or VM around the orchestrator is therefore the real sandbox boundary; token handling and hardened git operations are designed around that assumption.

## Top-level layout

```
orchestrator/
  main.py               — entry point, polling loop, self-restart guard
  config.py             — env loading, secrets handling, backend validation
  github.py             — PyGithub wrapper, label bootstrap, pinned-state comment
  agents.py             — coding-agent subprocess runner (codex/claude dispatch)
  scheduler.py          — process-local `IssueScheduler`: cross-repo / per-repo
                           caps, duplicate-active-issue gate, family-aware
                           mutex, and the `ThreadPoolExecutor` that actually
                           runs per-issue handlers. `main` builds one at
                           startup and threads it through every
                           `workflow.tick(gh, spec, scheduler=...)` call;
                           shut down (`wait=True`) on process exit so
                           in-flight workers complete cleanly.
  workflow.py           — slim facade: per-repo tick loop, `_FAMILY_AWARE_LABELS`
                           partitioning, `_process_issue` label dispatcher,
                           `_handle_pickup`, `_park_awaiting_human`,
                           `_finalize_if_pr_merged` (cross-stage external-merge
                           short-circuit reused by the `implementing` /
                           `documenting` / `validating` entry checks and by
                           the `_handle_blocked` / `_handle_umbrella` child
                           recovery), `_finalize_if_issue_closed` (closed-but-
                           not-merged counterpart that flips the issue to
                           `rejected`, emits `pr_closed_without_merge` when
                           a closed PR is pinned, and runs
                           `_cleanup_terminal_branch` only for the closed-PR
                           case; called right after `_finalize_if_pr_merged`
                           in the three new sweep handlers. Defers without
                           mutating state when the pinned PR cannot be
                           fetched, or when the second fetch reveals the
                           PR IS actually merged -- in either case the
                           helper still returns True so the caller does
                           NOT continue dev / docs / reviewer work on a
                           closed issue, and the next tick re-attempts
                           `_finalize_if_pr_merged` against a fresh PR
                           state), `_drain_review_pr_terminals`
                           (the review-side counterpart used by
                           `_handle_in_review` / `_handle_fixing` /
                           `_handle_resolving_conflict` to drain the
                           shared PR/issue terminal arcs: merged PR ->
                           `done` with cleanup, closed PR -> `rejected`
                           with cleanup, open PR + manually closed
                           issue -> `rejected` WITHOUT cleanup so the
                           operator can salvage the still-open PR. The
                           caller is responsible for the PR fetch and
                           its own fetch-failure semantics; passing
                           `pr=None` is a no-op so fixing's catch-and-
                           defer pattern arrives unchanged),
                           `_run_agent_tracked`. Re-exports the
                           cross-module helpers and the stage entry handlers
                           from the modules below under their original names
                           so existing test patches
                           (`patch.object(workflow, "_foo", ...)`) keep
                           working. Stage-private helpers that no other
                           module needs (e.g. `_bump_in_review_watermarks`,
                           `_seed_legacy_in_review_watermarks`,
                           `_emit_conflict_round_incremented`) stay private to
                           their stage module and are NOT re-exported.
                           `_comment_created_at` IS re-exported because the
                           `fixing` handler reuses it for the quiet-window
                           debounce.
  workflow_drift.py     — user-content drift helpers:
                           `_compute_user_content_hash`,
                           `_detect_user_content_change`,
                           `_build_user_content_change_prompt` (the drift /
                           user-content-change dev-resume prompt builder),
                           `_mark_drift_comments_consumed`,
                           `_route_drift_to_decomposing`.
  workflow_messages.py  — shared text/parsing/comment helpers: orchestrator
                           comment markers and post helpers, stderr redaction
                           and diagnostics, the implementer / reviewer /
                           decomposer / conflict-resolution / PR-comment
                           followup prompt builders, and the manifest /
                           review-verdict / drift-ACK parsers. The drift /
                           user-content-change prompt builder lives in
                           `workflow_drift.py`, not here.
  git_plumbing.py       — hardened git subprocess layer: `_GIT_NO_PROMPT_ENV`,
                           per-target_root locks (`_TARGET_ROOT_LOCKS`,
                           `_TARGET_ROOT_LOCKS_LOCK`, `_target_root_lock`)
                           that serialize writes to the parent clone's
                           `.git/config` / `.git/refs` / `.git/packed-refs`,
                           the thin `_git` subprocess wrapper, the
                           agent-hostile-environment `_git_hardened` variant
                           (no hooks / no fsmonitor / no credential helper /
                           detached global+system config), and the
                           authenticated push/fetch helpers (`_push_branch`,
                           `_authed_fetch`, `_authed_target_fetch`) that
                           deliver the GitHub PAT via tempfile askpass so
                           the token never appears on argv. Every name
                           here is re-exported from `worktrees.py` so
                           existing imports and `patch.object(worktrees,
                           "_foo", ...)` test patches keep working.
  worktrees.py          — git, branch, and worktree plumbing: the
                           workflow-aware helpers
                           `_squash_and_force_push`,
                           `_refresh_base_and_worktrees`, and
                           `_sync_worktree_with_base`, the local-verify
                           runner (`_run_verify_commands` + `VerifyResult`)
                           used by `_handle_validating`'s pre-handoff
                           gate (before the final-docs flip to
                           `documenting` and the eventual `in_review`),
                           and the conventional-commit / branch-state
                           probes (`_first_commit_subject`,
                           `_is_conventional_subject`,
                           `_pr_title_from_commit_or_issue`, `_head_sha`,
                           `_worktree_dirty_files`, `_branch_ahead_behind`,
                           `_rebase_base_into_worktree`,
                           `_rebase_in_progress`). The worktree naming /
                           layout / creation / restoration / cleanup
                           helpers (`_branch_name`, `_sanitize_slug`,
                           `_repo_worktrees_root`, `_worktree_path`,
                           `_decompose_worktree_path`, `_ensure_worktree`,
                           `_ensure_pr_worktree`,
                           `_ensure_decompose_worktree`,
                           `_cleanup_decompose_worktree`,
                           `_branch_has_unpushed_commits`,
                           `_cleanup_question_worktree`,
                           `_cleanup_terminal_branch`,
                           `_has_new_commits`) live in
                           `worktree_lifecycle.py`. The hardened-git
                           subprocess layer (`_GIT_NO_PROMPT_ENV`,
                           `_target_root_lock`, `_git`, `_git_hardened`,
                           `_authed_fetch`, `_authed_target_fetch`,
                           `_push_branch`) lives in `git_plumbing.py`.
                           Both sets of names are re-exported here.
  worktree_lifecycle.py — worktree naming, layout, creation,
                           restoration, and cleanup helpers extracted
                           from `worktrees.py`: `_branch_name`,
                           `_sanitize_slug`, `_repo_worktrees_root`,
                           `_worktree_path`, `_decompose_worktree_path`,
                           `_ensure_worktree`, `_ensure_pr_worktree`,
                           `_ensure_decompose_worktree`,
                           `_cleanup_decompose_worktree`,
                           `_branch_has_unpushed_commits`,
                           `_cleanup_question_worktree`,
                           `_cleanup_terminal_branch`,
                           `_has_new_commits`. Imports the hardened git
                           plumbing from `git_plumbing.py`; every name
                           here is re-exported from `worktrees.py` so
                           existing imports and `patch.object(worktrees,
                           "_foo", ...)` test patches keep working.
  stages/
    __init__.py         — package marker; the dispatcher in `workflow.py`
                           still owns label→handler routing.
    decomposition.py    — `_handle_decomposing`, `_handle_ready`,
                           `_handle_blocked`, `_handle_umbrella`, and the
                           decomposer-session lookup / resume helpers.
    implementing.py     — `_handle_implementing` plus the developer-session
                           lifecycle: `_read_dev_session`,
                           `_resume_developer_on_human_reply`,
                           `_resume_dev_with_text` (with poisoned-session
                           recovery), the 24h retry budget, and the post-agent
                           disposition helpers (`_on_commits`, `_on_question`,
                           `_on_dirty_worktree`). `_on_commits` relabels
                           straight to `validating`; the docs pass only
                           runs as the final-docs handoff after the
                           reviewer approves.
    documenting.py      — `_handle_documenting`: the single docs pass that
                           resumes the dev session on the existing PR
                           worktree between reviewer approval and
                           `in_review`. Reached only via the **final-docs
                           handoff** (the `documenting` label set by
                           `_handle_validating`'s approval branch); every
                           pre-approval push (`implementing` PR open,
                           `validating` / `fixing` pushed fixes,
                           `in_review` drift pushes, `resolving_conflict`
                           pushed exits) hands straight back to `validating`
                           so docs runs exactly once per reviewer-approval
                           handoff (a PR that bounces back through `fixing`
                           and earns another approval gets another
                           final-docs pass before the next `in_review`
                           handoff). Successful exits always advance to
                           `in_review`. The final-docs exit ratchets
                           `pr_last_comment_id` via
                           `_ratchet_in_review_watermark_for_final_docs`
                           past any issue-thread reply the awaiting-human
                           resume consumed, so the next in_review tick
                           does not replay it as fresh PR feedback and
                           bounce to `fixing`. A user-content drift
                           mid-hop invalidates the prior approval: the
                           handler resets `review_round=0` and relabels
                           back to `validating` without spawning the docs
                           agent (the reviewer re-evaluates the updated
                           body on the next tick). Before the relabel it
                           also reconciles the PR worktree -- fetch
                           `<remote>/<branch>` and, when the worktree
                           is ahead of remote, `git reset --hard
                           <remote>/<branch>` to discard any unpushed
                           local docs commit authored against the OLD
                           body. This is what stops the
                           recovered-commit shortcut on a future
                           final-docs hop from silently pushing the
                           stale commit without spawning a fresh docs
                           agent against the new requirements. The
                           ahead/behind probe runs inline so a probe
                           failure is distinguishable from a real "in
                           sync" result (the shared
                           `_branch_ahead_behind` helper swallows git
                           errors as `(0, 0)`). The reconcile fires
                           when `ahead > 0` (stale local commits),
                           `behind > 0` (remote moved past local while
                           documenting was in flight -- the next
                           reviewer round must `git diff` against the
                           real PR head, not an un-fetched local
                           snapshot), or `_worktree_dirty_files`
                           reports any modified-tracked / untracked
                           path: the `reset --hard` moves HEAD to the
                           remote PR head and clears modified-tracked
                           files + local commits, then `git clean -fd`
                           removes the untracked files / directories
                           `reset --hard` leaves behind so a prior
                           dirty-park's docs edits cannot ride into
                           the next reviewer round. If the fetch fails the
                           handler parks with
                           `park_reason="fetch_failed"`; if the inline
                           probe, the `git reset --hard`, or the
                           `git clean -fd` fails it parks with
                           `park_reason="worktree_reset_failed"`.
                           `review_round` is cleared before any fallible
                           step so each park leaves no stale counter an
                           operator unpark could ride into a new
                           final-docs handoff. The drift block
                           also persists `docs_drift_unwind_pending=
                           True` while a cleanup is in progress and
                           clears it only on the success path that
                           relabels to `validating`; an operator
                           unpark or fresh human comment re-enters the
                           drift block on the next tick to retry the
                           cleanup, so an unpark cannot fall through
                           to a docs spawn or recovered-commit
                           shortcut. While parked with the sentinel
                           and no new human input, the handler returns
                           silently to avoid re-posting the park
                           comment every tick. Refuses to act on
                           a stale or
                           diverged PR branch (fetch + behind check)
                           and routes unrecognized outcomes through the
                           existing dirty / question / push park
                           helpers. Advances without pushing only on
                           an explicit `DOCS: NO_CHANGE` verdict
                           against a remote-clean branch.
    validating.py       — `_handle_validating` plus reviewer-session
                           lifecycle: `_handle_dev_fix_result`,
                           `_post_user_content_change_result`, validating-side
                           transient-park recovery (returns
                           `"stuck"`/`"cleared"`/`"pushed"` so the caller
                           can re-spawn the reviewer cleanly),
                           the local-verify gate park helper
                           (`_park_verify_failure`), and the watermark
                           seeding for the validating→`documenting` (final-
                           docs) →`in_review` handoff. On approval (verify +
                           squash succeeded) the handler relabels to
                           `documenting` (NOT directly to `in_review`).
                           `_handle_documenting`'s success exits advance to
                           `in_review` unconditionally. Pushed dev fixes
                           (CHANGES_REQUESTED, awaiting-human resume, drift
                           pushed, transient-park recovery push) stay on
                           `validating` (no relabel emitted) so the
                           reviewer re-evaluates on the next tick without
                           a pre-review docs hop.
    in_review.py        — `_handle_in_review` plus PR-side primitives:
                           legacy watermark migration and the
                           cross-namespace watermark ratchet
                           (`_bump_in_review_watermarks`). The handler is
                           permanently manual-merge-only: an approved
                           + mergeable PR (real GitHub APPROVED review
                           on the current head, no standing
                           CHANGES_REQUESTED) earns a one-shot HITL
                           ping per head SHA, an unmergeable PR parks
                           awaiting human attention,
                           external merges/closes terminate the issue. No
                           orchestrator-initiated `gh.merge_pr` call,
                           `merge_attempt` / `pr_merged` emission, or
                           `resolving_conflict` route from a mergeability
                           gate. User-content drift bounces DIRECTLY back
                           to `validating` on both the pushed-fix and
                           no-commit ACK outcomes so the reviewer
                           re-evaluates against the updated body. Docs do
                           not run on the drift exit -- the single docs
                           pass is deferred to the final-docs handoff
                           after reviewer approval. Both outcomes reset
                           `review_round`.
    fixing.py           — `_handle_fixing` owns the PR-feedback quiet
                           window and the dev-resume / push /
                           hand-back-to-`validating` cycle. Stage entered
                           when `_handle_in_review` detects fresh PR
                           feedback and routes the issue there instead of
                           spawning the dev itself; rescans unread feedback
                           from the in_review watermarks each tick,
                           debounces against the freshest comment
                           timestamp, and resumes via `_resume_dev_with_text`
                           with a `_build_pr_comment_followup` prompt over
                           all unread surfaces. Both the pushed-fix exit
                           and the no-new-feedback bounce flip DIRECTLY
                           back to `validating`. Docs do not run on the
                           pushed-fix exit -- the single docs pass is
                           deferred to the final-docs handoff after
                           reviewer approval.
    conflicts.py        — `_handle_resolving_conflict` plus
                           `_post_conflict_resolution_result` and the
                           `conflict_round` audit-event emitter.
```

Stage modules reach back into the facade via `from .. import workflow as _wf`
at call time so test patches against `workflow.<helper>` still intercept calls
made from inside a stage handler. Direct imports from `workflow_drift` /
`workflow_messages` / `worktrees` would bind stable references that the
`patch.object(workflow, ...)` pattern cannot override, so stage handlers
deliberately avoid them.

## Workflow labels

An issue should have at most one workflow label at a time. The set is `decomposing`, `ready`, `blocked`, `umbrella`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`, and the two terminals `done` / `rejected`. The orchestrator also creates two non-workflow control labels: `hold_base_sync` pauses per-tick base sync and rebases while present, and `backlog` makes per-tick handlers skip the issue entirely.

Label names are part of the public contract because live GitHub issues already carry them. For the meaning of each label, the control-label semantics, and the per-stage transitions they trigger, see [`state-machine.md#workflow-labels`](state-machine.md#workflow-labels).

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main._self_modifying_merge_happened`): each tick fetches `origin/<ORCHESTRATOR_BASE_BRANCH>` (default `main`); if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop exits 0 so the wrapper can re-exec the new code. The branch is decoupled from `BASE_BRANCH` so a target repo with a different default branch does not interfere with self-update detection.
- **Signals**: SIGINT/SIGTERM set a flag; the current tick finishes, then the loop exits.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans `workflow.tick(gh, spec, scheduler=...)` out across **every configured repo** via `main._run_tick`: single-repo deployments stay in-thread (legacy), multi-repo deployments use a `ThreadPoolExecutor` sized to the repo count. A single long-lived `IssueScheduler` (global cap `MAX_PARALLEL_ISSUES_GLOBAL`, per-repo cap `MAX_PARALLEL_ISSUES_PER_REPO`) is shared across all `tick` calls; the tick itself enumerates pollable issues and submits one callable per issue to the scheduler without waiting for handler completion. Each submit classifies the issue as family-aware (`decomposing` / `blocked` / `umbrella` / unlabeled — parent ↔ child writes) or fan-out (everything else); the scheduler enforces one family worker per repo and rejects duplicate active issues, global cap hits, and per-repo cap hits, leaving any rejected work for the next polling pass.

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`). The orchestrator process is stateless; the label and the pinned JSON are the entire dispatch input.

For the full per-tick sequence (eligible-issue enumeration, family vs. fan-out partitioning, the pre-PR rebase / PR-having `resolving_conflict` detour, the `hold_base_sync` / `question` skips, the per-tick external-merge sweeps, and the complete pinned-state JSON schema), see [`state-machine.md#per-tick-flow-workflowtick`](state-machine.md#per-tick-flow-workflowtick).

## Stage handlers

Each workflow label dispatches to a `_handle_<label>` function. The handlers live under `orchestrator/stages/` (see the module map in [Top-level layout](#top-level-layout)) and are re-exported from `workflow.py` so test patches against `workflow.<helper>` keep intercepting calls from inside a stage handler. Every per-tick handler runs the user-content drift hook (`_compute_user_content_hash` → `_detect_user_content_change`) before its own work so an out-of-band human edit re-routes the issue back to `decomposing` (when no dev session exists yet) or resumes the locked dev session with the updated body.

For the full per-stage internal flow — pickup / drift handling / decomposing / ready / blocked / umbrella / implementing / documenting / validating / in_review / fixing / resolving_conflict / question — see [`state-machine.md#stage-handlers`](state-machine.md#stage-handlers).

## Agent command specs

`DEV_AGENT`, `REVIEW_AGENT`, and `DECOMPOSE_AGENT` are shell-like command specs, not bare backend names. `config._parse_agent_spec` runs `shlex.split` over each value and yields `(backend, extra_args)`:

- **First token rule**: must match `codex` or `claude` case-insensitively (`tokens[0].lower()` is what `_parse_agent_spec` compares, so `CODEX`, `Claude`, and `codex` all parse to the same backend). The lowercased form is used only for dispatch — `agents.run_agent` keys off it to pick `_run_codex` vs. `_run_claude`.

  Pinned state (`dev_agent` / `review_agent` / `decomposer_agent`) stores the **raw spec string verbatim** (whatever the env had at first spawn, including the original casing — `DEV_AGENT=CODEX -m gpt-5.5` is persisted as the literal `"CODEX -m gpt-5.5"`); the re-lowercase happens again on every resume when `_parse_agent_spec` re-parses the stored string.

  Any other first token value (full path, alias, typo, empty string, unbalanced quotes) aborts at import with a SystemExit so a misconfiguration cannot silently fall back to a default backend on the next restart. `DECOMPOSE_AGENT` is parsed at import even when `DECOMPOSE=off`, so toggling the kill switch back on never surfaces a fresh "that env var was always invalid" failure.
- **Remaining tokens**: forwarded verbatim as backend-CLI args on every spawn for that role — typically model / reasoning-effort selection. Quoting follows shell rules, so values containing `=`, spaces, or nested quotes survive the round-trip (e.g. `codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`).

  For codex these are placed BEFORE the `exec` subcommand (they are codex global options); for claude they are placed right after the binary, before the orchestrator's own `-p` / `--dangerously-skip-permissions` / `--output-format` flags. The safety/output flags and the prompt stay where they are so operator-provided args cannot silently displace them.
- **`CODEX_BIN` / `CLAUDE_BIN` interaction**: the first token is only a backend selector — it picks `_run_codex` vs. `_run_claude` in `agents.py`. The actual executable launched is `config.CODEX_BIN` when the first token is `codex` and `config.CLAUDE_BIN` when it is `claude`, so override those when the CLI is not on `$PATH`. Writing the full path as the first token is rejected (it would not match `codex` / `claude`).

Examples (any of these is a valid value for any of the three role env vars):

```dotenv
DEV_AGENT=claude
DEV_AGENT=claude --model claude-opus-4-7
DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'
REVIEW_AGENT=codex -m gpt-5.5-codex
REVIEW_AGENT=claude --model claude-sonnet-4-6 --effort high
DECOMPOSE_AGENT=claude --model claude-opus-4-7
```

### In-flight session lock

The parsed spec is persisted to pinned state as the **durable role identity** for an issue, so a config flip mid-flight cannot retarget a live session:

- `_handle_implementing` writes the current spec to `dev_agent` (raw string, e.g. `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`) BEFORE invoking `run_agent`. A spawn that nevertheless commits without surfacing a session id (empty `-o` file, unparseable JSONL line) therefore still anchors the role.
- `_handle_decomposing` does the same for `decomposer_agent`.
- On every resume, `_read_dev_session` / `_read_decomposer_session` re-parse the stored string via the same `_parse_agent_spec` to recover `(backend, extra_args)`. This is what guarantees in-flight issues keep using the **pinned full spec until the session ends** — flipping `DEV_AGENT` / `DECOMPOSE_AGENT` in env only affects fresh issues, and only after the in-flight issue reaches a terminal label (`done` / `rejected`).
- Legacy bare-backend values (`"codex"` / `"claude"`) round-trip to `(backend, ())` — no args, matching what those deployments had at the time. The pre-spec `codex_session_id` key is also still honored on read and yields `spec="codex"`.
- The reviewer is spawned **fresh every round**, so `REVIEW_AGENT` changes take effect on the next validating tick. The current value is recorded in `review_agent` for traceability only.

## Agent subprocess (`agents.run_agent`)

`run_agent(backend, prompt, cwd, ...)` dispatches to the per-backend runner (`_run_codex` / `_run_claude`); `backend` is one of `"codex"` / `"claude"` and is re-validated at call time so a misuse fails loudly. Both runners return a unified `AgentResult(session_id, last_message, exit_code, timed_out, stdout, stderr)`. `CodexResult` is kept as a transitional alias for one release.

- **Trigger**: called by handlers with a backend name + prompt + worktree path.
- **Codex command**: `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o <tempfile> <prompt>`. The `-o` path is a per-spawn `tempfile.mkstemp` outside the worktree (so target repos without `.codex-*` in `.gitignore` don't see it as untracked); `last_message` is read from it and the tempfile is unlinked in a `finally` block.
- **Claude command**: `claude -p --dangerously-skip-permissions --output-format stream-json --include-partial-messages --verbose <prompt>` (with `--resume <sid>` when resuming). `last_message` is parsed from the stream-json: prefers the terminal `{"type":"result","result":...}` event, falls back to the last `assistant`/`message` text content for schema-drift forward-compat.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT`/`REVIEW_TIMEOUT`).
- **Environment** (`agents._filter_agent_env`, shared with the verify-command runner; verify passes `allow_provider_auth=False` so the agent's own provider keys are stripped too — see below):
  - GitHub-token-bearing env vars are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc. — the `_FORBIDDEN_AGENT_ENV` exact-match set) so a prompt-injected agent cannot push or call the GitHub API.
  - Production-secret-shaped env vars are stripped by name shape: anything matching `_AGENT_SECRET_SUFFIXES` (`_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, `_PAT`, `_CREDENTIAL`) or the bare-name set (`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `PAT`, `CREDENTIAL`). Without this a `STRIPE_API_KEY` / `DATABASE_PASSWORD` set for the host's other work would ride into a sandbox-bypassed agent or into operator-configured verify shell running against agent-produced code.
  - Credential-file LOCATORS are stripped too -- env vars whose value is a path to a file holding the secret (`*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`, `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` / `CREDENTIALS_FILE`). The agent runs as the same OS user, so leaving the pointer in env lets a hostile dependency simply `cat` the target -- the most important case is `ORCHESTRATOR_TOKEN_FILE`, the orchestrator's own write-credential locator that frequently points at a non-default path in multi-repo deployments. The strip does not protect against the agent guessing a well-known default (`~/.aws/credentials`, `~/.config/<repo>/token`), but it removes the trivial follow-the-env-var-pointer exfiltration path.
  - Write-credential locators (`_AGENT_WRITE_CREDENTIAL_LOCATORS`: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`, `GIT_SSH_COMMAND`) are stripped by exact name. These aren't secret-shaped, but they let an agent or verify subprocess use the operator's already-loaded ssh-agent / askpass binary / SSH wrapper to push or authenticate as them. The orchestrator's own push path (`worktrees._push_branch`) constructs its own `GIT_ASKPASS` tempfile in the env it hands to `subprocess.run`, so stripping the operator's copy here does not break it.
  - Provider auth required to reach the agent's own model is allowlisted by exact name in `_AGENT_PROVIDER_AUTH_ALLOWLIST` (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`) **for agent subprocesses only**, so the shape filter does not break codex/claude. The verify-command runner passes `allow_provider_auth=False` and strips those keys too: a verify shell executes untrusted agent-produced code, and a hostile dependency reading `$ANTHROPIC_API_KEY` would gain billable access to the operator's model account. An operator who needs to drive an agent from a verify command must load the key from disk inside a wrapper script — `VERIFY_COMMANDS=./scripts/run-verify.sh` — rather than embedding the literal value, because the verify failure park comment publishes the offending command verbatim. Advanced deployments (Bedrock, Vertex, custom proxies) need to extend the allowlist explicitly.
  - `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` are injected from `AGENT_GIT_NAME`/`AGENT_GIT_EMAIL` (default `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) so agent commits are stamped with the orchestrator's identity, regardless of the host's `~/.gitconfig`.
- **Output**: `AgentResult(...)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at `session_id`/`conversation_id`/etc. (shared between both backends).

## Push path (`workflow._push_branch`)

The orchestrator (not the agent) pushes. The push is hardened against the agent-controlled worktree:
- Token delivered via `GIT_ASKPASS` tempfile, never argv.
- Detaches from `~/.gitconfig` and `/etc/gitconfig` (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`).
- Disables `core.hooksPath`, `credential.helper`, `core.fsmonitor`.
- Refuses to push if the worktree's local config has any `url.*.insteadOf`/`pushInsteadOf` rewrite.
- Pushes via explicit refspec `HEAD:refs/heads/<branch>` (no upstream stored).

## Observability

Three independent observability surfaces — an opt-in audit event log, a project-local analytics JSONL sink, and an operator-deployed Postgres aggregation target (with a Streamlit dashboard and the `orchestrator/usage.py` parser that feeds it). None of them feed back into dispatch: workflow correctness keys off the pinned `<!--orchestrator-state ...-->` JSON comment on the issue (and the workflow label), so every surface is observation-only and safe to truncate, rotate, or delete.

For the per-sink schema, event-kind tables, append / retention / rotation semantics, the analytics-DB compose layout, the sync / read-model / dashboard wiring, and the usage parser's cost-precedence rules, see [`observability.md`](observability.md).

## Summary of "what runs when"

| Component | Type | Trigger | Cadence |
|---|---|---|---|
| `main` polling loop | long-lived Python process | manual start (or wrapper) | every `POLL_INTERVAL`s |
| `workflow.tick(gh, spec)` | function call | each loop iteration | once per tick **per configured `RepoSpec`**, fanned out across a `ThreadPoolExecutor` (one worker thread per repo) when N>1; single-repo legacy mode collapses to N=1 and stays in-thread |
| `_refresh_base_and_worktrees(gh, spec)` | function call | start of each `workflow.tick` | once per tick per repo: one `git fetch <spec.remote_name> <spec.base_branch>` (remote defaults to `origin`, overridable per `REPOS` entry), then per-worktree dispatch (pre-PR worktrees rebase directly; PR-having worktrees behind base detour to `resolving_conflict`). See [Per-tick flow](state-machine.md#per-tick-flow-workflowtick) for the full open-PR / `awaiting_human` / watermark / conflict / dirty-tree rules. |
| `_handle_*` per issue | function call | issue's workflow label | once per tick per open issue (within its repo's `tick`); concurrent up to `spec.parallel_limit` per repo and `MAX_PARALLEL_ISSUES_GLOBAL` across all repos (single shared `IssueScheduler`) |
| decomposer agent (`DECOMPOSE_AGENT`) | subprocess (fresh or resumed, locked spec (backend + args)) | `_handle_decomposing` (retry budget OK) or HITL resume | one shot per tick when needed |
| implementer agent (`DEV_AGENT`) | subprocess | `_handle_implementing` (no commits yet, retry budget OK) or HITL resume | one shot per tick when needed |
| reviewer agent (`REVIEW_AGENT`) | subprocess (fresh session) | `_handle_validating`, round < max | one shot per tick |
| dev-fix agent | subprocess (resumed dev session, locked spec (backend + args)) | reviewer says CHANGES_REQUESTED | one shot per tick |
| `_handle_resolving_conflict` | function call | issue label `resolving_conflict` (operator-applied or set elsewhere); also fires on closed-`resolving_conflict` issues from the polling sweep | once per tick per such issue (drives PR-state terminals → `done`/`rejected`, ahead-of-remote recovery push, `git rebase origin/<base>` then clean-rebase no-op flip / clean-rebase push / dev-conflict resume / cap-park, plus all park branches) |
| dev-conflict agent | subprocess (resumed dev session, locked spec (backend + args)) | `_handle_resolving_conflict` and `git rebase origin/<base>` left conflicts | one shot per tick |
| `_handle_question` | function call | issue label `question` (operator-applied) OR closed-`question` issue from the polling sweep | once per tick per such issue; closed terminal finalizes to `done` + tears down the worktree, open issue spawns the question agent (or resumes it on a new human comment) and parks awaiting human |
| question agent (`DECOMPOSE_AGENT` backend) | subprocess (read-only; fresh first spawn, locked spec on resume) | `_handle_question` (no prior session OR new human comment on a parked Q&A) | one shot per tick when needed |
| `git push` | subprocess | after dev produces clean commits | per fix |
| self-restart check | git fetch + diff | start of each tick | every tick |

## Architecture schema

```
                     ┌──────────────────────────────────────┐
                     │   GitHub repo(s) (REPO or REPOS)     │
                     │   ─ one orchestrator drives N repos  │
                     │   ─ issues (with workflow labels)    │
                     │   ─ pinned state comment per issue   │
                     │   ─ branches / PRs                   │
                     └──────────────┬───────────────────────┘
                                    │ PyGithub (one token per slug)
                                    │
   ┌────────────────────────────────┴─────────────────────────────────────┐
   │  orchestrator process  (python -m orchestrator.main)                 │
   │  ───────────────────────────────────────────────────                 │
   │   main.py                                                            │
   │     startup: build [(spec, GitHubClient(repo_spec=spec)), ...] from  │
   │              config.default_repo_specs() and ensure_workflow_labels  │
   │              once per spec; build one shared                         │
   │              scheduler = IssueScheduler(                             │
   │                  global_cap=MAX_PARALLEL_ISSUES_GLOBAL,              │
   │                  per_repo_cap=MAX_PARALLEL_ISSUES_PER_REPO)          │
   │     loop every POLL_INTERVAL s:                                      │
   │       1. self-restart check                                          │
   │          (origin/<ORCHESTRATOR_BASE_BRANCH> moved & touches orch/?)   │
   │       2. _run_tick(clients, scheduler):                              │
   │            len(clients) == 1 → in-thread workflow.tick(              │
   │                                  gh, spec, scheduler=scheduler)      │
   │            len(clients)  > 1 → ThreadPoolExecutor                    │
   │                                  (max_workers=len(clients)) fans     │
   │                                  workflow.tick(gh, spec,             │
   │                                  scheduler=scheduler)                │
   │                                  across one worker thread per repo   │
   │          (per-repo exception logged + skipped, never aborts the tick)│
   │     shutdown: scheduler.shutdown(wait=True) so in-flight workers     │
   │               complete cleanly on exit (signal / --once / restart)  │
   │                    │                                                 │
   │                    ▼                                                 │
   │   workflow.tick(gh, spec, scheduler=...) →                           │
   │     _refresh_base_and_worktrees(gh, spec, scheduler=...): skip       │
   │       worktrees whose handler is still in flight in scheduler        │
   │     classify each pollable issue by label and submit to scheduler:   │
   │       family-aware (decomposing/blocked/umbrella/unlabeled) →        │
   │         submit(..., family=True) — one family worker per repo at a   │
   │         time (parent↔child writes never overlap)                     │
   │       fan-out (ready/implementing/documenting/validating/in_review/  │
   │                fixing/resolving_conflict) → submit(..., family=False)│
   │         — concurrent up to per-repo and global caps                  │
   │     scheduler rejects duplicate active issue / cap hit / family slot │
   │       held → skipped this tick, next polling pass retries            │
   │     accepted workers each call gh._for_worker_thread() + refetch     │
   │       the Issue against that client, then run _process_issue         │
   │   → for each accepted submit → dispatch by label:                    │
   │     (per-label handler tree, dispositions, and parking flow live in │
   │      docs/state-machine.md#stage-handlers; the compact label-       │
   │      lifecycle reference is at                                       │
   │      docs/state-machine.md#state-transition-label-lifecycle)         │
   │                                                                      │
   └─────────┬───────────────────────────────────────┬────────────────────┘
             │ subprocess                            │ subprocess (hardened)
             ▼                                       ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │  coding-agent CLI           │         │  git push                   │
   │  (codex or claude,          │         │  ─ GIT_ASKPASS tempfile     │
   │   per-issue worktree)       │         │  ─ no global/system config  │
   │  ─ env: GH tokens stripped  │         │  ─ hooks/helper disabled    │
   │  ─ env: GIT_AUTHOR/COMMITTER│         │  ─ refuses url-rewrite      │
   │     stamped (orchestrator)  │         └──────────────┬──────────────┘
   │  ─ provider auth left alone │                        │
   │  ─ --bypass / --skip perms  │                        │
   │  ─ JSONL → session_id       │                        │
   │  ─ last_message: -o (codex) │                        │
   │     or stream-json (claude) │                        │
   └──────────────┬──────────────┘                        │
                  │ commits to                            │ pushes branch to
                  ▼                                       ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  git worktree:  <WORKTREES_DIR>/<owner>__<name>/issue-<n>           │
   │  branch:        orchestrator/issue-<n>                              │
   │  ─ slug subdir keeps two repos with the same issue # from colliding │
   │  ─ created from origin/<spec.base_branch> in spec.target_root       │
   │    (or reused if has unpushed commits)                              │
   └─────────────────────────────────────────────────────────────────────┘
```

### Roles in one line

| Component | Role |
|---|---|
| **main.py** | polling loop + signal handling + self-restart + per-tick `analytics.prune_with_retention_logging` retention pass |
| **workflow.py** | facade: per-repo tick loop, family-aware/fan-out partitioning, `_process_issue` dispatcher, `_handle_pickup`, `_park_awaiting_human`, `_run_agent_tracked`; re-exports the cross-module helpers and stage entry handlers (`_comment_created_at` is re-exported because the `fixing` handler reuses it; other stage-private helpers stay private to their module) |
| **workflow_drift.py** | user-content drift detection and re-route helpers |
| **workflow_messages.py** | prompt builders, parsers, comment posting + orchestrator-comment markers, stderr redaction |
| **git_plumbing.py** | hardened git subprocess layer: `_GIT_NO_PROMPT_ENV`, per-target_root locks, `_git` / `_git_hardened`, `_authed_fetch` / `_authed_target_fetch`, `_push_branch` (all re-exported from `worktrees.py`) |
| **worktree_lifecycle.py** | worktree naming, layout, creation, restoration, and cleanup helpers (`_branch_name`, `_sanitize_slug`, `_repo_worktrees_root`, `_worktree_path`, `_decompose_worktree_path`, `_ensure_worktree`, `_ensure_pr_worktree`, `_ensure_decompose_worktree`, `_cleanup_decompose_worktree`, `_branch_has_unpushed_commits`, `_cleanup_question_worktree`, `_cleanup_terminal_branch`, `_has_new_commits`); all re-exported from `worktrees.py` |
| **worktrees.py** | git/branch/worktree plumbing, squash-on-approval, per-tick base refresh, conventional-commit / branch-state probes; re-exports the `git_plumbing.py` and `worktree_lifecycle.py` helpers above |
| **stages/decomposition.py** | `_handle_decomposing` / `_handle_ready` / `_handle_blocked` / `_handle_umbrella` |
| **stages/implementing.py** | `_handle_implementing` + developer-session lifecycle (relabels straight to `validating` after PR opens — docs run once after reviewer approval, not here) |
| **stages/documenting.py** | `_handle_documenting` — the single docs pass on the existing PR worktree, run only as the **final-docs handoff** between reviewer approval and `in_review` (the `documenting` label is set by `_handle_validating`'s approval branch). Success exits always advance to `in_review` and ratchet `pr_last_comment_id` past any consumed awaiting-human reply. A user-content drift mid-hop relabels back to `validating` for re-review without spawning the docs agent and, before the relabel, fetches `<remote>/<branch>`, probes HEAD inline, and runs `git reset --hard` + `git clean -fd` when the local branch is ahead of remote, behind remote, OR has uncommitted/untracked edits -- so the next reviewer round runs against the actual remote PR head and no docs work authored against the old body survives; parks with `fetch_failed` on fetch failure and `worktree_reset_failed` on probe / reset / clean failure. |
| **stages/validating.py** | `_handle_validating` + reviewer-session lifecycle |
| **stages/in_review.py** | `_handle_in_review` + PR-watermark primitives; permanently manual-merge-only — routes fresh PR feedback to `fixing`, pings HITL once per head SHA when the PR is approved (real GitHub APPROVED review on the current head) and mergeable, parks unmergeable PRs for human attention |
| **stages/fixing.py** | `_handle_fixing` — PR-feedback quiet window, dev resume via `_resume_dev_with_text`, watermark advance, and a direct flip back to `validating` on both the pushed-fix and the no-new-feedback bounce exits (docs do not run here -- the single docs pass is deferred to the final-docs handoff after reviewer approval) |
| **stages/conflicts.py** | `_handle_resolving_conflict` + rebase-loop primitives |
| **stages/question.py** | `_handle_question` + question-session lifecycle (read-only Q&A on the `question` label, no PR) |
| **agents.py** | dispatch + spawn codex/claude subprocess, capture session id + last message |
| **scheduler.py** | process-local `IssueScheduler`: global / per-repo caps, duplicate-active-issue gate, family-aware mutex, the `ThreadPoolExecutor` that runs per-issue handlers; `main` constructs one at startup and threads it through every `workflow.tick(gh, spec, scheduler=...)` call, then shuts it down on exit |
| **github.py** | issues, comments, labels, pinned state, PR open/comment |
| **config.py** | env + token loading (token kept outside REPO_ROOT), backend validation |
| **codex / claude** | the only things that write code; run in isolated worktree |

### State transition (label lifecycle)

The compact label-lifecycle diagram for every forward, fix-loop, terminal, and HITL-park transition lives in [`state-machine.md#state-transition-label-lifecycle`](state-machine.md#state-transition-label-lifecycle).
