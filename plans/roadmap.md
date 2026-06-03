# Agent Orchestrator — Roadmap

## Status as of 2026-06-01

The full label lifecycle (no label → `decomposing` → `ready` / `blocked` /
`umbrella` → `implementing` → `validating` → `documenting` (final-docs
hop) → `in_review` → `fixing` (on fresh PR feedback) or
`resolving_conflict` (operator relabel or per-tick base-sync detour)
→ `done` / `rejected`) is wired end-to-end. The single docs pass runs
after the reviewer's final approval via the `documenting` handoff, so
docs land against the approved/squashed head without spending a no-op
pass on each code-changing push. The orchestrator is permanently
manual-merge-only — `_handle_in_review` pings HITL once per head SHA
for a mergeable + approved PR, parks awaiting human attention for an
unmergeable PR, and never calls `gh.merge_pr`. Every pre-approval
code-changing route lands directly back on `validating`: the initial
`implementing` PR open, every `validating` pushed fix
(CHANGES_REQUESTED, awaiting-human resume, user-content drift,
transient-park recovery), the PR-feedback `fixing` pushed-fix exit,
the `in_review` user-content drift, and every `resolving_conflict`
pushed exit (clean rebase, recovered push, agent-resolved,
awaiting-human resume, drift-pushed fix) all stay on (or return to)
`validating` with `review_round` reset so the next reviewer round
re-evaluates the freshly-pushed head. `_handle_documenting`'s own success
exits always advance to `in_review`; its drift block relabels
directly to `validating` without spawning the docs agent when a
body edit invalidates the prior approval mid-hop. `_handle_fixing` owns the PR-feedback quiet window
and the dev-resume / push / hand-back-to-`validating` cycle, with
watermark advancement on success and on failure-park; the in_review
route, the closed-issue sweep, the PR-worktree refresh detour, and
the PR-state terminal arcs are all in place. Both the pushed-fix
exit and the no-new-feedback bounce flip directly to `validating`.
The operator-applied `question` label adds a read-only Q&A
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
or reuse PR, flip straight to `validating` (no pre-review docs hop);
dirty or no commits → park. Awaiting-human replies resume the locked
dev session. PR titles and commits follow Conventional Commits.

**Documenting stage.** `_handle_documenting` runs on the PR worktree
ONLY after reviewer approval, as the **final-docs hop** before
`in_review`. There is no pre-approval entry: every `implementing` PR
open, every pushed fix in `validating` / `fixing`, every `in_review`
drift push, and every `resolving_conflict` pushed exit hands straight
back to `validating`. The handler's own success exits always advance
to `in_review` — `_advance_after_docs_push` / `_advance_after_docs_no_change`
both target `in_review` unconditionally on every success exit.
A `docs:` commit lands → push + advance to
`in_review`. The final-docs exit additionally ratchets
`pr_last_comment_id` past any issue-thread reply consumed by the
awaiting-human resume, so the next in_review tick does not replay it
as fresh PR feedback and bounce to `fixing`. An explicit `DOCS:
NO_CHANGE` marker on a remote-clean branch advances without pushing.
The `documenting` label set by `_handle_validating`'s approval
branch is the only entry point. A user-content drift during the
final-docs hop invalidates the prior approval: the handler resets
`review_round=0` and relabels back to `validating` without
spawning the docs agent so the reviewer re-evaluates the updated
body. Before the relabel the handler fetches `<remote>/<branch>`, probes
HEAD inline (so a probe failure is distinguishable from a real "in
sync" result), and -- when the local branch is ahead of remote,
behind remote (the remote PR head moved past local while
documenting was in flight), OR the worktree is dirty (any
modified-tracked or untracked path) -- runs
`git reset --hard <remote>/<branch>` + `git clean -fd` on the PR
worktree, so the next reviewer round runs against the actual remote
PR head and no unpushed local docs commit / uncommitted / untracked
docs edits authored against the OLD body survive. Otherwise the recovered-commit shortcut on a future
final-docs hop could silently push a stale commit (especially under
`SQUASH_ON_APPROVAL=off`) and a prior dirty-park's edits could ride
into the next reviewer round. `review_round` is cleared before any
fallible step, so each park (`fetch_failed` on fetch failure,
`worktree_reset_failed` on probe / reset / clean failure) leaves no
stale counter an operator unpark could ride into a fresh final-docs
handoff. The drift block also persists
`docs_drift_unwind_pending=True` while a cleanup is in progress and
clears it only on the success path that relabels to `validating`;
an operator unpark or fresh human comment re-enters the drift block
on the next tick to retry the cleanup, so an unpark cannot fall
through to a docs spawn or recovered-commit shortcut. Timeout /
dirty / push-fail / silent parks reuse the shared disposition
tokens.
Because the final-docs hop covers every code-changing update by
definition, split decompositions no longer need a synthetic final
docs child.

**Validating stage.** `_handle_validating` spawns a fresh reviewer on
`git diff origin/<base>...HEAD` and parses the last `VERDICT:` marker.
On `APPROVED` it runs `VERIFY_COMMANDS` (default empty), optionally
squashes (`SQUASH_ON_APPROVAL`), seeds the in_review PR watermarks,
and flips to `documenting` — the final-docs hop runs against the
squashed head before `in_review` picks up. Verify failures park with
a typed `park_reason`. `CHANGES_REQUESTED` resumes the dev; a clean
pushed fix stays on `validating` (no docs hop) so the next reviewer
round re-evaluates the freshly-pushed head. `MAX_REVIEW_ROUNDS`
(default 3) caps iterations.

**In-review terminals and HITL ping.** `_handle_in_review` covers PR
merged → `done`; PR closed unmerged → `rejected`; fresh actionable PR
feedback → flip to `fixing` (the fixing stage owns the resume cycle);
user-content drift resumes the dev directly. The orchestrator is
permanently manual-merge-only: an approved + mergeable PR earns a
one-shot HITL ping per head SHA so the human knows the PR is ready,
and an unmergeable PR parks awaiting human attention. Approval here
means a real GitHub APPROVED review on the current head (the reviewer
agent posts an issue comment, not a PR review, so the ping waits until
a human or bot formally approves the PR). No `gh.merge_pr` call,
no `merge_attempt` / orchestrator-initiated `pr_merged` emission, and
no `resolving_conflict` route from this stage.
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
is also closed. The three review-side stages (`_handle_in_review`,
`_handle_fixing`, `_handle_resolving_conflict`) share a single
finalize path via `_drain_review_pr_terminals` in `workflow.py` for
their merged / closed-unmerged / open-PR-with-closed-issue arcs --
counterpart to `_finalize_if_pr_merged` on the in-flight side -- so
the cleanup behavior, event shape, and open-PR salvage semantics
cannot drift between the three handlers.

**Fixing stage.** `fixing` sits between `in_review` and `validating`
in the PR-feedback fix loop. `_handle_fixing` rescans unread feedback
each tick (filtering orchestrator comments by id and hidden body
marker), debounces against the freshest comment timestamp
(`IN_REVIEW_DEBOUNCE_SECONDS`), then resumes the locked dev session
with a prompt built across all unread surfaces. A pushed fix clears
the bookmarks, resets `review_round`, and flips DIRECTLY back to
`validating`; the no-new-feedback bounce also
flips to `validating`. Docs do not run on the pushed-fix exit --
the single docs pass is deferred to the final-docs handoff after
reviewer approval. Failed resumes park awaiting human.
PR-state terminals and the closed-issue sweep mirror
`_handle_in_review`.

**Conflict resolution stage.** `_handle_resolving_conflict` is reached
via an operator relabel or the per-tick base-sync detour (a PR-having
worktree behind `<remote>/<base>`). `_handle_resolving_conflict`
fetches base and runs `git rebase` under the hardened envelope.
Every exit — pushed resolution or base-up-to-date no-op — hands
straight back to `validating`; the single docs pass runs after the
reviewer's final approval via the `documenting` handoff. Real
conflicts resume the dev with up to 20 conflicted paths.
`MAX_CONFLICT_ROUNDS` (default 3) caps attempts; every pushed rebase
resets `review_round`.

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
bound concurrent cap-counted handlers. A single long-lived
`IssueScheduler` (`orchestrator/scheduler.py`) is built at startup
with those caps and threaded through every
`workflow.tick(gh, spec, scheduler=...)` call: the tick enumerates
pollable issues, classifies them as family-aware or fan-out, and
hands work to the scheduler. Fan-out issues are submitted as one
nonblocking callable per issue; family-aware issues (`decomposing`,
`blocked`, `umbrella`, unlabeled pickup) are folded into ONE bucket
submit per repo that drains them sequentially on a single executor
worker, so a stale child cannot take the family slot and starve the
parent umbrella. When every family-aware issue in this tick's bucket
carries the `umbrella` label, the bucket is submitted cap-exempt and
runs on a dedicated executor pool (`_EXEMPT_POOL_WORKERS`, sized
independently of `global_cap`): umbrella handling is a pure
label/dep-graph walk with no agent or worktree work, so it must
always get its turn and must not consume a `MAX_PARALLEL_ISSUES_*`
slot. Mixed buckets (umbrella alongside `decomposing` / `blocked` /
unlabeled pickup) stay cap-counted because the non-umbrella entries
do real work. Each per-issue iteration inside the bucket wraps
`_process_issue` in `scheduler.track_active(repo, n)` (claim lives in
a separate set, so it does not inflate the global or per-repo cap
counters) so the refresh-skip contract keeps holding for the family
issue currently being processed. The scheduler enforces the
in-flight set, per-repo counter, family mutex, and rejects duplicate
active issues; every skip is logged with the reason
(`duplicate_active` at DEBUG, `family_slot_held` / `global_cap` /
`per_repo_cap` / `closed` at INFO), and rejected work re-tries on the
next polling pass. The pre-tick base refresh consults
`scheduler.is_active` per worktree so a base advance cannot rebase a
pre-PR worktree under a running agent or relabel a PR-having worktree
mid-handler. Each worker thread mints a fresh `GitHubClient` so
PyGithub `Requester`s aren't shared. The polling loop in
`main._run_tick` is nonblocking: each per-repo `workflow.tick` returns
as soon as it has submitted the eligible-issue callables, so a
long-running handler in one repo cannot block the next poll from
dispatching another repo's work or the same repo's other issues.
`_run_tick` calls `scheduler.reap()` and
`analytics.prune_with_retention_logging()` exactly once per polling
pass so worker failures recorded between polls surface in the
orchestrator log and the analytics retention window is applied at the
same cadence. `main` shuts the scheduler down (`wait=True`) on every
exit path (normal `--once`, SIGINT/SIGTERM, self-modifying-merge
restart) so in-flight workers complete cleanly; the SIGINT/SIGTERM
signal handler also calls `scheduler.shutdown(wait=False)` immediately
so an in-progress `workflow.tick` stops enqueueing new submits the
instant the user asks to stop instead of running its dispatch loop to
the end with `_running=False` and growing the in-flight set
post-signal.

**Workflow module split.** `workflow.py` is a slim facade owning the
tick loop, label dispatcher, unlabeled-pickup handler,
`_park_awaiting_human`, and `_run_agent_tracked`. Stage handler bodies
live under `orchestrator/stages/`; shared helpers live in
`workflow_drift.py`, `workflow_messages.py`, `worktree_lifecycle.py`
(worktree naming / layout / creation / restoration / cleanup helpers),
`git_plumbing.py` (the hardened git subprocess layer), `verify.py`
(the local-verify runner and its worktree-state probes),
`branch_publication.py` (the PR branch publication helpers --
`_CONVENTIONAL_RE`, `_is_conventional_subject`,
`_first_commit_subject`, `_pr_title_from_commit_or_issue`,
`_branch_ahead_behind`, `_squash_and_force_push`), and `base_sync.py`
(the per-tick base refresh and rebase routing). The five worktree-
subsystem modules were carved out of the original `worktrees.py`, and
`worktrees.py` is now a documented compatibility re-export hub that
imports every name from them under its historical surface so existing
call sites and `patch.object(worktrees, ...)` tests keep working. The
`workflow.py` facade re-exports cross-module helpers under their
original names, and stage modules call back via `from .. import
workflow as _wf` so existing `patch.object(workflow, ...)` tests keep
working.

**Tests.** Per-stage suites under `tests/test_workflow_*.py` cover
every handler; focused `tests/test_workflow_*_routing.py` modules cover
the dispatcher routing for each label, and the remaining facade-level
helpers (worktree serialization, drain-terminals, finalize-if-pr-merged,
stage analytics, fresh-feedback routing) live in their own focused
files. Shared helpers live in `tests/workflow_helpers.py` and in-memory
fakes in `tests/fakes.py`. Coverage spans the manifest parser,
watermarks, debounce, manual-merge HITL ping, squash, conflicts,
umbrella, multi-repo dispatch, and park-comment-replay prevention.

**Project CI.** GitHub Actions runs `ruff` and `pytest` on PRs under a
top-level `contents: read` token so the workflow is read-only and
non-publishing.
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
repeating the predicate). On top of the base indexes (`ts`,
`(event, ts)`, `(repo, issue)`, partial on non-null `stage`), the
schema carries per-event-kind partial indexes on `(repo, ts DESC)`
for `event='agent_exit'` and `event='stage_enter'` (the two hot
dashboard query shapes) and a composite `(event, repo, stage, ts)`
index for the multi-filter widgets. A `CREATE OR REPLACE VIEW
analytics_agent_runs` over `event='agent_exit'` rows promotes the
derivations the dashboard / read model want (`model` from
`COALESCE(models->>0, 'unknown')`, `total_tokens`,
`total_cache_tokens`, a categorical `review_round_bucket`
(`0`/`1`/`2`/`3-5`/`6+`), `failed = exit_code <> 0` with NULL
preserved, `has_cost = cost_usd IS NOT NULL`) so consumers do not
re-code them in every query. `ANALYTICS_DB_URL` is
a single libpq URL so swapping local for remote managed Postgres is a
one-line repoint. `orchestrator/analytics/sync.py` is the operator-
driven CLI (`python -m orchestrator.analytics.sync`) that replays
JSONL records into Postgres with `INSERT ... ON CONFLICT
(content_hash) DO NOTHING`, idempotent across repeated runs and
across `prune_old_records` rewrites. The CLI surfaces operator
feedback through the module logger and stdout summary: a UTC-stamped
`connecting` / `connection established` pair brackets the connect
call (with credentials stripped from both the netloc and libpq
query-string forms of the URL), a per-`_PROGRESS_INTERVAL`-lines
`progress lines=N inserted=… duplicate=… malformed=… elapsed=…s`
record advances on every chunk, and a final `completed in %.3fs (…)`
log plus a UTC-stamped stdout `duration_s=` summary close the run. `orchestrator/analytics/read.py`
is the read-side counterpart: a thin data-access module exposing
plain-Python functions that `orchestrator/dashboard.py` calls into.
The base-table aggregates (`get_filter_options`, `get_data_extent`,
`get_summary`, `get_time_series`, `get_stage_breakdown`,
`get_event_breakdown`, `get_recent_agent_exits`, `get_issues`,
`get_issue_events`, `get_repo_breakdown`, `get_hourly_heatmap`, and
the resolved/rejected `get_throughput_breakdown` over
`stage_enter` rows) carry the standard event / stage / date / repo /
issue filter contract. The agent-run aggregates that read the
`analytics_agent_runs` view (`get_review_round_breakdown`,
`get_backend_efficiency`, `get_cost_coverage`) cannot push an
`event IN (...)` clause down (the view has no `event` column -- it
filters internally to `event = 'agent_exit'`); they honor the
contract by short-circuiting to empty when the operator's events
selection excludes `agent_exit` (or is cleared) so the dashboard's
"show nothing for this dimension" semantics stays consistent across
widgets. `get_summary` rolls up `total_agent_runs` /
`failed_agent_runs` alongside the events / cost totals so the
success-rate panel reads off one query; `get_time_series` also
returns per-(day, event) cost / token aggregates so the
spend-over-time and tokens-over-time charts pivot the same query.
`get_stage_breakdown` rolls up cost / token totals per stage, and
`get_issues` adds `max_review_round` plus `failed_agent_runs` per
`(repo, issue)`. `get_cost_coverage` exposes the `unknown-price`
cohort verbatim -- never collapsed into a generic "unknown" bucket
-- so the operator can see how many runs the parser could not
price. `distinct_issues` in `get_summary` counts `(repo, issue)`
pairs so cross-repo windows do not collapse same-numbered issues.
Unset `ANALYTICS_DB_URL` short-circuits every read to an
empty / zero-valued result; connection / query failures wrap in a
single `AnalyticsReadError`. Streamlit-free in this layer so the
dashboard wiring can land independently. The driver is
`psycopg[binary]` (pinned in `pyproject.toml`) and lazy-imported in
both modules so the polling tick remains driver-free. Neither the
sync nor the read model is wired into the polling loop —
orchestrator correctness must not depend on database availability.

**Analytics dashboard.** `orchestrator/dashboard.py` is the
Streamlit app over the read model rendering the redesigned
standalone analytics view (#341). The chrome — topbar, filter
bar, KPI strip, insight banners, card grid — is hand-rolled
HTML injected through `dashboard_theme.PAGE_CSS`; Streamlit
owns only the inputs (the inline `3D` / `7D` / `All` preset
radio, the two date pickers, the sidebar multiselects, the
"By token type / By backend" toggle on the hero chart) and the
Plotly figures inside each card. Preset windows resolve via
`preset_window(...)` bounded by `get_data_extent`. The sidebar
still carries the repo selector, event / stage multi-selects,
and a `#123` / `123` issue-number input. Every read call is
wrapped in `st.cache_data` keyed by
`(start, end, repo, events, stages, issue)` so a filter change
invalidates every cached query in lockstep. The body renders, in
order: computed insight banners (failure rate ≥ 10 %, cost swing
≥ 25 % vs the previous window, unpriced cost coverage ≥ 10 %,
rework share ≥ 30 % from `get_review_round_breakdown`, and a
"spend is back-loaded" callout when the per-stage
`validating + documenting` cost exceeds `implementing` cost and
the latter is non-zero), a
four-tile KPI strip (total spend, total tokens, cost / resolved
issue, rework share — each with an inline-SVG sparkline and a
previous-window delta where applicable), the hero
`usage_over_time` stacked-area + cost-line chart with a
by-token-type / by-backend toggle, side-by-side
`cost_by_stage` (7/12) + `cost_by_review_round` (5/12)
horizontal-bar cards, a 7/5 split between the top-cost issues
table and the backend-efficiency cards + cost-source coverage
bar, another 7/5 split between the `cost_by_repo` bars and a
six-tile reliability panel above the issues-resolved-per-day
chart, the 7 × 24 weekday × hour activity heatmap, the recent
agent-runs table as a collapsible expander, and the per-issue
drill-down at the bottom. An empty-window guard (zero events
in the filtered window) short-circuits the body to a single
banner. Every filter is threaded through the read
model's SQL via `_build_window_where`, so every widget narrows
together rather than diverging by surface. The event / stage
selections distinguish three cases: ``None`` (no filter on the
column), a non-empty sequence (parameterised ``IN (...)``), and
an empty sequence (a tautologically-false predicate so the
cleared-multiselect case shows nothing rather than reverting to
"all"). The event multiselect maps straight to that contract
because `event` is `NOT NULL` in the schema; the stage
multiselect routes through `resolve_stage_filter` so the
all-selected default (and the no-stage-options case) collapses
to ``None`` rather than ``IN (every-non-null-stage)`` --
``options.stages`` lists only non-null stages, so the latter
would silently exclude legitimate NULL-stage rows
(`stage_evaluation` records on issues with no workflow label).
The issue input acts as a SQL-level filter that narrows every
widget when a specific repo is selected and triggers the
drill-down section; without a repo it stays inert (GitHub issue
numbers are not unique across repos) and the drill-down renders
an instructive notice. Streamlit (and its transitive pandas),
`plotly`, and the dashboard-only modules
`orchestrator.dashboard_charts` / `orchestrator.dashboard_theme`
are imported lazily inside `main()` so the polling tick stays
free of the dashboard's footprint, and the module loads cleanly
even when `streamlit` or `plotly` is not installed (a
`tests/test_dashboard.py` guard asserts the invariant for
`streamlit`, `pandas`, `plotly`, and
`orchestrator.dashboard_charts`). The dependencies live in a
separate `dashboard` group in `pyproject.toml` (`streamlit` plus
`plotly`) so `uv sync --locked` keeps installing only the minimum
runtime; `uv sync --group dashboard` then
`uv run streamlit run orchestrator/dashboard.py` is the launch
path. Missing-DB, empty-extent, and `AnalyticsReadError` cases
surface as in-app `st.warning` / `st.info` / `st.error` banners
that stop further rendering rather than crashing the app.

**Dashboard visual support layer.** `orchestrator/dashboard_charts.py`
holds pure Plotly figure builders (`usage_over_time` with a
`mode="type"` / `mode="backend"` switch, the shared
`cost_horizontal_bars` primitive plus the `cost_by_stage` /
`cost_by_review_round` / `cost_by_repo` adapters, `hour_weekday_heatmap`,
`done_per_day_bars`) that take read-model rows and return a
`plotly.graph_objects.Figure`; each builder routes its no-data
branch through a shared empty-state annotation so the "nothing
matches" message stays consistent across charts. The plotly-free
token module `orchestrator/dashboard_theme.py` exposes the
redesigned palette taken straight off the standalone mock's
`:root` block (cool gray `#f4f5f8` page, white cards, indigo
accent, `#1c2030` ink / `#565d72` ink-2 / `#8a90a3` ink-3 muted
tints, `--radius: 14px`, `--pad: 20px`, `--gap: 16px`, `1480px`
content max-width, IBM Plex Sans / Mono with system fallbacks,
plus per-token-type / per-backend / per-review-round / per-stage
/ per-`cost_source` palettes), the deterministic `color_for(...)`
fallback, a `base_layout(...)` dict the chart builders splat into
every figure, the `PAGE_CSS` block the dashboard injects through
`st.markdown` -- which also makes the topbar (sticky at `top: 0`)
and filter bar (sticky at `top: 71px`) full-bleed via a `100vw` +
negative-margin trick so they stay glued to the chrome as the
operator scrolls, matching the mock -- and the `fmt_money` /
`fmt_money_exact` / `fmt_tokens` / `fmt_num` formatters every
value label runs through. `.streamlit/config.toml` mirrors the
same palette into Streamlit's own `[theme]` (cool gray
background, indigo `primaryColor`, dark blue-gray text) and
disables the `[browser] gatherUsageStats` POST. The redesigned
`dashboard.py` consumes the chart builders
alongside HTML blocks for the topbar, KPI strip, insight stack,
backend-efficiency cards, cost-source coverage bar, reliability
tiles, and footer; the lazy import surface is asserted by
`tests/test_dashboard.py`, and the chart-builder regression
tests (`tests/test_dashboard_charts.py` -- skips cleanly when
`plotly` is absent -- and `tests/test_dashboard_theme.py`) keep
the plotly-touching code paths covered.

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
- **Symphony-inspired per-repo policy and hooks.** See
  [`plans/symphony-spec-review.md`](symphony-spec-review.md) for the full
  review. Two proposals survived the critical filter: a narrow
  `<target_root>/.agent-orchestrator/policy.toml` overrides file
  (verify commands, retry / review-round budgets) with hot-reload on
  file change, and three workspace lifecycle hooks
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
