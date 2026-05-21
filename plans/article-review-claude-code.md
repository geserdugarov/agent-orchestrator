# Design doc: extensibility & observability proposals from the Claude Code article

Resolves #56. The source article is
<https://sathwick.xyz/blog/claude-code.html>; this doc is the
deliverable, not a placeholder for follow-up issues. Anything in here
that the maintainer wants to actually build can be lifted into its own
ticket later; the design rationale stays here either way.

## What was reviewed

The article walks through Claude Code's internals — query-loop state
machine, terminal renderer (Ink + Yoga + double-buffered cell grid),
multi-strategy context compaction, speculation state, a four-type
memory system (user / feedback / project / reference), hooks,
deferred tool discovery, ULTRAPLAN remote planning,
KAIROS auto-dreaming, sampled startup profiler, five-mode permission
system, and more.

Most of it does not translate to this project. The orchestrator is a
stateless 60-second polling loop that drives `codex` / `claude` as
subprocesses; it owns no chat UI, no rendering, no in-memory tool
schema, no per-turn context window, no permission prompts. Patterns
around terminal rendering, prompt-cache stability, max-output-token
escalation, microcompaction, snip boundaries, AppState/Zustand, etc.
have no obvious surface to attach to here.

Three patterns do have surface. They are described below as design
proposals, deliberately small, none of which need to ship for the
current workflow to keep working. If only one were to ship,
**proposal 1 (tick event log)** is the clearest win — small,
isolated, and pays back the moment an issue gets stuck.

## Proposal 1: JSONL tick event log

### Problem

When an issue gets stuck on `awaiting_human` or ping-pongs between
`validating` and `in_review`, the only signal an operator has is the
issue's pinned-state comment and the orchestrator's stdout log. The
stdout log is unstructured, intermixed across repos and issues, and
gone the moment `run.sh` re-execs after a self-modifying merge.

### Sketch

Optional JSONL audit log, opt-in via env var (`EVENT_LOG_PATH`,
unset = disabled). One line per significant transition, written
synchronously from inside the relevant handler. Schema:

```json
{"ts": "2026-05-19T12:34:56Z", "repo": "owner/name", "issue": 42,
 "stage": "validating", "event": "review_verdict", "verdict": "approved",
 "agent": "codex", "session_id": "…", "duration_s": 47.3,
 "review_round": 1, "retry_count": 0}
```

Event kinds, to start: `stage_enter`, `agent_spawn`, `agent_exit`,
`review_verdict`, `merge_attempt`, `park_awaiting_human`,
`conflict_round`, `pr_opened`, `pr_merged`, `pr_closed_without_merge`.
Each event names exactly one issue; cross-repo grep is the operator's
problem.

### Why this fits the architecture

The pinned state comment already exists for durability and human
inspection; the event log is its low-frequency stream complement.
Both can disagree (the log is append-only, the pinned state is
last-write-wins); when they do, the pinned state wins by design.

### Out of scope

- No log rotation; operators point `EVENT_LOG_PATH` at a path their own
  logrotate handles, or at `/dev/stdout`.
- No metrics aggregation, no dashboards. Anyone who wants those reads
  the JSONL themselves.
- No cross-tick correlation IDs beyond `(repo, issue, ts)`.

### Acceptance

- `EVENT_LOG_PATH` unset → no behavior change.
- `EVENT_LOG_PATH=/tmp/orch.jsonl` → at least one event per stage
  transition lands in the file, valid JSONL, one line per event.
- Tests in `tests/test_workflow.py` exercise the new emission points
  via `FakeGitHubClient` and assert on captured events.

### Inspiration

Claude Code's startup profiler: sampled telemetry that lets the team
investigate cold paths without rebuilds. The orchestrator's analog is
much smaller — there is no hot loop to profile — but the same
"emit structured events behind a flag" principle applies.

## Proposal 2: Per-stage operator hooks

### Problem

Today every per-repo observer integration (post to Slack on
`validating → in_review`, mirror PR-opened events into a JIRA
ticket, warn the on-call channel when a `migrations/` change
lands, ship transition events to an external compliance audit log,
etc.) requires editing `workflow.py`. Multi-repo deployments cannot
do this without forking.

All of these are post-action observers, not gatekeepers. Pre-action
veto cases ("don't open the PR if X", "refuse to auto-merge if Y")
are deliberately *not* in scope for this proposal — see
"Hooks are observers, not gatekeepers" below.

### Sketch

A declarative hook config — `ORCHESTRATOR_HOOKS_FILE` env var pointing
at a YAML file (or, if we want to keep `pyproject.toml`-only deps, a
JSON file). Schema:

```yaml
hooks:
  - on: stage_enter
    stage: validating
    repo: acme/api               # optional; absent = all repos
    when:                        # optional declarative predicate map
      branch_prefix: orchestrator/issue-
    exec:                        # absolute path; no shell, no PATH lookup
      - /opt/orchestrator-hooks/notify-slack.sh
      - "${REPO}"
      - "${ISSUE}"
      - validating
    timeout_s: 30
    fail: warn                   # one of: warn | block | ignore
```

### Filtering and placeholder substitution

Two related primitives that both need to be expression-free.

**`when:`** is an optional map of `field_op: value` predicates,
ANDed. Supported keys: `repo_equals`, `branch_prefix`,
`branch_suffix`, `branch_contains`, `agent_in` (list),
`event_in` (list), `stage_in` (list), `pr_state_in` (list). There
is no expression language and no embedded code. The evaluator
walks the keys, looks each one up in the fire-time event map
described below, and applies the named operator (`_equals`,
`_prefix`, `_suffix`, `_contains`, `_in`). Unknown keys or
unknown operators are config errors caught at hooks-file load.

**`exec:` placeholders** are substituted at fire time, not
config-load time. The exact placeholder set is the fixed map
built from the current event: `${REPO}`, `${ISSUE}`, `${STAGE}`,
`${EVENT}`, `${PR_NUMBER}` (empty string for pre-PR events),
`${BRANCH}` (empty string when the issue has no branch yet),
`${WORKTREE}` (empty string when no worktree exists), and
`${AGENT_BACKEND}` (empty string outside agent-spawn events).
Substitution is a literal token replacement inside each argv
element; any `${…}` referring to a name not in the map is a
config error and the hook is skipped with a loud log on that
tick. There is no shell expansion, no glob, no command
substitution, no nesting.

Config-load-time resolution would be wrong for everything except
maybe `${REPO}` (and even that only when the entry is scoped to a
single repo via `repo:`) — `${ISSUE}`, `${BRANCH}`, `${WORKTREE}`
are per-event by definition. Doing all substitution at fire time
keeps the rule uniform and easy to reason about.

### Hooks are observers, not gatekeepers

Hooks fire *after* the orchestrator has already taken its action
(the label is already flipped, the comment already posted) — they
cannot veto a transition. `fail: block` does not roll the
transition back; it routes the affected issue to `awaiting_human`
so the *next* tick parks rather than acts. `fail: warn` logs and
continues. `fail: ignore` is fire-and-forget.

If a hook needs to gate the action itself ("don't open the PR if
X", "refuse to auto-merge if Y"), that is a workflow change, not a
hook, and belongs in `workflow.py` proper. Pre-action veto is
deliberately out of scope: it would need a synchronous rollback
path through every handler, which is the kind of complexity the
state machine is designed to keep out.

### Trust boundary

The hook script runs as the orchestrator user. That user holds the
GitHub PAT (`~/.config/<owner>/<repo>/token`), the host's ssh keys,
and any other credentials present on the box. None of those are
the implementer agent's. Loading the hook process with anything
agent-modifiable would silently dissolve that boundary: the agent
owns its worktree and can replace files there at will, so a hook
script at a worktree-relative path is the agent's code running as
the orchestrator user.

The design therefore requires:

1. **Absolute paths only.** `exec[0]` must be an absolute path.
   Relative paths are rejected at hooks-file load.
2. **Path must live outside every worktree.** The orchestrator
   `realpath()`s `exec[0]` and refuses to run it if the resolved
   path is under any configured `WORKTREES_DIR` or any
   `RepoSpec.target_root`. The same check runs again at fire time,
   because a symlink target can be replaced between load and fire.
3. **Explicit-allowlist environment.** The hook inherits no env
   from the orchestrator. The hook process sees exactly the
   injected variables (`REPO`, `ISSUE`, `STAGE`, `EVENT`,
   `PR_NUMBER`, `BRANCH`, `WORKTREE`, `AGENT_BACKEND`) plus a
   fixed minimal `PATH` (e.g. `/usr/local/sbin:/usr/local/bin:
   /usr/sbin:/usr/bin:/sbin:/bin`). `HOME`, `GITHUB_TOKEN`,
   provider keys, `SSH_AUTH_SOCK`, `XDG_RUNTIME_DIR`, the
   orchestrator's venv vars — all stripped. Hooks that need their
   own credentials read them from absolute paths the hook script
   knows about; the orchestrator does not forward them.
4. **No shell, no argv interpolation by the orchestrator.** `exec`
   is passed straight to `subprocess.run(..., shell=False)`. The
   only substitution is the fire-time `${VAR}` placeholder
   replacement described under "Filtering and placeholder
   substitution" above — a fixed map, literal token replacement,
   unknown names rejected. There is no general environment
   expansion, no glob, no command substitution.
5. **`cwd` is the orchestrator's cwd**, not the worktree.
   `WORKTREE` is passed as an env var so hooks that genuinely need
   to read the diff can `cd` there explicitly; the choice to
   touch agent-controlled files is then visible in the hook
   source, not implicit.

### Why this is still risky

Even with the constraints above, hooks are an arbitrary-code
execution surface owned by the operator: if `/opt/orchestrator-
hooks/notify-slack.sh` itself is compromised, all of the above
becomes irrelevant. That is fine when the orchestrator runs on a
single host the operator trusts. It is *not* fine on a multi-tenant
deployment. The first version should ship gated:
`ORCHESTRATOR_HOOKS_ENABLED=on` required, off by default,
documented as "single-tenant only" until isolation lands (see
roadmap "Dockerfile / systemd / GitHub App migration").

### Out of scope (v1)

- No hook chaining / pipelines.
- No LLM-prompt hooks (Claude Code's hooks system supports those;
  the orchestrator does not have a model client in-process, only
  subprocess CLIs).
- No `on: before_*` hooks. Pre-transition hooks would have to be
  able to veto, which complicates the state machine considerably;
  defer.

### Acceptance

- Hooks file absent → no behavior change.
- Hooks file with one `stage_enter: validating` entry → the script
  runs exactly once when an issue enters `validating`.
- Hook script timing out / exiting non-zero → behavior matches
  `fail:` field (warn / block / ignore).
- Tests: a hooks-file parser test, plus an end-to-end test that
  uses a fake script and checks side effects.

Security-relevant acceptance:

- `exec[0]` with a relative path → hooks-file load fails with a
  clear error; the whole file is rejected (one bad entry does not
  silently disable just itself).
- `exec[0]` whose `realpath` resolves to anywhere under any
  configured `WORKTREES_DIR` or `RepoSpec.target_root` → load
  failure, plus a fire-time re-check that aborts the hook (so a
  symlink target swapped after load is also caught).
- Hook process env (captured via a test hook that dumps `os.environ`
  to a file) contains exactly the documented injected variables
  plus the fixed `PATH`; in particular, `HOME`, `GITHUB_TOKEN`,
  `SSH_AUTH_SOCK`, `XDG_RUNTIME_DIR`, and the orchestrator's
  venv-marker vars are absent.
- An `exec` argv element containing `${ISSUE}` fires the hook with
  the *event-time* issue number, not a value frozen at load. A
  second hook entry with `${UNKNOWN_THING}` is a config error
  caught at hooks-file load.
- A `when:` map with an unknown key (`foo_equals: bar`) or unknown
  operator (`branch_unsupported_op: …`) fails hooks-file load with
  a clear error.

### Inspiration

Claude Code's hooks system (shell commands / LLM prompts / HTTP
calls firing before/after tool use, conditional on permission-rule
syntax). This proposal copies the surface but only the
shell-command flavor and only post-transition, because that is what
fits a polling loop.

## Proposal 3: Per-repo lessons file injected into the dev prompt

### Problem

`CLAUDE.md` / `AGENTS.md` already document conventions for a target
repo, and `claude` auto-loads them. But the file is human-curated
and slow-moving. Two failure modes recur:

1. Reviewer flags the same anti-pattern on issue N+3 that it flagged
   on issue N — the dev agent has no memory of past review feedback
   across issues.
2. A target repo without a `CLAUDE.md` gets no repo-specific context
   at all; the dev agent goes in cold every time.

### Sketch

A persistent "lessons" file per repo, lives outside the worktree
(suggested path: `~/.config/<owner>/<repo>/lessons.md`, alongside
the token file). On every implementer spawn, the orchestrator reads
`lessons.md` and prepends its contents to the implementer prompt
under a `## Lessons from prior issues in this repo` section.

The file is operator-curated to start. **No auto-write in v1** —
the "orchestrator harvests patterns from reviewer feedback" idea
(closer to Claude Code's KAIROS auto-dreaming) is interesting but
speculative and big; punt it to a v2 once we know whether the
manual-curation version is even useful.

### Why not just use `CLAUDE.md` / `AGENTS.md`?

Both backends already auto-load the conventional files
(`claude` reads `CLAUDE.md`, `codex` reads `AGENTS.md`; either
shows up in the prompt when run inside the worktree). So this
proposal is *not* a backend-compat patch. Two reasons it is still
worth a separate channel:

1. `CLAUDE.md` / `AGENTS.md` live in the target repo, owned by
   the target repo's team. The orchestrator operator may not own
   that repo, may not have merge rights there, and even when they
   do the update cadence and authorship are different
   (reviewer-feedback-driven vs convention-driven). An
   out-of-tree, operator-owned file at
   `~/.config/<owner>/<repo>/lessons.md` does not need a target-repo
   PR to iterate.
2. Lessons are advisory ("avoid `time.sleep` in tests — flaky"),
   `CLAUDE.md` / `AGENTS.md` is normative ("license header on
   every file"). Mixing them dilutes both.

### Out of scope (v1)

- No auto-write. Manual edits only.
- No structured schema. Plain markdown injected verbatim.
- No size cap beyond a sanity check (refuse to inject if > 50KB).

### Acceptance

- File missing → no behavior change.
- File present → its contents appear in the implementer prompt
  under the documented heading.
- The reviewer prompt is **not** modified; review independence
  matters.
- Test in `tests/test_workflow.py` asserts that an injected
  lessons file shows up in the prompt passed to `agents.run_agent`.

### Inspiration

Claude Code's four-type memory system (user / feedback / project /
reference). This proposal takes only the "project" slice — the
narrowest, simplest, and least speculative.

## Rejected ideas

For transparency, here is what was considered and dropped, with a
one-line reason per item.

- **PLAN stage (ULTRAPLAN-style)** — duplicates the existing
  `decomposing` stage; the decomposer already produces a manifest
  and the `awaiting_human` mechanic already lets the agent surface
  a plan for HITL approval.
- **Sampled startup profiler / debug flags** — the orchestrator's
  startup is a `git fetch` and an env load; there is no cold path
  to profile.
- **Multi-strategy context compaction** — the orchestrator does
  not own the agent's context window; both `codex` and `claude` do
  their own compaction.
- **Terminal renderer / Ink / Yoga** — no UI surface.
- **Prompt cache stability via alphabetic tool sort** — the
  orchestrator does not call the Anthropic API directly.
- **Speculation state / overlay filesystem** — no REPL, no typing
  latency to hide.
- **Five-mode permission system / dangerous-pattern detection** —
  the host is the sandbox boundary by design; adding an
  orchestrator-level permission layer over CLIs that already run
  with `--dangerously-skip-permissions` / `--dangerously-bypass-…`
  would be defense in depth in the wrong place.
- **Deferred tool discovery / `ToolSearch`** — the orchestrator
  exposes no tools to the agent; everything is prompt-driven.
- **Coordinator-with-synthesis discipline** — already largely in
  effect: the decomposer manifest schema forces concrete child
  issues with titles and bodies, not paraphrases. Sharpening the
  prompt to ask for file paths and line numbers is a one-line
  improvement, not an issue's worth of work.
- **Worktree isolation for subagents** — already shipped
  (per-issue worktrees under `WORKTREES_DIR/<owner>__<name>/issue-N`).
- **Conventional Commits enforcement** — already shipped
  (`_pr_title_from_commit_or_issue`, implementer prompt).
- **Three-tier recovery / exponential backoff for API errors** —
  PyGithub handles GitHub-side retries; the orchestrator already
  has agent-level retry/round/budget caps.
- **KAIROS auto-dreaming (memory consolidation as a background
  agent)** — depends on the lessons file existing first; revisit
  after proposal 3 ships and proves out manual curation.
