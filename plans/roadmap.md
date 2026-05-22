# Agent Orchestrator — Roadmap

## Status as of 2026-05-21

The full label lifecycle (no label → `decomposing` → `ready` / `blocked` /
`umbrella` → `implementing` → `validating` → `in_review` → `resolving_conflict`
optional detour → `done` / `rejected`) is wired end-to-end. The orchestrator
runs as a single long-lived Python process (`python -m orchestrator.main`,
wrapped by `run.sh` for self-restart), polls one or more configured repos,
and delegates the actual coding to `codex` / `claude` CLI subprocesses
running in per-issue git worktrees. State lives in GitHub Issues themselves
(one workflow label plus one pinned JSON comment), so the loop stays
stateless and progress is observable on github.com.

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
from JSONL events for resumes. `DEV_AGENT` / `REVIEW_AGENT` /
`DECOMPOSE_AGENT` are independently configurable shell-like command specs
parsed by `config._parse_agent_spec`: the first token names the backend
(`codex` / `claude`, mapped to `CODEX_BIN` / `CLAUDE_BIN`) and any
remaining tokens are forwarded verbatim as backend-CLI args on every
spawn — so roles like "implement with codex at xhigh reasoning"
(`DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`),
"review with claude opus at high effort"
(`REVIEW_AGENT=claude --model claude-opus-4-7 --effort high`), or
"review with codex at high reasoning"
(`REVIEW_AGENT=codex -m gpt-5.5-codex -c 'model_reasoning_effort="high"'`)
all stay declarative in env. The full spec (backend + args) is persisted to
pinned state and re-parsed on every resume, so in-flight issues keep
using the pinned spec until the session ends and an env-var flip cannot
migrate live work. `AGENT_TIMEOUT` / `REVIEW_TIMEOUT` wall-clock caps
with grandchild reaper; `MAX_RETRIES_PER_DAY` per-issue fresh-spawn
budget over 24h.

**Security hardening.** `agents._agent_env` strips all GitHub tokens from
the agent environment; PAT is rejected if found in `REPO_ROOT/.env` and
must come from the process env or a file outside `REPO_ROOT` (default
`~/.config/<owner>/<repo>/token`). Hardened `git push` via `GIT_ASKPASS`
tempfile, neutered git-config envelope (hooks / credential / fsmonitor /
global / system disabled), refuses `insteadOf` rewrites, and pushes via
explicit refspec. Agent commit identity stamped via `AGENT_GIT_NAME` /
`AGENT_GIT_EMAIL`.

**Decomposing stage.** `_handle_decomposing` runs `DECOMPOSE_AGENT` and
parses a fenced ` ```orchestrator-manifest ` JSON block: `decision=single`
flips parent to `ready`; `decision=split` creates up to 10 children with
shape / dependency / cycle validation, routing the parent to `blocked` or
`umbrella` depending on the flag. `_handle_blocked` walks the dep graph
each tick to unblock middle children; umbrella parents close to `done`
once all children resolve. Children link via `Parent: #<n>` (never
`Resolves`). `DECOMPOSE=off` reverts to direct-to-`implementing`;
`ALLOWED_ISSUE_AUTHORS` gates pickup.

**Implementing stage.** `_handle_implementing` ensures a per-issue
worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` from
`origin/<base>`. New commits + clean tree → push, open / reuse PR, flip
to `validating`; dirty tree or no commits → park. Awaiting-human replies
resume the dev session on its locked spec (backend + args, re-parsed
from `dev_agent`). PR titles and commits follow
Conventional Commits, reusing the agent's first commit subject when
conformant.

**Validating stage.** `_handle_validating` spawns a fresh reviewer on
`git diff origin/<base>...HEAD` and parses the last `VERDICT:` marker.
`APPROVED` snapshots `agent_approved_sha`, optionally squashes
(`SQUASH_ON_APPROVAL`, default on, `--force-with-lease`), and flips to
`in_review`. `CHANGES_REQUESTED` posts feedback, resumes the dev, and
increments `review_round`; `MAX_REVIEW_ROUNDS` (default 3) caps
iterations. Silent reviewer crashes are tagged transient for retry.

**In-review terminals and auto-merge.** `_handle_in_review` covers:
PR merged → `done` + branch cleanup; PR closed unmerged → `rejected`;
new comments past `IN_REVIEW_DEBOUNCE_SECONDS` (default 600s) → resume
dev, bounce to `validating`; `AUTO_MERGE=on` + agent-or-human approval +
no veto + mergeable + green CI → SHA-pinned `gh.merge_pr` → `done`.
Three independent watermarks separate IssueComment / PullRequestComment /
PullRequestReview namespaces; park comments bump watermarks past
themselves to avoid replay.

**Conflict resolution stage.** Under `AUTO_MERGE=on`, approved-but-
unmergeable PRs route to `resolving_conflict` instead of parking.
`_handle_resolving_conflict` fetches base via `_authed_fetch`, runs
`git merge --no-edit` under `_git_hardened`, and flips back to
`validating` on clean merge (or no-op already-up-to-date). Real conflicts
resume the dev session with a prompt naming up to 20 conflicted paths.
`MAX_CONFLICT_ROUNDS` (default 3) caps attempts. Merge over rebase
preserves the stored `agent_approved_sha`.

**Multi-repo support.** `RepoSpec(slug, target_root, base_branch)` is
threaded through every handler. `REPOS` env
(`owner/name|target_root|base_branch`, `;`- or newline-separated) drives
fan-out; legacy single-repo mode applies when `REPOS` is unset.
Validation at import aborts on malformed entries, bad slugs, or
duplicates. Worktrees namespaced by slug. Each tick iterates every
`(spec, GitHubClient)` with per-repo exception isolation. Per-slug token
resolution; `ORCHESTRATOR_BASE_BRANCH` decoupled from `BASE_BRANCH`;
`TARGET_REPO_ROOT` decouples orchestrator checkout from target clones.

**Tests.** `tests/test_workflow.py` covers every stage handler, the
manifest parser, watermark / debounce logic, the auto-merge gate,
squash-on-approval, the resolving-conflict suite, the umbrella handler,
the multi-repo dispatcher, and park-comment-replay prevention.
`tests/fakes.py` exposes in-memory `FakeGitHubClient` / `FakePR` /
`FakePRRef` / `FakeIssue`. `tests/test_config.py`, `tests/test_agents.py`,
and `tests/test_main.py` cover their respective modules.

**Project CI.** GitHub Actions workflow runs `ruff` and `pytest` on PRs;
the auto-merge gate consults `pr_combined_check_state` so project-level
checks must pass before merge.

## Future work

- **Spec-first split / separate test writer.** Add a `specifying` stage
  between `ready` and `implementing` so an independent spec agent writes
  failing tests before production work starts:
  `ready → specifying → implementing → validating → …`. The spec agent
  is allowed to edit only test paths, and the orchestrator must verify
  the new tests fail against `origin/<base>` before letting an
  implementer run. The implementer prompt should carry the generated
  test-file allowlist plus an explicit rule forbidding edits under
  `tests/**`; after the implementer exits, the orchestrator rejects and
  parks if `git diff --name-only HEAD origin/<base>` shows touched test
  files. Spec-agent inability to produce tests should park with a typed
  reason such as `ac-clarification`, `dep-missing`, or
  `design-question`, giving humans a clearer next action than a freeform
  park comment. Some issues cannot use this path, so extend the
  decomposer manifest with a backward-compatible `spec_skip: true`
  opt-out for docs, refactors, and other work that cannot be expressed
  as failing tests.
- **Repo memory carried across issues.** Add a small per-target-repo
  memory file at `<target_root>/.agent-orchestrator/repo-memory.json` so
  each issue does not start cold. Treat the file as orchestrator-owned
  context, not PR content; implementation should prevent it from leaking
  into agent commits or policy checks. Initial schema: `schema_version`,
  `verify_commands`, `touched_files_top`, and capped `common_failures`
  entries with summaries and timestamps. Update it from
  `_handle_in_review` merge terminals on a best-effort basis, never
  blocking a successful merge if the memory write fails. Read it into
  decomposer and implementer prompts with strict caps such as top 10
  touched files and top 5 failures, so agents get useful repository
  context without turning the prompt into a stale knowledge base. Keep
  the first version fixed-schema and file-backed; richer search,
  exemplars, or lesson mining can wait until the simple signal proves
  useful.
- **Project tests/linters during `validating`.** Run project-specific
  verification locally before the reviewer-approved branch can flip to
  `in_review`. Today `_handle_validating` only runs the reviewer agent;
  `ruff`, `pytest`, `mypy`, or target-repo scripts run externally in PR
  CI and are consulted later by the auto-merge gate. Add a configurable
  verify-command list and park with actionable output on failure, while
  keeping CI as the final merge gate.
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
