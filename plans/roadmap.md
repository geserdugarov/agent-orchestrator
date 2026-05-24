# Agent Orchestrator — Roadmap

## Status as of 2026-05-24

The full label lifecycle (no label → `decomposing` → `ready` / `blocked` /
`umbrella` → `implementing` → `validating` → `in_review` → `fixing`
(on fresh PR feedback) or `resolving_conflict` (auto-merge detour) →
`done` / `rejected`) is wired end-to-end. `_handle_fixing` owns the
PR-feedback quiet window and the dev-resume / push / bounce-back-to-
`validating` cycle, with watermark advancement on success and on
failure-park; the in_review route, the closed-issue sweep, the
PR-worktree refresh detour, and the PR-state terminal arcs are all in
place. The operator-applied `question` label adds a read-only Q&A
side-branch (`_handle_question`) that runs the decomposer backend in
the per-issue worktree to answer clarifying questions without opening
a PR; closing the issue is the terminal signal.

The orchestrator runs as a single long-lived Python process
(`python -m orchestrator.main`, wrapped by `run.sh` for self-restart), polls
one or more configured repos, and delegates the actual coding to `codex` /
`claude` CLI subprocesses running in per-issue git worktrees. Per-repo
ticks fan out concurrently and per-issue handlers within each repo can
run in parallel up to configurable caps. State lives in GitHub Issues
themselves (one workflow label plus one pinned JSON comment), so the
loop stays stateless and progress is observable on github.com.

See `docs/architecture.md` for the design, stage semantics, and
implementation walk-through. This file tracks what shipped and what is
still open.

## Shipped

**Bootstrap path.** Polling loop with `--once`, signal-clean shutdown, and
ancestry-aware self-update detection on `orchestrator/`. `run.sh`
self-restart wrapper. `GitHubClient` PyGithub wrapper handles issues,
labels, pinned-state JSON comments, PRs, and idempotent workflow-label
bootstrap.

**Agent invocation.** `agents.run_agent` dispatches to `_run_codex` /
`_run_claude` returning a unified `AgentResult`; session ids are harvested
from JSONL events for resumes.

`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` are independently
configurable shell-like command specs parsed by `config._parse_agent_spec`.

The first token names the backend (`codex` / `claude`, mapped to
`CODEX_BIN` / `CLAUDE_BIN`) and any remaining tokens are forwarded verbatim
as backend-CLI args on every spawn.

Roles stay declarative in env, e.g.:

- "implement with codex at xhigh reasoning":
  `DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`
- "review with claude opus at high effort":
  `REVIEW_AGENT=claude --model claude-opus-4-7 --effort high`
- "review with codex at high reasoning":
  `REVIEW_AGENT=codex -m gpt-5.5-codex -c 'model_reasoning_effort="high"'`

The full spec (backend + args) is persisted to pinned state and re-parsed
on every resume, so in-flight issues keep using the pinned spec until the
session ends and an env-var flip cannot migrate live work.
`AGENT_TIMEOUT` / `REVIEW_TIMEOUT` wall-clock caps with grandchild reaper;
`MAX_RETRIES_PER_DAY` per-issue fresh-spawn budget over 24h.

**Security hardening.** `agents._agent_env` strips all GitHub tokens from
the agent environment; PAT is rejected if found in `REPO_ROOT/.env` and
must come from the process env or a file outside `REPO_ROOT` (default
`~/.config/<owner>/<repo>/token`).

Hardened `git push` via `GIT_ASKPASS` tempfile, neutered git-config
envelope (hooks / credential / fsmonitor / global / system disabled),
refuses `insteadOf` rewrites, and pushes via explicit refspec. Agent
commit identity stamped via `AGENT_GIT_NAME` / `AGENT_GIT_EMAIL`.

**Decomposing stage.** `_handle_decomposing` runs `DECOMPOSE_AGENT` and
parses a fenced ` ```orchestrator-manifest ` JSON block: `decision=single`
flips parent to `ready`; `decision=split` creates up to 10 children with
shape / dependency / cycle validation, routing the parent to `blocked` or
`umbrella` depending on the flag.

`_handle_blocked` walks the dep graph each tick to unblock middle
children; umbrella parents close to `done` once all children resolve.
Children link via `Parent: #<n>` (never `Resolves`).

`DECOMPOSE=off` reverts to direct-to-`implementing`;
`ALLOWED_ISSUE_AUTHORS` gates pickup.

**Implementing stage.** `_handle_implementing` ensures a per-issue
worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` from
`origin/<base>`. New commits + clean tree → push, open / reuse PR, flip
to `validating`; dirty tree or no commits → park.

Awaiting-human replies resume the dev session on its locked spec
(backend + args, re-parsed from `dev_agent`). PR titles and commits
follow Conventional Commits, reusing the agent's first commit subject
when conformant.

**Validating stage.** `_handle_validating` spawns a fresh reviewer on
`git diff origin/<base>...HEAD` and parses the last `VERDICT:` marker.
On `APPROVED` the handler runs the configured `VERIFY_COMMANDS`
(default empty — legacy behavior preserved) in the per-issue worktree
before snapshotting `agent_approved_sha`, optionally squashing
(`SQUASH_ON_APPROVAL`, default on, `--force-with-lease`), and flipping
to `in_review`. A verify failure (non-zero exit, `VERIFY_TIMEOUT`,
dirty worktree, or HEAD moved by the command) parks on `validating`
with a typed `park_reason` (`verify_failed` / `verify_timeout` /
`verify_dirty` / `verify_head_changed`) and a redacted / truncated tail
of the command output; GitHub CI remains the later auto-merge gate
consulted by `_handle_in_review`.

`CHANGES_REQUESTED` posts feedback, resumes the dev, and increments
`review_round`; `MAX_REVIEW_ROUNDS` (default 3) caps iterations. Silent
reviewer crashes are tagged transient for retry.

**In-review terminals and auto-merge.** `_handle_in_review` covers:
PR merged → `done` + branch cleanup; PR closed unmerged → `rejected`;
fresh actionable PR feedback on any of the four comment surfaces →
record pending-fix metadata in pinned state and flip the label to
`fixing` immediately (no debounce wait, no dev spawn from this
handler — the `fixing` stage owns the resume + push + bounce-back-to-
`validating` cycle, with debouncing applied there);
`AUTO_MERGE=on` + agent-or-human approval + no veto + mergeable +
green CI → SHA-pinned `gh.merge_pr` → `done`.

Three independent watermarks separate IssueComment / PullRequestComment /
PullRequestReview namespaces; park comments bump watermarks past
themselves to avoid replay. The route to `fixing` deliberately does NOT
advance these watermarks so the fixing handler can read the triggering
comments to build its dev-resume prompt; the `pending_fix_*_max_id`
keys are bookmarks (a hint for the fixing handler / forensics), not
watermarks.

**Fixing stage.** `fixing` is the routable workflow label that sits
between `in_review` and `validating` in the PR-feedback fix loop.
The label means unread in-review feedback or a human CI-fix request is
queued during the quiet window or actively being addressed; a
successful fix returns to `validating` so the reviewer re-approves
the new head before auto-merge can proceed.
`_handle_fixing` rescans unread feedback from the three in_review
watermarks each tick (filtering orchestrator-authored comments by id
AND by the hidden `<!--orchestrator-comment-->` body marker), debounces
the resume against the freshest comment timestamp
(`IN_REVIEW_DEBOUNCE_SECONDS`) so newer comments arriving while
already labeled `fixing` naturally extend the wait, then builds a
`_build_pr_comment_followup` prompt across ALL unread surfaces and
resumes the locked dev session via `_resume_dev_with_text`. Regardless
of outcome the handler then advances the three in_review watermarks
ONLY to the max id actually fed to the dev per surface (deliberately
tighter than `_bump_in_review_watermarks` so a concurrent human
comment that landed mid-handler survives to the next tick on BOTH the
success and the failure path -- the orchestrator's own park comment is
filtered by id + body marker on the next tick's rescan, so the broad
bump is unnecessary). On a pushed fix the handler clears the
`pending_fix_*` bookmarks, resets `review_round`, drops the now-stale
`agent_approved_sha`, and flips the label to `validating`. On a failed
resume (timeout / dirty / push fail / no-commit) the validating-side
`_handle_dev_fix_result` parks awaiting human and the issue stays in
`fixing` until the human reply unsticks it. PR-state terminal arcs
(merged / closed / open-PR-closed-issue) mirror `_handle_in_review` so
external manual actions still finalize cleanly. Closed `fixing` issues
join the closed-issue sweep alongside `in_review`,
`resolving_conflict`, and `question` so an external manual merge with
`Resolves #N` finalizes to `done`, and the pre-tick base refresh
treats `fixing` as a PR-having stage eligible for the
`resolving_conflict` detour.

**Conflict resolution stage.** Under `AUTO_MERGE=on`, approved-but-
unmergeable PRs route to `resolving_conflict` instead of parking.
`_handle_resolving_conflict` fetches base via `_authed_fetch`, runs
`git merge --no-edit` under `_git_hardened`, and flips back to
`validating` on clean merge (or no-op already-up-to-date).

Real conflicts resume the dev session with a prompt naming up to 20
conflicted paths. `MAX_CONFLICT_ROUNDS` (default 3) caps attempts. Merge
over rebase preserves the stored `agent_approved_sha`.

**Question stage.** The operator-applied `question` workflow label
runs `_handle_question` (in `orchestrator/stages/question.py`) as a
read-only side-branch: no implementation, no PR, no push. The handler
spawns the configured `DECOMPOSE_AGENT` in the issue's `issue-N`
worktree, posts the agent's answer (or its clarifying follow-up
question) as an issue comment pinging `HITL_MENTIONS`, and parks
awaiting human. Subsequent human comments resume the locked session
(`question_agent` / `question_session_id` pinned per issue,
independent from any decomposing-session pins) for multi-turn Q&A.

Read-only violations are typed: `question_commits` /
`question_dirty` / `question_timeout` parks PRESERVE the worktree for
operator inspection and the per-tick base sync is skipped while the
label is `question`; the safe parks (`question_answer`,
`question_silent`) tear it down. Relabeling to `implementing` from a
`question` park clears the question flags only when the worktree and
local branch are both clean; otherwise the implementer parks with
`question_unsafe_relabel` and refuses to publish question-agent
state as a dev PR.

The closed-issue sweep (`list_pollable_issues`) surfaces closed-
`question` issues so `_handle_question` finalizes them to `done`,
stamps `question_closed_at`, and tears down the per-issue worktree
and local branch — closing the issue is the terminal signal.

**Multi-repo support.** `RepoSpec(slug, target_root, base_branch,
remote_name, parallel_limit)` is threaded through every handler. `REPOS`
env (`owner/name|target_root|base_branch[|remote_name[|parallel_limit]]`,
`;`- or newline-separated) drives fan-out; legacy single-repo mode
applies when `REPOS` is unset.

Validation at import aborts on malformed entries, bad slugs, duplicates,
empty `remote_name`, and non-integer / non-positive `parallel_limit`.
Worktrees namespaced by slug. With multiple `REPOS` entries each tick
fans the per-repo `workflow.tick(gh, spec)` calls out across a
`ThreadPoolExecutor` so a slow repo cannot delay the others; per-repo
exception isolation keeps a wedged repo from stopping the rest.

Per-slug token resolution; `ORCHESTRATOR_BASE_BRANCH` decoupled from
`BASE_BRANCH`; `TARGET_REPO_ROOT` decouples orchestrator checkout from
target clones.

**Parallel issue processing.** Two caps bound concurrent agent fan-out:
`MAX_PARALLEL_ISSUES_PER_REPO` (default 1, overridable per `REPOS`
entry via the optional fifth pipe-separated field) caps the number of
per-issue handlers from one repo that may be in flight at once on a
single tick (every pollable issue is still considered each tick — the
cap throttles concurrent execution, not the per-tick workload);
`MAX_PARALLEL_ISSUES_GLOBAL` (default 3) caps the total in-flight
per-issue handlers across all repos via a single
`threading.BoundedSemaphore` shared between every repo's tick.

Within a tick, the parallel path partitions pollable issues by label:
family-aware stages (`decomposing`, `blocked`, `umbrella`, unlabeled
pickup) that read/write across parent/child boundaries are drained
sequentially on one worker thread so parent and child handlers cannot
race on the same pinned-state comment; the remaining stages (`ready`,
`implementing`, `documenting`, `validating`, `in_review`, `fixing`,
`resolving_conflict`) fan out across the bounded executor because they
only touch per-issue state. Each worker thread mints a fresh `GitHubClient` via
`gh._for_worker_thread()` so concurrent HTTP traffic does not share a
PyGithub `Requester`.

**Workflow module split.** `workflow.py` is now a slim facade that owns
the per-repo `tick` loop, family-aware / fan-out label partitioning, the
`_process_issue` label dispatcher, the unlabeled-pickup handler
(`_handle_pickup`), `_park_awaiting_human`, and `_run_agent_tracked`.
Stage handler bodies live under `orchestrator/stages/` —
`decomposition.py` (decomposing / ready / blocked / umbrella),
`implementing.py` (developer-session lifecycle), `validating.py`
(reviewer-session lifecycle), `in_review.py` (PR watermarks and the
auto-merge gate), `conflicts.py` (`_handle_resolving_conflict`), and
`question.py` (`_handle_question` — read-only Q&A on the `question`
label, no PR).
Shared support helpers live in `workflow_drift.py` (user-content drift),
`workflow_messages.py` (prompts, parsers, comment posting), and
`worktrees.py` (git/branch/worktree plumbing, hardened fetch/push).
The facade re-exports the cross-module helpers and the stage entry
handlers under their original names, and stage modules call back through
`from .. import workflow as _wf` so existing
`patch.object(workflow, "_foo", ...)` tests keep working unchanged.
Stage-private helpers that no other module needs (such as
`_bump_in_review_watermarks`, `_auto_merge_gates_pass`,
`_seed_legacy_in_review_watermarks`, and `_emit_conflict_round_incremented`)
stay private to their stage module and are deliberately not re-exported.

**Tests.** Stage suites under `tests/test_workflow_*.py` cover every
stage handler — `test_workflow_decomposition.py`,
`test_workflow_implementing.py`, `test_workflow_validating.py`,
`test_workflow_in_review.py`, `test_workflow_fixing.py`, and
`test_workflow_conflicts.py` — plus `test_workflow.py` for
facade-level dispatcher / tick / pickup behavior.
Shared helpers live in `tests/workflow_helpers.py`. Coverage spans the
manifest parser, watermark / debounce logic, the auto-merge gate,
squash-on-approval, the resolving-conflict suite, the umbrella handler,
the multi-repo dispatcher, and park-comment-replay prevention.

`tests/fakes.py` exposes in-memory `FakeGitHubClient` / `FakePR` /
`FakePRRef` / `FakeIssue`. `tests/test_config.py`, `tests/test_agents.py`,
and `tests/test_main.py` cover their respective modules.

**Project CI.** GitHub Actions workflow runs `ruff` and `pytest` on PRs;
the auto-merge gate consults `pr_combined_check_state` so project-level
checks must pass before merge.

**Audit event log.** Optional opt-in JSONL sink at `EVENT_LOG_PATH`:
`GitHubClient.emit_event` appends one `{ts, repo, issue, event, stage, …}`
record per workflow event (`stage_enter`, `agent_spawn` / `agent_exit`,
`review_verdict`, `park_awaiting_human`, `pr_opened`, `pr_merged`,
`pr_closed_without_merge`, `merge_attempt`, `conflict_round`) via the
shared `_write_event_record` helper. Unset by default — observable
behavior matches a deployment without the sink.

Pinned state on the issue remains the authoritative source for every
dispatch decision; the log is append-only audit / observability and is
safe to truncate or rotate (no built-in rotation — pair with `logrotate`
for long-lived deployments).

## Future work

- **Spec-first split / separate test writer.** Add a `specifying` stage
  between `ready` and `implementing` so an independent spec agent writes
  failing tests before production work starts:
  `ready → specifying → implementing → validating → …`. The spec agent
  is allowed to edit only test paths, and the orchestrator must verify
  the new tests fail against `origin/<base>` before letting an
  implementer run.

  The implementer prompt should carry the generated test-file allowlist
  plus an explicit rule forbidding edits under `tests/**`; after the
  implementer exits, the orchestrator rejects and parks if
  `git diff --name-only HEAD origin/<base>` shows touched test files.
  Spec-agent inability to produce tests should park with a typed reason
  such as `ac-clarification`, `dep-missing`, or `design-question`, giving
  humans a clearer next action than a freeform park comment.

  Some issues cannot use this path, so extend the decomposer manifest
  with a backward-compatible `spec_skip: true` opt-out for docs,
  refactors, and other work that cannot be expressed as failing tests.
- **Repo memory carried across issues.** Add a small per-target-repo
  memory file at `<target_root>/.agent-orchestrator/repo-memory.json` so
  each issue does not start cold. Treat the file as orchestrator-owned
  context, not PR content; implementation should prevent it from leaking
  into agent commits or policy checks.

  Initial schema: `schema_version`, `verify_commands`,
  `touched_files_top`, and capped `common_failures` entries with
  summaries and timestamps. Update it from `_handle_in_review` merge
  terminals on a best-effort basis, never blocking a successful merge if
  the memory write fails.

  Read it into decomposer and implementer prompts with strict caps such
  as top 10 touched files and top 5 failures, so agents get useful
  repository context without turning the prompt into a stale knowledge
  base. Keep the first version fixed-schema and file-backed; richer
  search, exemplars, or lesson mining can wait until the simple signal
  proves useful.
- **Dockerfile / systemd / GitHub App migration.** The current deployment
  is a `run.sh` wrapper around `python -m orchestrator.main` on a single
  host. Container / VM isolation remains an open deployment question.
  Moving to a long-running VPS deployment also lets `systemd
  Restart=always` replace the `run.sh` self-restart wrapper, and the
  GitHub App migration lets the orchestrator drop the per-repo PAT in
  favor of an installation token.
- **Architectural review at `validating`.** Add an optional reviewer pass
  that flags structural issues such as oversized files that should be
  split. Not yet implemented.
- **Documentation stage.** Add an explicit stage that keeps README,
  `docs/`, and `plans/` in sync as code changes land. The decomposer
  prompt currently asks split issues to create a final docs child, but a
  stage would make that expectation visible and enforceable.
- **Dynamic workflow.** Add a planner agent ahead of execution that picks
  the stages a given issue needs, such as extra architectural
  exploration or skipping acceptance for trivial fixes. Judged excessive
  for the original 2-week budget; revisit once the static flow is fully
  dogfooded.

## Risks

- **R1 — Codex/Claude CLI output format drift.** Isolated in
  `agents.parse_session_id()` and the per-backend last-message capture;
  failure modes surface as `session_id=None` (logged, agent still runs)
  or empty `last_message` (the orchestrator parks with the agent's
  stderr quoted via `_format_stderr_diagnostics`).
- **R2 — Self-mutation while running.** Mitigated by per-issue worktrees
  + ancestry-aware self-update detection in
  `main._self_modifying_merge_happened` + the `run.sh` self-restart
  wrapper.
- **R3 — Runaway agent loops / token cost.** Wall-clock timeouts
  (`AGENT_TIMEOUT`, `REVIEW_TIMEOUT`), per-issue retry budget
  (`MAX_RETRIES_PER_DAY`), review/fix cap (`MAX_REVIEW_ROUNDS`), and
  conflict-resolution cap (`MAX_CONFLICT_ROUNDS`).
- **R4 — GitHub rate limits.** PyGithub handles backoff; 60s ticks are
  well under the 5000 req/hr limit.
- **R5 — Race between human comments and orchestrator action.**
  Re-fetch issue + pinned-state immediately before each transition; any
  comment newer than the recorded watermark is treated as a pause signal
  that drives the awaiting-human resume branch.
