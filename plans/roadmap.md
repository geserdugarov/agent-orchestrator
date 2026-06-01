# Agent Orchestrator — Roadmap

## Status as of 2026-05-25

The full label lifecycle (no label → `decomposing` → `ready` / `blocked` /
`umbrella` → `implementing` → `documenting` → `validating` →
`in_review` → `fixing` (on fresh PR feedback) or `resolving_conflict`
(auto-merge detour) → `done` / `rejected`) is wired end-to-end.
Every code-changing branch update (initial implementation, any
`validating` pushed fix, any `fixing` PR-feedback push, any `in_review`
drift push, and any `resolving_conflict` push) routes through
`documenting` before the reviewer re-runs. `_handle_fixing` owns the
PR-feedback quiet window and the dev-resume / push / route-through-
`documenting` cycle, with watermark advancement on success and on
failure-park; the in_review route, the closed-issue sweep, the
PR-worktree refresh detour, and the PR-state terminal arcs are all in
place. A pushed fix flips to `documenting` so the docs pass runs
against the new head before the reviewer re-evaluates; the
no-new-feedback bounce still flips directly to `validating`. The operator-applied `question` label adds a read-only Q&A
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

See `docs/architecture.md` for the design and implementation
walk-through, and `docs/state-machine.md` for the label set, per-tick
flow, and stage-handler semantics. This file tracks what shipped and
what is still open.

## Shipped

**Bootstrap path.** Polling loop with `--once` and signal-clean shutdown,
ancestry-aware self-update detection, and a `run.sh` self-restart wrapper.
`GitHubClient` wraps PyGithub for issues, labels, pinned-state comments,
PRs, and workflow-label bootstrap.

**Agent invocation.** `agents.run_agent` dispatches to `_run_codex` /
`_run_claude` and returns a unified `AgentResult`; session ids are
harvested from JSONL for resumes. `DEV_AGENT` / `REVIEW_AGENT` /
`DECOMPOSE_AGENT` are independent shell-like command specs whose first
token names the backend and remaining tokens are forwarded as CLI args.
The full spec is pinned per issue and re-parsed on every resume, so
env-var flips cannot migrate live work. `AGENT_TIMEOUT` /
`REVIEW_TIMEOUT` cap wall-clock time; `MAX_RETRIES_PER_DAY` bounds
fresh spawns per issue.

**Security hardening.** Agent and verify-command env strip GitHub
tokens, production-secret-shaped vars (`*_TOKEN`/`*_KEY`/`*_SECRET`
/`*_PASSWORD`/`*_PAT`/`*_CREDENTIAL` and bare-name variants),
credential-file locators (`*_TOKEN_FILE`/`*_CREDENTIALS`/
`*_CREDENTIALS_FILE`, e.g. `ORCHESTRATOR_TOKEN_FILE`,
`GOOGLE_APPLICATION_CREDENTIALS`, `AWS_SHARED_CREDENTIALS_FILE`), and
write-credential locators (`SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`,
`GIT_SSH_COMMAND` — non-secret-shaped pointers to the operator's
loaded auth that would otherwise let a subprocess push or authenticate
as them). Provider auth (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) is
allowlisted by exact name for agent subprocesses only; verify commands
run with provider keys stripped too, because a hostile dependency in
agent-produced code would otherwise gain billable model access. The
orchestrator's own PAT must come from process env or a file outside
`REPO_ROOT`. `git push` is hardened via `GIT_ASKPASS`, a neutered
git-config envelope, explicit refspec, and a stamped commit identity
(`AGENT_GIT_NAME` / `AGENT_GIT_EMAIL`).

**Decomposing stage.** `_handle_decomposing` runs `DECOMPOSE_AGENT` and
parses a fenced `orchestrator-manifest` JSON block: `single` flips parent
to `ready`; `split` creates up to 10 children (shape / cycle validated)
and routes the parent to `blocked` or `umbrella`. `_handle_blocked`
walks deps each tick; umbrella parents close to `done` once children
resolve. `DECOMPOSE=off` skips straight to `implementing`;
`ALLOWED_ISSUE_AUTHORS` gates pickup.

**Implementing stage.** `_handle_implementing` ensures a per-issue
worktree from `origin/<base>`. New commits + clean tree → push, open
or reuse PR, flip to `documenting`; dirty or no commits → park.
Awaiting-human replies resume the locked dev session. PR titles and
commits follow Conventional Commits.

**Documenting stage.** `_handle_documenting` runs on the PR worktree
between `implementing` and `validating`, after every later
code-changing branch update, AND once more after reviewer approval as
the **final-docs hop** before `in_review`. A `docs:` commit lands → push
+ advance (to `validating` on pre-approval trips, to `in_review` on the
final-docs hop; on the final-docs hop the push also updates
`agent_approved_sha` to the new head so AUTO_MERGE survives, gated on
the companion sentinel `final_docs_approval_seeded` that validating
sets only when it actually persisted a non-empty `agent_approved_sha`
this round (both `gh.get_pr()` succeeded AND `_head_sha()` returned a
non-empty local SHA). When either fails the sentinel is absent and any
stale `agent_approved_sha` left over from a prior round stays untouched
so AUTO_MERGE remains gated). The final-docs exit additionally ratchets
`pr_last_comment_id` past any issue-thread reply consumed by the
awaiting-human resume, so the next in_review tick does not replay it
as fresh PR feedback and bounce to `fixing`. An explicit `DOCS:
NO_CHANGE` marker on a remote-clean branch advances without pushing.
The discriminator is the `docs_final_pending` marker set by
`_handle_validating`'s approval branch. Timeout / dirty / push-fail /
silent parks reuse the shared disposition tokens. Because every
code-changing update routes through this stage, split decompositions
no longer need a synthetic final docs child.

**Validating stage.** `_handle_validating` spawns a fresh reviewer on
`git diff origin/<base>...HEAD` and parses the last `VERDICT:` marker.
On `APPROVED` it runs `VERIFY_COMMANDS` (default empty), snapshots
`agent_approved_sha`, optionally squashes (`SQUASH_ON_APPROVAL`), sets
`docs_final_pending=True`, and flips to `documenting` — the final-docs
hop runs against the squashed head before `in_review` picks up. Verify
failures park with a typed `park_reason`. `CHANGES_REQUESTED` resumes
the dev; a clean pushed fix routes through `documenting` (without the
marker) and back to `validating` before the next review.
`MAX_REVIEW_ROUNDS` (default 3) caps iterations.

**In-review terminals and auto-merge.** `_handle_in_review` covers PR
merged → `done`; PR closed unmerged → `rejected`; fresh actionable PR
feedback → flip to `fixing` (the fixing stage owns the resume cycle);
user-content drift resumes the dev directly. `AUTO_MERGE=on` +
approval + no veto + mergeable + green CI → SHA-pinned merge → `done`.
Three independent watermarks separate IssueComment / PullRequestComment
/ PullRequestReview namespaces; the route to `fixing` deliberately
leaves them un-advanced so the fixing handler can read the triggering
comments. A cross-stage `_finalize_if_pr_merged` check at the entry of
`implementing`, `documenting`, and `validating` (and during the
umbrella / blocked manually-closed child recovery) catches an
externally-merged PR that landed before the issue reached `in_review`,
so the issue still flips to `done` and the umbrella aggregation does
not stall on a stale child label. A paired `_finalize_if_issue_closed`
runs right after at the same three entries: closed `implementing` /
`documenting` / `validating` issues yielded by the now-expanded
closed-issue sweep flip to `rejected` instead of spawning the dev /
docs / reviewer agent, with branch cleanup only when the linked PR
is also closed.

**Fixing stage.** `fixing` sits between `in_review` and `documenting`
in the PR-feedback fix loop. `_handle_fixing` rescans unread feedback
each tick (filtering orchestrator comments by id and hidden body
marker), debounces against the freshest comment timestamp
(`IN_REVIEW_DEBOUNCE_SECONDS`), then resumes the locked dev session
with a prompt built across all unread surfaces. A pushed fix clears
the bookmarks, resets `review_round`, drops `agent_approved_sha`, and
flips to `documenting`; a no-new-feedback bounce flips directly to
`validating`. Failed resumes park awaiting human. PR-state terminals
and the closed-issue sweep mirror `_handle_in_review`.

**Conflict resolution stage.** Under `AUTO_MERGE=on`, approved-but-
unmergeable PRs route to `resolving_conflict`.
`_handle_resolving_conflict` fetches base and runs `git rebase` under
the hardened envelope. Pushed resolutions flip to `documenting`; a
base-up-to-date no-op bounces straight back to `validating`. Real
conflicts resume the dev with up to 20 conflicted paths.
`MAX_CONFLICT_ROUNDS` (default 3) caps attempts; every pushed rebase
drops `agent_approved_sha`.

**Question stage.** The operator-applied `question` label runs
`_handle_question` as a read-only side-branch: no implementation, no
PR. The handler spawns `DECOMPOSE_AGENT` in the issue worktree, posts
the answer (or follow-up question) as a comment pinging
`HITL_MENTIONS`, and parks awaiting human. Subsequent comments resume
the locked session for multi-turn Q&A. Read-only violations are
typed; unsafe relabels to `implementing` refuse to publish
question-agent state. Closing the issue is the terminal signal.

**Multi-repo support.** `RepoSpec` is threaded through every handler.
`REPOS` env (`owner/name|target_root|base_branch[|remote_name[|parallel_limit]]`,
`;`- or newline-separated) drives fan-out; legacy single-repo mode
applies when unset. Per-tick repo calls fan out across a
`ThreadPoolExecutor` with per-repo exception isolation. Worktrees are
slug-namespaced; tokens, base branch, and target root are each
independently configurable.

**Parallel issue processing.** `MAX_PARALLEL_ISSUES_PER_REPO` (default
1, per-`REPOS`-override) and `MAX_PARALLEL_ISSUES_GLOBAL` (default 3)
bound concurrent handlers. Within a tick, family-aware stages
(`decomposing`, `blocked`, `umbrella`, unlabeled pickup) drain
sequentially to avoid parent/child races; the remaining stages fan
out across the bounded executor. Each worker thread mints a fresh
`GitHubClient` so PyGithub `Requester`s aren't shared.

**Workflow module split.** `workflow.py` is a slim facade owning the
tick loop, label dispatcher, unlabeled-pickup handler,
`_park_awaiting_human`, and `_run_agent_tracked`. Stage handler bodies
live under `orchestrator/stages/`; shared helpers live in
`workflow_drift.py`, `workflow_messages.py`, and `worktrees.py`. The
facade re-exports cross-module helpers under their original names, and
stage modules call back via `from .. import workflow as _wf` so
existing `patch.object(workflow, ...)` tests keep working.

**Tests.** Per-stage suites under `tests/test_workflow_*.py` cover
every handler; `tests/test_workflow.py` covers the facade. Shared
helpers live in `tests/workflow_helpers.py` and in-memory fakes in
`tests/fakes.py`. Coverage spans the manifest parser, watermarks,
debounce, auto-merge, squash, conflicts, umbrella, multi-repo
dispatch, and park-comment-replay prevention.

**Project CI.** GitHub Actions runs `ruff` and `pytest` on PRs under a
top-level `contents: read` token so the workflow is read-only and
non-publishing; the auto-merge gate consults `pr_combined_check_state`.
Dependabot opens weekly update PRs for the `github-actions` and `uv`
ecosystems with a 30-day `cooldown.default-days` window so freshly cut
upstream releases ripen before they land here, and a `dependency-review`
workflow blocks PRs that introduce vulnerable or non-compliant
dependencies.

**Audit event log.** Optional opt-in JSONL sink at `EVENT_LOG_PATH`.
`GitHubClient.emit_event` appends one record per workflow event
(`stage_enter`, `agent_spawn` / `agent_exit`, `review_verdict`,
`park_awaiting_human`, PR / merge / conflict events). Unset by default;
pinned state remains the authoritative dispatch source. Safe to
truncate or rotate.

**Analytics sink.** Separate project-local JSONL sink at
`ANALYTICS_LOG_PATH` (default `<LOG_DIR>/analytics.jsonl`).
`orchestrator/analytics/` exposes `build_record` / `append_record` /
`prune_old_records`; `ANALYTICS_RETENTION_DAYS` (default 90) bounds
retention and the polling loop prunes once per tick. Three event kinds
write today: `stage_enter`, `stage_evaluation` (timing per dispatch),
and `agent_exit` (token / cost). `ANALYTICS_LOG_PATH`,
`ANALYTICS_RETENTION_DAYS`, and `ANALYTICS_DB_URL` (below) are parsed
at import inside `orchestrator/analytics/__init__.py` rather than
`orchestrator/config.py`, so the package owns its own configuration
surface and consumers of `config.LOG_DIR` do not pull analytics
defaults in transitively. Orchestrator-side remains filesystem-only —
no DB driver or external services in-process.

**Analytics database.** Repo-local `analytics-db/` ships the Docker
Compose service (`postgres:16`, `127.0.0.1`-pinned, host-bind data
volume) and the `analytics_events` schema mirroring the JSONL record
shape (typed columns plus `extras` JSONB for forward-compat, plus
`source_path` / `source_line` for forensic context and a
`content_hash` plain unique index for dedup, kept non-partial so the
sync's `ON CONFLICT (content_hash)` arbiter resolves without
repeating the predicate). `ANALYTICS_DB_URL` is
a single libpq URL so swapping local for remote managed Postgres is a
one-line repoint. `orchestrator/analytics/sync.py` is the operator-
driven CLI (`python -m orchestrator.analytics.sync`) that replays
JSONL records into Postgres with `INSERT ... ON CONFLICT
(content_hash) DO NOTHING`, idempotent across repeated runs and
across `prune_old_records` rewrites. `orchestrator/analytics/read.py`
is the read-side counterpart: a thin data-access module exposing
plain-Python functions (`get_filter_options`, `get_summary`,
`get_time_series`, `get_stage_breakdown`, `get_event_breakdown`,
`get_recent_agent_exits`, `get_issues`, `get_issue_events`) that
`orchestrator/dashboard.py` calls into. `distinct_issues` in `get_summary`
counts `(repo, issue)` pairs so cross-repo windows do not collapse
same-numbered issues. Unset `ANALYTICS_DB_URL` short-circuits every read to an
empty / zero-valued result; connection / query failures wrap in a
single `AnalyticsReadError`. Streamlit-free in this layer so the
dashboard wiring can land independently. The driver is
`psycopg[binary]` (pinned in `pyproject.toml`) and lazy-imported in
both modules so the polling tick remains driver-free. Neither the
sync nor the read model is wired into the polling loop —
orchestrator correctness must not depend on database availability.

**Analytics dashboard.** `orchestrator/dashboard.py` is the
Streamlit app over the read model. Sidebar controls cover the date
window, repo selector, event / stage multi-selects, and a
`#123` / `123` issue-number input; the body renders summary
metrics (events / distinct issues / repos / cost / tokens), a daily
time-series bar chart, side-by-side stage / event breakdowns, the
recent `agent_exit` table with token / cost columns, the
date-bounded issues overview, and a per-issue event drill-down.
Every filter is threaded through the read model's SQL via
`_build_window_where`, so every widget narrows together rather
than diverging by surface. The event / stage selections
distinguish three cases: ``None`` (no filter on the column), a
non-empty sequence (parameterised ``IN (...)``), and an empty
sequence (a tautologically-false predicate so the cleared-
multiselect case shows nothing rather than reverting to "all").
The event multiselect maps straight to that contract because
`event` is `NOT NULL` in the schema; the stage multiselect routes
through `resolve_stage_filter` so the all-selected default (and
the no-stage-options case) collapses to ``None`` rather than
``IN (every-non-null-stage)`` -- ``options.stages`` lists only
non-null stages, so the latter would silently exclude legitimate
NULL-stage rows (`stage_evaluation` records on issues with no
workflow label).
The issue input acts as a SQL-level filter that narrows every
widget when a specific repo is selected and triggers the
drill-down section; without a repo it stays inert (GitHub issue
numbers are not unique across repos) and the drill-down renders
an instructive notice. `get_recent_agent_exits` accepts the same
window / event / stage / issue shape so the recent-runs table
moves with the date range; deselecting `agent_exit` from the
events multiselect short-circuits that widget to empty without
a DB round trip. Streamlit (and its transitive pandas) are
imported lazily inside `main()` so the polling tick stays free of
the dashboard's footprint, and the module loads cleanly even when
`streamlit` is not installed (a `tests/test_dashboard.py` guard
asserts the invariant). The dependency lives in a separate
`dashboard` group in `pyproject.toml` so `uv sync --locked` keeps
installing only the minimum runtime; `uv sync --group dashboard`
then `uv run streamlit run orchestrator/dashboard.py` is the
launch path. Missing-DB and `AnalyticsReadError` cases surface as
in-app `st.warning` / `st.error` banners that stop further
rendering rather than crashing the app.

**Agent usage / cost parser.** `orchestrator/usage.py` decodes the
JSONL stdout carried by `AgentResult` into a `UsageMetrics` dataclass:
backend, model(s), turn count, token totals, `cost_usd`, and a
`cost_source` tag. Parsers are pure Python and tolerate malformed
lines. A CLI-reported cost always wins; otherwise a baked-in price
table produces a best-effort estimate, and unknown SKUs yield
`unknown-price`. `_run_agent_tracked` appends one `agent_exit`
analytics record per invocation. Prompts, raw output, secrets, and
worktree contents are excluded; parser / sink IO failures are
swallowed.

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
- **Dynamic workflow.** Add a planner agent ahead of execution that picks
  the stages a given issue needs, such as extra architectural
  exploration or skipping acceptance for trivial fixes. Judged excessive
  for the original 2-week budget; revisit once the static flow is fully
  dogfooded.
- **Single-pass `documenting` after reviewer approval.** Today every
  code-changing branch update (initial implementation, validating fix,
  fixing-stage PR-feedback push, in_review drift push, every
  resolving_conflict push) routes through `documenting` before the
  reviewer re-runs. The proposed simplification routes those pushes to
  `validating` instead and runs a single docs pass after the reviewer
  emits `VERDICT: APPROVED`, before the `in_review` handoff. See
  [`plans/review-stages-lifecycle.md`](review-stages-lifecycle.md) for
  the full transition map (every `set_workflow_label` call site grouped
  by stage, every current entry into `documenting`, and the proposed
  target shape). **Issue #266 landed the final-docs handoff half**: on
  `VERDICT: APPROVED` `_handle_validating` now sets
  `docs_final_pending=True` and flips to `documenting`, and
  `_handle_documenting` advances to `in_review` on its success exits
  (updating `agent_approved_sha` when a docs commit lands AND the
  companion sentinel `final_docs_approval_seeded` confirms validating
  actually persisted a non-empty approval SHA this round — both
  `gh.get_pr()` succeeded AND `_head_sha()` returned a non-empty local
  SHA — so AUTO_MERGE survives; when either fails and the sentinel is
  absent, the docs push leaves any stale `agent_approved_sha` untouched
  so AUTO_MERGE stays gated).
  Collapsing the pre-approval `documenting` entries into direct
  `validating` routes is the remaining work under #262.
- **Symphony-inspired per-repo policy and hooks.** See
  [`plans/symphony-spec-review.md`](symphony-spec-review.md) for the full
  review. Two proposals survived the critical filter: a narrow
  `<target_root>/.agent-orchestrator/policy.toml` overrides file
  (verify commands, retry / review-round budgets, auto-merge) with
  hot-reload on file change, and three workspace lifecycle hooks
  (`after_create`, `before_run`, `after_run`) under
  `<target_root>/.agent-orchestrator/hooks/` so target repos can do
  bootstrap work without bloating agent prompts. Both stay opt-in; an
  absent file leaves behavior identical to today. The review also
  documents the rest of Symphony's surface (HTTP server, SSH worker
  pool, in-worker continuation loop, per-state caps, event-stream stall
  detection, `linear_graphql` tool, strict template engine, full
  `WORKFLOW.md` adoption) as deliberately not adopted.

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
