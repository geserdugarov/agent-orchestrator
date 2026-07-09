# Security checklist and operator-owned controls

This page maps the project security checklist to the `agent-orchestrator` repo: what the repo files already enforce and
what is **operator-owned** (GitHub or org settings that no file in the repo can set).

The orchestrator gives `codex` / `claude` CLI subprocesses sandbox-bypass flags on the host, so the host is the real
trust boundary — see [`architecture.md`](architecture.md#design-constraints).

## Checklist mapping

- **Required human reviews for dependency changes** — operator-owned. Branch protection + `CODEOWNERS`. See
  [Required human reviews for dependency-touching changes](#required-human-reviews-for-dependency-touching-changes).
- **Automated dependency vulnerability scan** — in repo + operator-owned to enforce.
  [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) runs on every PR. See
  [Required checks](#required-checks).
- **2FA for all maintainers** — operator-owned. See [2FA](#2fa).
- **Secret scanning + push protection** — operator-owned. See
  [Secret scanning and push protection](#secret-scanning-and-push-protection).
- **`main` (and any release branch) protected, no force-push** — operator-owned. See
  [Branch protection](#branch-protection).
- **Required status checks** — operator-owned. See [Required checks](#required-checks).
- **Fork PRs cannot read repository secrets** — in repo + operator-owned. Workflows declare
  `permissions: contents: read` and reference no secrets
  ([`configuration.md#continuous-integration`](configuration.md#continuous-integration)). See
  [Fork-PR secret policy](#fork-pr-secret-policy).
- **No CI publishing / deploys unless run on a protected ref** — N/A today, policy below. No publishing workflow
  exists yet; see [No CI publishing / deploys outside protected refs](#no-ci-publishing--deploys-outside-protected-refs)
  before adding one.
- **Backup / restore drills** — operator-owned. See [Backup and restore drills](#backup-and-restore-drills).
- **Review / tests / scans for AI-generated code** — in repo. See
  [AI-generated code review, tests, and scans](#ai-generated-code-review-tests-and-scans).
- **Package-registry hygiene (lockfiles, registry pinning)** — in repo. Runtime deps (`PyGithub`, `psycopg[binary]`)
  are declared in [`../pyproject.toml`](../pyproject.toml); exact versions are pinned in [`../uv.lock`](../uv.lock); CI
  installs via `uv sync --locked`
  ([`configuration.md#continuous-integration`](configuration.md#continuous-integration)). Dependabot covers `uv` and
  `github-actions` in [`../.github/dependabot.yml`](../.github/dependabot.yml).

## Operator-owned controls (GitHub / org settings)

The items below cannot be enforced by files inside this repo — an operator must configure them once on GitHub. Walk
this list when bootstrapping a fork, an org migration, or a new release branch.

### 2FA

- Require 2FA for every maintainer's GitHub account.
- For org-owned repos, enable **"Require two-factor authentication for everyone in your organization"** at
  `https://github.com/organizations/<org>/settings/security`. Members without 2FA are removed when this is turned on.
- Prefer hardware security keys (WebAuthn) or a TOTP app over SMS.

### Secret scanning and push protection

Enable both at `Settings → Code security`:

- **Secret scanning** — alerts on tokens found in the repo's history.
- **Push protection** — blocks pushes that introduce a detected secret pattern. The orchestrator never reads
  `GITHUB_TOKEN` from `.env` ([`.env.example`](../.env.example)), so push protection is defense-in-depth against an
  accidental paste.

On org-owned repos, set the same defaults at the org level.

### Branch protection

Add a branch-protection rule for `main` (and any release branch) at `Settings → Branches`:

- **Require a pull request before merging.** The orchestrator only ever opens PRs; humans click Merge
  ([`architecture.md`](architecture.md)).
- **Require status checks to pass before merging** — list the checks in [Required checks](#required-checks).
- **Require branches to be up to date before merging** — keeps the per-tick base-sync auto rebase +
  [`resolving_conflict`](state-machine.md#_handle_resolving_conflict-label-resolving_conflict) (for actual rebase
  conflicts) flow honest.
- **Do not allow force pushes.**
- **Do not allow deletions.**
- **Restrict who can push** to `main`. The restriction applies to every protected-branch update including PR merges (see
  [GitHub docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)).
  The allowlist is the small named set of maintainers permitted to merge or push break-glass fixes. The orchestrator's
  personal access token does **not** belong here — granting it direct-push access would only widen blast radius if the
  token leaked.

### Required human reviews for dependency-touching changes

A PR that adds, removes, or pins a dependency — or that edits a workflow file pulling actions — should not merge on
green CI alone. The automated [Dependency Review scan](#required-checks) flags known-vulnerable versions; a human
reviewer covers license, maintainership, and supply-chain judgment calls the scanner cannot.

Two GitHub-side controls combine to enforce this:

1. **Branch protection — "Require approvals" ≥ 1** in the `main` branch-protection rule.
2. **`CODEOWNERS` for the dependency surface.** Add `.github/CODEOWNERS` listing the dependency-touching paths against
   the maintainer set, then enable **"Require review from Code Owners"** in the same rule. Recommended pattern set:

   ```
   /pyproject.toml          @<maintainer-handle>
   /uv.lock                 @<maintainer-handle>
   /.github/dependabot.yml  @<maintainer-handle>
   /.github/workflows/      @<maintainer-handle>
   ```

   Replace `@<maintainer-handle>` with the GitHub login(s) or team slug that should sign off. The right reviewer set
   varies by deployment (solo maintainer vs. team vs. org), so the orchestrator does not create or maintain this file.

### Required checks

Mark these checks **required** in the branch-protection rule (job names as they appear on the PR):

- `ci` from [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) — `ruff check` + `pytest` on Python 3.12,
  installed from [`../uv.lock`](../uv.lock).
- `dependency-review` from [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml)
  — fails when a PR introduces a vulnerable or non-compliant dep.

Both workflows run on `pull_request` and declare `permissions: contents: read`, so the `GITHUB_TOKEN` minted for each
run is read-only.

### Fork-PR secret policy

- Workflows already use no secrets and request only `contents: read`. Do **not** add `pull_request_target` triggers,
  `secrets.*` references, or higher token permissions without a written justification.
- At `Settings → Actions → General → Fork pull request workflows from outside collaborators`, set **"Require
  approval for first-time contributors who are new to GitHub"** (or stricter).
- For org-owned repos, mirror this default at the org level.

### No CI publishing / deploys outside protected refs

No publishing or deploy workflow exists in [`../.github/workflows/`](../.github/workflows/) today. If one is added:

- Run it only on `push` to `main` (a protected branch) or on pushes of tags covered by a **protected tag ruleset**
  (`Settings → Rules → Rulesets → New tag ruleset`). Never on `pull_request` or `pull_request_target`, and never
  on an unprotected tag pattern. Without a protected tag ruleset, drop the tag trigger and publish from `push` to `main`
  only.
- The protected tag ruleset must restrict tag creation / update / deletion to the same named maintainer set as the
  `main` rule, so an attacker who lands a benign PR cannot then push a release tag to trigger the deploy.
- Put the credentials behind a GitHub **environment** with required reviewers, and scope the environment to the
  protected branch / tag patterns (`Settings → Environments → Deployment branches and tags`) so secrets cannot be
  read from any other ref.
- Keep `permissions:` minimal — only the scopes the job actually needs (`id-token: write` for OIDC, `contents: read`
  for checkout, etc.).
- Do not call `actions/upload-artifact` with sensitive content from a fork-PR-triggered job.

### Backup and restore drills

GitHub holds the durable state:

- **Code and history** — the git repository on github.com.
- **Per-issue workflow state** — the workflow label + pinned `<!--orchestrator-state ...-->` JSON comment on each
  Issue (schema in [`state-machine.md`](state-machine.md#per-tick-flow-workflowtick)).

The orchestrator process is stateless; restoring an Issue restores progress.

Operator drill checklist (run at least once after setup, then on a recurring cadence):

1. Confirm a current clone of the repo exists off the orchestrator host, tracking `main`.
2. Export open / recently-closed Issues via the GitHub API (`gh issue list --state all --json …`) off-host. The
   pinned-state JSON comment is part of the export.
3. Verify that re-cloning the repo and re-running `./run.sh` against a fresh `WORKTREES_DIR` recovers in-flight Issues
   from their labels + pinned comments — the documented restart contract
   ([`configuration.md#what-survives-a-restart`](configuration.md#what-survives-a-restart)).
4. Confirm `~/.config/<owner>/<repo>/token` (or whatever `ORCHESTRATOR_TOKEN_FILE` points at) is backed up out-of-band;
   the personal access token is not stored in the repo and not recoverable from a code restore alone.

Worktrees under `WORKTREES_DIR` are cache, not state — losing them only forces the next tick to re-create the worktree
from `origin/<base>`.

### AI-generated code review, tests, and scans

Every PR opened by the orchestrator is AI-generated, so the policy is the workflow's normal path, not an extra step:

- **Independent reviewer agent.** The `validating` stage spawns a fresh reviewer against `git diff origin/<base>...HEAD`
  ([`state-machine.md#_handle_validating-label-validating`](state-machine.md#_handle_validating-label-validating)). It
  uses a different agent role from the implementer (`REVIEW_AGENT` vs. `DEV_AGENT`) and starts with no shared session
  state.
- **Local verify gate.** When the reviewer says `APPROVED`, the orchestrator runs `VERIFY_COMMANDS` in the per-issue
  worktree before relabeling to `documenting`
  ([`configuration.md#local-verification-gate`](configuration.md#local-verification-gate)). Set
  `VERIFY_COMMANDS=python3 -m pytest -q;ruff check .` (or your project equivalent) so an AI-produced regression is
  caught locally before the PR is advertised to humans for merge.
- **CI on every PR.** [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) re-runs lint + tests;
  [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) blocks vulnerable /
  non-compliant deps. Mark both **required** in branch protection (see [Required checks](#required-checks)).
- **Human merge by default.** The orchestrator is permanently manual-merge-only — it pings HITL handles when a PR is
  mergeable but never calls `gh.merge_pr`. A human clicks Merge on every PR that lands.
- **Sandboxing reminder.** Agents are spawned with sandbox-bypass flags; the host (or container / VM) is the real trust
  boundary. Agent env is stripped of GitHub tokens, production-secret-shaped vars, and credential-file locators, but a
  hostile dependency executed inside a verify command still runs as the orchestrator's OS user. Keep the orchestrator on
  its own host or in a dedicated VM / container; do not co-locate it with other workloads' secrets on the same user
  account.

## Comment trust boundary (`ALLOWED_ISSUE_AUTHORS`)

The orchestrator feeds issue- and PR-thread comments to coding agents as workflow-driving instructions. On a public
repo that is a prompt-injection surface: any account can post a comment that steers an agent, resumes a parked session,
or re-triggers work. `ALLOWED_ISSUE_AUTHORS` is the operator's control. It defaults unset; setting it to the maintainer
logins turns the pickup allowlist into a comment trust boundary enforced by the shared `comment_trust` helpers
(`is_trusted_author` / `filter_trusted`). The env-var reference is in
[`configuration.md#agent-roles`](configuration.md#agent-roles); the full per-surface filter list is in
[`state-machine.md#user-content-drift-detection`](state-machine.md#user-content-drift-detection).

The security posture:

- **Opt-in, legacy-safe by default.** Unset (the default) trusts every author, preserving the single-user behavior a
  private-repo deployment expects. The boundary exists only once an operator lists the trusted logins, so enabling it is
  a deliberate act, not a silent behavior change.
- **Visible, not deleted.** An untrusted comment stays on the GitHub thread for humans to read; the orchestrator never
  hides, edits, or deletes it. What changes is only its *use as workflow input* — it is omitted from agent
  prompts, the `user_content_hash` drift signal, every awaiting-human resume signal (including the base-sync
  auto-rebase retry-unpark and the `/orchestrator add-review-rounds` review-cap command), and the `in_review` /
  `fixing` PR-feedback loop. So an outsider on a public repo cannot inject instructions into an agent, resume a
  parked session, retry a parked rebase, route `in_review` to `fixing`, or shift the drift hash, while the audit
  trail of what they said stays intact.
- **Filtering is fail-safe.** A comment whose author failed to load (empty login) is untrusted. On the awaiting-human
  resume paths (and the auto-rebase retry-unpark) the filter runs on the whole comment batch up front, so an untrusted
  comment there never advances the consumed-watermark nor is marked read — it is re-filtered on each later tick
  rather than silently absorbed as a new baseline. The `in_review` drift path instead excludes untrusted
  PR-conversation comments from the drift prompt but still advances its watermarks past them, so a later tick does
  not re-scan them as fresh feedback.
- **Third-party Bot/App handling is deliberate.** Two distinct mechanisms apply. The `user_content_hash` drift hash and
  the community-contribution PR sweep exclude Bot / GitHub-App accounts (Dependabot, Renovate, CI bots) structurally via
  GitHub's `user.type == "Bot"` flag, independent of the allowlist. The comment trust boundary itself does not: on the
  prompt / resume / PR-feedback surfaces a bot is gated like any other author — trusted while the allowlist is empty
  (legacy behavior), and under a populated allowlist trusted only when its own login is explicitly listed. So an
  intentionally allowlisted automation account still works; an unlisted one does not.
- **Scope is comment content, not capability.** This boundary keeps untrusted *words* out of agent prompts and workflow
  signals; it is not a sandbox. Agents still run as the orchestrator's OS user with sandbox bypass, so the host remains
  the real trust boundary (see [above](#ai-generated-code-review-tests-and-scans) and
  [`architecture.md#design-constraints`](architecture.md#design-constraints)).

## Pinned-state authentication

The workflow's durable state — the `<!--orchestrator-state ...-->` JSON comment — is authenticated separately from the
`ALLOWED_ISSUE_AUTHORS` boundary above. That allowlist decides which comments are *workflow input*; it does **not**
decide which comment holds *authoritative state*. `read_pinned_state` trusts a comment as state only when **both** hold:
it is authored by the account backing the orchestrator's token (resolved once from `GET /user` and threaded into
worker-thread clients), **and** its entire body is the state marker — exactly what `write_pinned_state` emits.

- **Author, not marker presence.** Any account can post — or edit an older comment to carry — the hidden state marker.
  Trusting the first marker by document order would let an outsider preempt the real pinned state and steer agent
  session fields, branch / PR selection, and terminal branch cleanup (CWE-345). A foreign author's marker is skipped
  before its body is parsed, so it cannot even shadow state with malformed JSON.
- **State-only body, not embedded substring.** The author check alone is not enough: the orchestrator posts ordinary
  comments (e.g. decomposer rationale via `_post_issue_comment`) whose text is attacker-influenced, and does so before
  the real state comment exists on a manually-labeled issue. Such a comment that merely embeds a marker in prose is not
  state-only, so it is never mistaken for state — only a comment that is *nothing but* the marker qualifies.
- **Legacy-safe, no migration.** Existing pinned comments were written by this same account and are state-only by
  construction, so both checks keep honoring them; state writes keep targeting the trusted comment id once found.
- **Independent of the comment boundary.** This authenticates *which comment is state*; `ALLOWED_ISSUE_AUTHORS`
  authenticates *which comments are input*. Both are enforced independently, and the state boundary applies even when
  the allowlist is unset.

## Cross-repo awareness disclosure (`EXPOSE_TRACKED_REPOS`)

When more than one repo is configured (`REPOS`) and `EXPOSE_TRACKED_REPOS` is on (the default), working-agent prompts
carry a compact block naming the *other* tracked repos — each repo's slug, its local `target_root` checkout, and its
base branch — so an agent reasoning about a sibling repo knows it is monitored and where its source lives. The env-var
reference is in [`configuration.md#agent-roles`](configuration.md#agent-roles); the prompt content is in
[`workflow.md`](workflow.md#tracked-repos-awareness-in-working-agent-prompts).
The security posture:

- **Disclosure, not escalation.** Agents already run as the orchestrator's own OS user with sandbox bypass, so the host
  is the trust boundary (above, and [`architecture.md#design-constraints`](architecture.md#design-constraints)). Every
  other repo's `target_root` is already on that host and already readable by the agent — it could enumerate the
  checkouts by walking the filesystem today. Naming the paths is information disclosure of data the agent could already
  obtain; it grants no new capability.
- **No secrets in the block.** The block carries only slugs, base branches, and the `target_root` paths the operator
  themselves wrote into `REPOS`. No tokens, no `ORCHESTRATOR_TOKEN_FILE`, no provider keys, no remote URLs — there is
  nothing secret-shaped to redact because nothing secret is included by construction.
- **Write-containment is unchanged.** The orchestrator pushes only the *current* issue's branch from the current
  worktree, via an explicit `HEAD:refs/heads/<branch>` refspec under the hardened git envelope
  ([`architecture.md#push-path`](architecture.md#push-path-workflow_push_branch)). If a misled agent edits a sibling
  checkout, nothing the orchestrator does publishes it — it surfaces as a dirty foreign tree, never as a PR. The
  block's framing also states every listed path is read-only.
- **Prompt-injection blast radius.** Untrusted issue / comment text could now point an agent at a named sibling path,
  but (a) the path was already discoverable and (b) exfiltration still needs an egress channel, and
  `agents._filter_agent_env` already strips the GitHub token, secret-shaped vars, and credential / write-credential
  locators, leaving the agent only its own model-provider auth. The net-new exposure is a *map*, not a new *door*.
- **Local paths in GitHub.** An agent could quote a `target_root` into a PR body or park comment. Paths are not secret,
  but an operator who treats them as sensitive flips `EXPOSE_TRACKED_REPOS=off` to suppress the disclosure globally.

The feature defaults on but is **inert for single-repo hosts** — the block is emitted only when more than one repo is
configured — so a default deployment discloses nothing. `EXPOSE_TRACKED_REPOS=off` is the operator kill switch and
reverts to today's behavior with zero added prompt content.
