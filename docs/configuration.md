# Configuration reference

All settings load from `.env` (or the process environment). [`../.env.example`](../.env.example) holds the basic
parameters needed for a first run; [`../.env.example.advanced`](../.env.example.advanced) carries common advanced
overrides and illustrative examples for opt-in settings. This page and the two beside it are the source of truth —
every setting and every default lives on one of them, and both `.env.example*` files keep their inline comments terse
and link back for the full rationale.

The orchestrator is deliberately stateless: every setting here selects backends and budgets at startup, or names
files/paths outside the repo. Per-issue state lives in the issue's pinned JSON comment on GitHub.

Two companion pages carry what is read on its own rather than scanned for a value:

- [`configuration/observability.md`](configuration/observability.md) — the sink paths and retention windows, the
  analytics database URL, skill-trigger tracking, the dashboard read mode, and the dashboard quickstart.
- [`configuration/operations.md`](configuration/operations.md) — continuous integration, run modes, the systemd user
  service, and what an edited `.env` takes to apply.
- [`configuration/snapshot-capability-check.md`](configuration/snapshot-capability-check.md) — the
  disposable-repository check that proves a production token and its rulesets can create, fetch, verify, and delete
  the late split's snapshot refs, and what each failure means.

Each of their sections keeps a one-paragraph pointer at its place below, so a link written against this reference
still lands on the answer.

## Required

- `GITHUB_TOKEN` — default _(required, env-only — not read from `.env`)_. fine-grained personal access token.
  A token written into `.env` is ignored with a warning at startup.
- `ORCHESTRATOR_TOKEN_FILE` — default `~/.config/<owner>/<repo>/token` (from `REPO`). path to the personal access
  token file (used when `GITHUB_TOKEN` is not in env)
- `HITL_HANDLE` — default `geserdugarov`. comma-separated GitHub logins to @-mention when a human is needed

### GitHub Personal Access Token

`GITHUB_TOKEN` is the fine-grained personal access token the orchestrator uses for every GitHub call. Required scopes on
the target repository:

- **Contents** — read/write (worktree branches and squash commits)
- **Issues** — read/write (label transitions, pinned-state comments, `HITL_HANDLE` @-mentions)
- **Pull requests** — read/write (opening PRs and posting PR comments; the orchestrator never merges PRs)
- **Metadata** — read-only (issue / PR enumeration)

Create the personal access token at <https://github.com/settings/personal-access-tokens>.

The token is deliberately NOT loaded from `.env`. The implementer agent runs in a sibling worktree with sandbox bypass,
so anything readable inside `REPO_ROOT` (including `.env`) is recoverable by a prompt-injected agent via a relative-path
read like `cat ../agent-orchestrator/.env`. `GITHUB_TOKEN` (and the aliases `GH_TOKEN`, `GITHUB_PAT`,
`GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GIT_TOKEN`) found in `.env` is logged-and-skipped at startup.

Token resolution order:

1. `GITHUB_TOKEN` exported in the orchestrator's launch environment.
2. The file at `~/.config/<owner>/<repo>/token` — path derived from `REPO`, override with `ORCHESTRATOR_TOKEN_FILE`.
   Pick a path the agent worktree cannot reach via known relatives, and `chmod 600` it.

## Target repository

Use `REPO` for a single repo (the default), or `REPOS` to drive several from one process. When `REPOS` is set, the
legacy single-repo quartet (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME`) is ignored.

- `REPO` — default `geserdugarov/agent-orchestrator`. `owner/name` of the single repo to manage (ignored when `REPOS`
  is set)
- `TARGET_REPO_ROOT` — default `REPO_ROOT` (self-bootstrap). path to the local clone of `REPO` — worktrees are
  `git worktree add`-ed from here
- `BASE_BRANCH` — default `main`. branch PRs target
- `REMOTE_NAME` — default `origin`. git remote in `TARGET_REPO_ROOT` that points at `REPO` on GitHub
- `REPOS` — default _(unset)_. multi-repo configuration, entries separated by newlines or `;`

### Multi-repo `REPOS` syntax

Each entry is `owner/name|target_root|base_branch`, with two optional trailing fields:

- fourth `|remote_name` — defaults to `origin`;
- fifth `|parallel_limit` — defaults to `MAX_PARALLEL_ISSUES_PER_REPO`. Positional: to override `parallel_limit` you
  must also write the `remote_name` (use `origin` explicitly to keep the default).

```dotenv
REPOS=acme/api|/srv/clones/acme-api|main;acme/web|/srv/clones/acme-web|master|private|2
```

Validation happens at import — a malformed entry, empty owner/name, empty base branch, empty `remote_name`, a
non-integer or non-positive `parallel_limit`, or a duplicate slug aborts startup with a clear error. A `target_root`
that does not exist on disk warns to stderr but does not block startup.

Each repo can have its own personal access token at `~/.config/<owner>/<repo>/token`, or a single `GITHUB_TOKEN`
covering every listed repo. Worktrees are namespaced `WORKTREES_DIR/<owner>__<name>/issue-N` and PR branches are
namespaced `orchestrator/<owner>__<name>/issue-N`, so two repos with the same issue number cannot collide on disk or on
the branch ref — important when several `REPOS` entries share a `target_root` (e.g. one local clone with multiple
remotes), where git would otherwise refuse to check the same `orchestrator/issue-N` ref out in two worktrees. In-flight
issues whose pinned `branch` was set before this change keep using the legacy `orchestrator/issue-N` name; fresh issues
take the namespaced form.

Slugs whose repo name contains `.lock`, `..`, or a trailing `.` (all rejected by `git check-ref-format`) get an extra
`__h<16-hex>` suffix on the branch segment so two distinct slugs that would otherwise collapse to the same form (e.g.
`owner/foo.lock` and `owner/foo_lock`) stay on distinct branches. The worktree directory keeps the readable
`<owner>__<name>` form because filesystems tolerate these characters; only the branch ref carries the hash.

## Agent roles

The first token of each role spec selects the backend (`codex` / `claude`); any remaining tokens are forwarded as
backend-CLI args (model, reasoning effort, etc.). See
[`workflow/command-specs.md`](workflow/command-specs.md) for the spec format, in-flight session lock, and full
examples.

- `DEV_AGENT` — default `claude`. implementer command spec
- `REVIEW_AGENT` — default `codex`. reviewer command spec
- `DECOMPOSE_AGENT` — default `claude`. decomposer command spec (validated even when `DECOMPOSE=off`); also drives the
  `question` and `discussion` stages
- `DECOMPOSE` — default `on`. enable the `decomposing` stage; `off` reverts to the legacy
  "no label → `workflow:implementing`" pickup, and sends an issue already sitting on `workflow:decomposing` the same
  way through that stage's own handler (once any half-finished split above it is settled) rather than spawning
  another decomposer. What it does not gate is the two operator-applied conversation labels: `question` and
  `discussion` still run on the decomposer's spec with `DECOMPOSE=off`, which is why that spec is validated either
  way
- `CODEX_BIN` — default `codex`. executable launched when a role's first token is `codex`; override only if `codex` is
  not on `$PATH`
- `CLAUDE_BIN` — default `claude`. executable launched when a role's first token is `claude`; override only if
  `claude` is not on `$PATH`
- `ALLOWED_ISSUE_AUTHORS` — default _(unset)_. comma-separated GitHub logins; when set, only auto-pick-up unlabeled
  issues from those authors, and the per-tick sweep labels open PRs from anyone outside the list with
  `workflow:community_contribution` and @-mentions `HITL_HANDLE` once per PR (bot-authored PRs such as Dependabot are
  excluded via `user.type == "Bot"`). When set it additionally becomes a comment trust boundary: comments from authors
  outside the list stay visible on GitHub but are dropped from the conversation text fed to every agent prompt
  (implement / review / documentation / decompose / question / discussion / conflict, the awaiting-human resumes, and
  the `in_review` / `fixing` PR-feedback loop), from the base-sync auto-rebase retry-unpark signal, and from the
  `user_content_hash` drift signal, so an outsider on a public repo cannot inject workflow-driving instructions into
  an agent, resume an awaiting-human session, retry a parked auto-rebase, reset the review-round cap via
  `/orchestrator add-review-rounds`, route `in_review` to `workflow:fixing` (or set its pending-fix bookmark), or
  shift the hash to re-trigger drift. Login comparison is case-insensitive; an empty allowlist trusts every author
  (legacy single-user behavior), so on these prompt / resume / PR-feedback surfaces a Bot/App login is gated like any
  other author — excluded once the allowlist is populated and its login is not on it. A separate `user.type == "Bot"`
  structural check, independent of the allowlist, covers the `user_content_hash` drift hash and the
  community-contribution PR sweep. One prompt keeps something the allowlist would drop: the `discussion` stage
  rebuilds the whole conversation for a round with no session to resume, and retains the orchestrator's *own* posted
  comments there — by the ids it recorded when it posted them — so a deployment that lists its humans and not the
  token's account still hands that agent both halves of the thread. No third-party comment is ever retained.
  See [`state-machine/delivery-stages.md`](state-machine/delivery-stages.md#user-content-drift-detection) for the
  full drift-hash filter list and
  [`security.md`](security.md#comment-trust-boundary-allowed_issue_authors) for the trust-boundary rationale

## Cadence and budgets

- `POLL_INTERVAL` — default `60`. seconds between polling ticks
- `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` — default `1`. how many ticks apart the closed-issue recovery sweep runs (see
  [GitHub rate limits](#github-rate-limits) below). The sweep issues one `GET …/issues?state=closed&labels=<L>` per
  non-terminal workflow label the repository actually carries **per repo**; across many repos at a short
  `POLL_INTERVAL` that fixed cost dominates request volume and is the main driver of GitHub *primary* rate-limit
  (5000 req/hour/PAT) exhaustion. `1` runs it every tick (unchanged behavior). Raise it (e.g. `4`–`5`) on multi-repo
  deployments; the only cost is that an externally-merged/closed issue may take up to `N-1` extra ticks to finalize to
  `done`. The latency-sensitive open-issue poll always runs every tick. A closed `discussion` is the one issue the
  sweep keeps costing: with no plan PR published it waits the same `N-1` ticks to finalize to `rejected`, and with one
  still open it deliberately keeps its `discussion` label so the sweep goes on yielding it every pass until the humans
  decide that pull request — a verdict `N>1` therefore picks up that many ticks later as well.
- `AGENT_TIMEOUT` — default `1800`. wall-clock cap per agent invocation, seconds
- `REVIEW_TIMEOUT` — default (= `AGENT_TIMEOUT`). wall-clock cap per reviewer invocation, seconds
- `SHUTDOWN_GRACE_SECONDS` — default `30`. seconds after SIGTERM/SIGINT before the loop force-terminates in-flight
  agent subprocesses and hard-exits (`128 + signum`). Keep below the systemd unit's `TimeoutStopSec` (90s) so
  `systemctl restart` sees a clean stop instead of escalating to SIGKILL — without it the drain waits on in-flight
  workers bounded only by `AGENT_TIMEOUT` (1800s) or a blocking GitHub retry/backoff, which overruns the stop deadline.
- `MAX_REVIEW_ROUNDS` — default `3`. review/fix iterations before parking on `awaiting_human`
- `MAX_CONFLICT_ROUNDS` — default `3`. auto-conflict-resolution rounds before parking on `awaiting_human`
- `MAX_RETRIES_PER_DAY` — default `3`. fresh implementer spawns per issue per 24h window (`0` = unbounded)
- `DEV_SESSION_MAX_RESUMES` — default `10`. resumes of one dev session before it is retired and respawned fresh from
  durable state (issue body + recent comments + the committed branch), so a growing `--resume` transcript cannot creep
  into a `Prompt is too long` context overflow. `0` = resume forever. The reactive overflow handler still recovers a
  session that blows the window in fewer resumes.
- `MAX_ADDED_LINES` — default `4000`. size ceiling one implementation candidate may publish under, counted in the
  textual lines its prospective pull request **adds**: the frozen remote base commit against the exact committed
  candidate commit, across every path. Binary content contributes nothing (git has no lines to report for it), a
  moved file counts where it lands (rename detection is off, so relocating work cannot buy a smaller number), and
  there is no exemption for lockfiles, generated code, migrations, snapshots, golden fixtures, i18n catalogs,
  notebooks, or vendored trees — an exemption list is a bypass anybody can move work into, and the number has to be
  reproducible from the diff a reviewer opens. What decides which paths have lines, and how many, is pinned to the
  commit rather than to the checkout — attributes are read from the candidate's own tree, so an uncommitted
  `.gitattributes` cannot make textual work report as binary, and the diff algorithm is named, so the same two
  commits cannot count differently on two hosts — and a reading that cannot be pinned (an `info/attributes` file, a
  diff driver in the repository's config) is a typed failure, never a count of zero. The
  comparison is **strictly greater than**: a candidate landing exactly on the configured value publishes the way it
  always has, and only one past it is oversized. Positive integer, validated at import like the parallelism caps —
  `0` or a negative would call every candidate oversized. Global on purpose; a per-repository override waits on
  telemetry that shows repository skew
- `ORCHESTRATOR_BASE_BRANCH` — default `main`. base branch of the orchestrator's own repo, used by the self-update
  path
- `SQUASH_ON_APPROVAL` — default `on`. after the reviewer emits `VERDICT: APPROVED`, squash the dev's commits on the
  PR branch into a single subject-only commit and force-push with lease. The subject reuses the dev's first commit
  subject when it carries a reusable `<prefix>:` form (Conventional **or** repo-local such as `event:`/`career:`);
  otherwise it is synthesized with a prefix inferred from recent base-branch history. `off` leaves the per-step commit
  history intact (useful when downstream tooling depends on it). Parsed as a boolean: `1` / `true` / `on` / `yes`
  enable, anything else disables.
- `EXPOSE_TRACKED_REPOS` — default `on`. tell working agents about the *other* repos this orchestrator tracks (slug,
  local `target_root`, base branch) for cross-repo reference. Inert for single-repo hosts — the awareness block is
  emitted only when more than one repo is configured, so a default deployment sees zero added prompt tokens. The
  disclosed data is operator-configured and non-secret (no tokens, no remote URLs), and write-containment is unchanged.
  `off` forces the disclosure off globally. Parsed as a boolean: `1` / `true` / `on` / `yes` enable, anything else
  disables.

### GitHub rate limits

A single fine-grained PAT gets **5000 REST requests/hour** (the GitHub *primary* rate limit). Each tick spends a roughly
fixed number of `GET /repos/…` requests **per repo**, independent of how much real work the repo has:

- the open-issue poll (`list_pollable_issues`): 1+ requests,
- the closed-issue recovery sweep: one `GET …/issues?state=closed&labels=<L>` per non-terminal workflow label (8
  today — the six PR-carrying stages plus `question` and `discussion`, each of which has a terminal a closed issue
  may still owe), and
- the community-contribution PR sweep: 1 `GET …/pulls` request.

With `R` repos at `POLL_INTERVAL` seconds, the floor is roughly `R × (10 + sweep) × 3600 / POLL_INTERVAL`
requests/hour even when every repo is idle. Past ~5–6 repos at the 60s default this exceeds 5000/hour: the budget is
spent partway into each hour, GitHub starts returning `403: Forbidden` with an `X-RateLimit-Reset` in the future, and
PyGithub's `GithubRetry` sleeps (uninterruptibly) until the reset — typically a ~15–18 minute stall **every hour**, on
the hour. The signature in `orchestrator.log` is repeated `github.GithubRetry: … failed with 403: Forbidden` followed
by `Setting next backoff to <hundreds-to-1000+>s`.

Two built-in mitigations reduce the floor without touching `POLL_INTERVAL`:

- **Workflow-label objects are cached** per repo client. They are immutable after `ensure_workflow_labels`, so the
  closed sweep no longer re-fetches them every tick — eliminating 8 `GET …/labels/<name>` requests per repo per tick
  automatically.
- **A pre-namespace label the repository does not have is asked for rarely.** The sweep looks up each swept state
  under both its `workflow:`-namespaced name and its pre-namespace one, so an issue still carrying the old label is
  not stranded (see [`state-machine/labels-and-state.md`][pollable-issues]). A lookup that comes back
  404 cannot be cached as a Label object, so it is instead thrown away for 20 sweeps before being asked again — on a
  repository the bootstrap rename already reached, the five legacy spellings therefore cost five `GET …/labels/<name>`
  requests every twentieth sweep and nothing in between. A 403 is never treated this way: rate-limit exhaustion is not
  an answer about whether the label exists, so it is retried on the next sweep.
- **`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS`** batches the closed-issue recovery sweep to once every N ticks (see the list
  above). At `N=4`–`5` the closed-label queries are amortized down by the same factor while the open-issue poll
  stays every tick.

If you still approach the cap, the remaining levers are operator-side: raise `POLL_INTERVAL`, split repos across more
than one PAT (one token file per slug under `~/.config/<owner>/<repo>/token`), or reduce the number of tracked repos.
Check current consumption with `curl -H "Authorization: Bearer $TOKEN" https://api.github.com/rate_limit`.

## Local verification gate

When the reviewer agent emits `VERDICT: APPROVED`, `_handle_validating` runs the configured `VERIFY_COMMANDS` in the
per-issue worktree **before** posting the approval comment, squashing, seeding watermarks, or relabeling to
`workflow:documenting`. A clean run advances the issue as usual; any failure parks the issue on `workflow:validating`
with `awaiting_human=True` and a typed `park_reason`, so an operator can fix the breakage and resume.

The verify gate is the first gate after the reviewer agent — it catches regressions locally so an obviously-broken
branch never reaches `in_review`. GitHub CI still runs against the PR; the human merging the PR is the consumer of CI's
verdict, since the orchestrator never merges from `in_review` itself.

### Secret stripping

The verify shell shares the agent's environment filter (`agents.environment.filter_agent_env`, called with
`allow_provider_auth=False`). Stripped from the verify environment:

- GitHub-token aliases (`GITHUB_TOKEN`, `GH_TOKEN`, …).
- Secret-shaped vars: anything matching `*_TOKEN` / `*_KEY` / `*_SECRET` / `*_PASSWORD` / `*_PAT` / `*_CREDENTIAL`, plus
  the bare names `TOKEN` / `KEY` / `SECRET` / `PASSWORD` / `PAT` / `CREDENTIAL`.
- Credential-file locators: `*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`, `*_CREDENTIAL_FILE`,
  `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` / `CREDENTIALS_FILE`. Explicitly covers
  `ORCHESTRATOR_TOKEN_FILE`, `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_SHARED_CREDENTIALS_FILE`.
- Write-credential locators: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`, `GIT_SSH_COMMAND`.
- The agent's own provider-auth keys: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `OPENAI_API_KEY` (stricter than the agent-subprocess case — a verify command runs operator-configured shell against
  agent-produced code, and a hostile dependency reading `$ANTHROPIC_API_KEY` would gain billable access to the
  operator's model account).

**Do not embed secret literals in `VERIFY_COMMANDS`.** Verify failures park `awaiting_human` with the offending command
string published *verbatim* in the GitHub issue comment, so an inline `ANTHROPIC_API_KEY=sk-… pytest` entry would leak
the literal secret on the first failure. If a verify command legitimately needs a secret-shaped var, load it from disk
inside a wrapper script and reference the script from `VERIFY_COMMANDS` — `VERIFY_COMMANDS=./scripts/run-verify.sh`
where the script reads the value from a file outside the worktree (`~/.config/<provider>/key`) and exports it before
running tests.

### Settings

- `VERIFY_COMMANDS` — default _(empty — no verification)_. Ordered shell commands run sequentially in the per-issue
  worktree on `VERDICT: APPROVED`. Entries are separated by `;` or newlines; blank lines and `#`-comment lines are
  skipped. Each entry runs via the shell so quoting, pipes, and `&&` work; stdout and stderr are merged into one
  captured block.
- `VERIFY_TIMEOUT` — default `600`. Per-command wall-clock cap in seconds. A single slow command parks with
  `verify_timeout`. Ignored when `VERIFY_COMMANDS` is empty.

### Failure modes and `park_reason` tokens

The park comment names the failing command, its exit code (or timeout), and a redacted / truncated tail (last 4096
bytes) of the captured output. Output is redacted via `config.credentials.redact_secrets` **before** truncation so a
secret straddling the cut cannot leak a partial value. `park_reason` is set to one of:

- `verify_failed` — Command exited non-zero.
- `verify_timeout` — Command exceeded `VERIFY_TIMEOUT`.
- `verify_dirty` — Command exited 0 but left uncommitted changes in the worktree (handing off a dirty tree would
  advertise the PR as ready for human merge with state the dev never committed).
- `verify_head_changed` — Command exited 0, tree clean, but the command moved `HEAD` (e.g. ran `git commit` on its
  own). The subsequent squash + force-push would otherwise publish an unreviewed commit; the park comment surfaces the
  before / after SHAs.

### Examples

```dotenv
# single command
VERIFY_COMMANDS=python3 -m pytest -q

# multiple commands (semicolon-separated because the .env loader cannot
# represent newlines inside a value)
VERIFY_COMMANDS=python3 -m pytest -q;ruff check .

# raise the per-command cap to 20 min for a slow test suite
VERIFY_TIMEOUT=1200
```

When exporting in a shell instead of `.env`, prefer one command per line — the parser accepts both `;` and newlines as
separators.

## Parallel processing

Each polling tick advances issues concurrently along two axes:

- **Across repos.** When `REPOS` lists more than one entry, `runtime.ticks.run_tick` fans the per-repo
  `workflow.tick(gh, spec)` calls out across a `ThreadPoolExecutor` (one worker per repo). The legacy single-repo mode
  (`REPOS` unset) stays in-thread.
- **Within a repo.** Per-issue handlers are dispatched to a long-lived `IssueScheduler`. Fan-out issues
  (`workflow:ready` / `workflow:implementing` / `workflow:documenting` / `workflow:validating` / `in_review` /
  `workflow:fixing` / `workflow:resolving_conflict` / `question` / `discussion`) are submitted one callable per
  issue. Family-aware issues (`workflow:decomposing` / `workflow:blocked` / `workflow:umbrella` / unlabeled pickup)
  are folded into ONE bucket submit per repo that drains them sequentially.

The two caps below are the levers:

- `MAX_PARALLEL_ISSUES_PER_REPO` — default `1`. per-repo cap on concurrent in-flight per-issue handlers. Each `REPOS`
  entry can override via its fifth pipe-separated field. Must be a positive integer.
- `MAX_PARALLEL_ISSUES_GLOBAL` — default `3`. global cap across all configured repos. Must be a positive integer;
  raise only with the CPU / memory headroom to run that many agent CLIs at once. No-agent family buckets
  (`workflow:blocked` / `workflow:umbrella`) are cap-exempt and run on a dedicated executor.
- `WORKFLOW_TRANSITION_GUARD` — default `warn`. governs the workflow-label transition-legality check in
  `set_workflow_label` against the declared `ALLOWED_TRANSITIONS` table (see
  [`state-machine/labels-and-state.md`][transition-guard]). `warn` logs an illegal transition and
  proceeds; `enforce` raises; `off` disables it. The label *typo* guard is always strict regardless of this setting.
  One write is exempt even under `enforce`: the late size gate putting a label back where a human moved it from,
  which is a repair of a move the transition graph never declared an edge for — enforcing there would strand the
  issue under the wrong label for as long as the guard stayed on. Invalid values abort at startup.

Both caps are enforced by a single `IssueScheduler` (`orchestrator/scheduler/`) built once at startup and threaded
through every `workflow.tick` call. New callers may pass a frozen `SubmissionRequest`; the historical
`submit(repo_slug, issue_number, fn, *, ...)` positional/all-keyword API remains supported. A submit is skipped this
tick (and retried next pass) when:

- the `(repo_slug, issue_number)` pair is already in flight (duplicate-active gate),
- the global or per-repo cap is reached,
- another family worker on the same repo is already in flight (family mutex).

**No-agent bucket exemption.** When every family-aware issue in this tick's bucket runs a no-agent handler —
`workflow:blocked` or `workflow:umbrella`, both pure label / dep-graph walks — the dispatcher submits the bucket as
cap-exempt: it does not consume cap slots and runs on a dedicated executor pool. This keeps a cheap-polling parent
from being starved by ordinary implementation work; in particular a blocked parent waiting on its own children would
otherwise deadlock those children for the only per-repo slot under the default `parallel_limit=1`. The family mutex
still applies. A bucket containing `workflow:decomposing` (spawns the decomposer agent) or an unlabeled-pickup issue
stays cap-counted.

**Family vs fan-out labels:**

- **Family-aware** (`workflow:decomposing`, `workflow:blocked`, `workflow:umbrella`, unlabeled): read and write
  cross-issue state (parent ↔ child) and must never run two at a time on the same repo.
- **Fan-out** (`workflow:ready`, `workflow:implementing`, `workflow:documenting`, `workflow:validating`, `in_review`,
  `workflow:fixing`, `workflow:resolving_conflict`, `question`, `discussion`): only touch per-issue state; fan out
  concurrently up to the caps.

The pre-tick base refresh (`_refresh_base_and_worktrees`) is scheduler-aware: per-issue worktrees whose handler is
currently in flight are skipped this tick, so a base advance cannot rebase a pre-PR worktree under a still-running
agent. The skip is conditional on active state.

`shutdown(wait=True)` runs on process exit (normal `--once` return, `SIGINT` / `SIGTERM`, or self-modifying-merge
restart) so any in-flight workers complete cleanly. The signal handler also calls `scheduler.shutdown(wait=False)`
synchronously the instant the signal lands, so the submit path is closed mid-tick.

`runtime.ticks.run_tick` calls `scheduler.reap()` exactly once per polling pass (right before
`retention.prune_with_retention_logging()`) so worker failure-completion records drain before the next iteration.
`_dispatch_via_scheduler` deliberately does NOT reap.

Non-positive or non-integer values for either cap (or for a per-entry `parallel_limit`) abort startup with a clear
error.

## Workspace and agent identity

- `WORKTREES_DIR` — default `../wt-orchestrator`. where per-issue git worktrees are created; layout is
  `WORKTREES_DIR/<owner>__<name>/issue-N`
- `LOG_DIR` — default `<REPO_ROOT>/logs`. directory `runtime/logs.py` attaches its `FileHandler` under
  (`orchestrator.log`, rotated ~10 MiB × 5). Also the default parent for `ANALYTICS_LOG_PATH`
  (`LOG_DIR/analytics.jsonl`). Already covered by the `*.log` `.gitignore` rule.
- `AGENT_GIT_NAME` — default `agent-orchestrator`. `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` injected into agent spawns
- `AGENT_GIT_EMAIL` — default `agent-orchestrator@users.noreply.github.com`. `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL`
  injected into agent spawns

## In-review behavior

The orchestrator is permanently manual-merge-only: humans click Merge. `_handle_in_review` routes fresh PR feedback to
`workflow:fixing`, pings the HITL handles once per head SHA when the PR is mergeable and the current head completed
the reviewer-approved final-docs handoff (or carries a real GitHub APPROVED review), and parks awaiting human
attention for an unmergeable PR.

- `IN_REVIEW_DEBOUNCE_SECONDS` — default `600`. quiet window the `fixing` stage honours before resuming the dev on PR
  feedback. Newer comments arriving while already labeled `workflow:fixing` reset the window. `_handle_in_review`
  itself routes fresh feedback to the fixing loop immediately and does NOT apply the debounce.

## Observability

Every sink path, retention window, and observability switch — `EVENT_LOG_PATH`, `ANALYTICS_LOG_PATH`,
`ANALYTICS_RETENTION_DAYS`, `ANALYTICS_DB_URL`, `TRAJECTORY_LOG_PATH`, `TRAJECTORY_RETENTION_DAYS`,
`TRACK_SKILL_TRIGGERS`, and `DASHBOARD_PARALLEL_READS` — is documented in
[`configuration/observability.md`](configuration/observability.md), together with where each is parsed and which
process reads it. What the sinks record, and how the database, dashboards, and usage parser read them back, is on the
pages [`observability.md`](observability.md) maps.

### Analytics dashboard quickstart

The five steps from a JSONL sink to a running Streamlit page — confirm the records, start Postgres, point
`ANALYTICS_DB_URL` at it, sync, launch — are in
[`configuration/observability.md#analytics-dashboard-quickstart`](configuration/observability.md#analytics-dashboard-quickstart).

## Continuous integration

[`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs `ruff check orchestrator tests`,
`flake8 orchestrator tests --select=WPS`, and `pytest tests` as three separate mandatory steps for every push to
`main` and every pull request, and Dependabot opens weekly `workflow:dependencies` update PRs. The per-file lint
scopes, the repository-wide 120-column target, the workflow's read-only token, and the dependency review are in
[`configuration/operations.md#continuous-integration`](configuration/operations.md#continuous-integration).

## Run modes

`./run.sh` for production polling, `python -m orchestrator --once` for a single tick, `--log-level DEBUG` for verbose
logs, and the `agent-orchestrator` console script equivalent to all three are in
[`configuration/operations.md#run-modes`](configuration/operations.md#run-modes).

## Running under systemd (user service)

The recommended production deployment is a systemd **user** service supervising `run.sh` directly. The unit file, the
`loginctl enable-linger` that boot-time start requires, and the day-to-day `systemctl --user` commands are in
[`configuration/operations.md#running-under-systemd-user-service`](configuration/operations.md#running-under-systemd-user-service).

## Applying `.env` changes

`.env` is read once, when `python -m orchestrator` starts, so most edits take effect on the next fresh Python start —
there is no signal to make a running process re-read configuration. Which restart is safe, what each launch style
needs, and when each individual setting takes effect are in
[`configuration/operations.md#applying-env-changes`](configuration/operations.md#applying-env-changes).

### What survives a restart

Per-issue progress lives in the issue's pinned JSON comment on GitHub and in the per-issue worktree, so restarting
between ticks loses nothing. The two hazards that are not covered by that — a live `codex` / `claude` child, and the
agent spec pinned into an in-flight session — are in
[`configuration/operations.md#what-survives-a-restart`](configuration/operations.md#what-survives-a-restart).

## Control labels

- `backlog` — Apply to an issue (typically at creation) to keep the orchestrator from picking it up. The dispatcher
  skips the issue entirely while the label is present; remove the label to release the issue for processing.
- `paused` — Same hard skip as `backlog`, but intended for an already in-flight issue: apply it to freeze processing
  (no handler runs, no worktree is rebased, no PR-stage relabel) without discarding the issue's state, and remove it
  to resume where it left off. Removing the label is the whole resume action, honored on the next poll; there is no
  un-pause command, and `/orchestrator continue` is unrelated — it retries a specific `awaiting_human` session-failure
  park (`agent_silent` / `agent_timeout`) across the dev stages (`implementing`, `documenting`, `validating`,
  `fixing`, `resolving_conflict`), not a `paused` hold. Applying `paused` while a developer agent is mid-run also
  takes effect: every stage that resumes a dev agent (`implementing`, `validating`, `documenting`, `in_review`,
  `fixing`, `resolving_conflict`) re-reads the label after the run returns, before any post-agent side effect, and
  discards the result rather than pushing, opening a PR, relabeling, advancing watermarks, or posting comments, so the
  committed work stays on the branch and republishes once the label is removed. The two conversation stages honor it
  on the same terms: a `paused` that lands while a `question` or `discussion` round is running suppresses every
  disposition below it, so nothing is posted, parked, folded into the usage counters, or written to pinned state. A
  discussion round that had just committed the confirmed plan keeps that commit on its branch and has it published by
  the tick after the label comes off, classified against the round anchor the stage wrote before the spawn.
- `workflow:community_contribution` — Applied automatically (not by an operator) by the per-tick open-PR sweep when
  `ALLOWED_ISSUE_AUTHORS` is set: any open PR whose author is outside the allowlist is labeled and `HITL_HANDLE` is
  @-mentioned once per PR so a human reviews the community-submitted work. Bot authors (Dependabot, Renovate, CI bots)
  are skipped. With the allowlist empty (the default), the sweep is a no-op. It carries the `workflow:` prefix — unlike
  the two controls above — because the orchestrator writes it itself; a PR still carrying the pre-namespace spelling
  counts as already labeled, so the migration cannot cost it a second HITL ping.

[pollable-issues]: state-machine/labels-and-state.md#pollable-issues-and-finalization
[transition-guard]: state-machine/labels-and-state.md#typed-states-and-the-transition-guard
