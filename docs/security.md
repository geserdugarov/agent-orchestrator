# Security checklist and operator-owned controls

This page maps the project security checklist to the `agent-orchestrator` repo: what the repo files already enforce, what is **operator-owned** (GitHub or org settings that no file in the repo can set), and what is **N/A** for this codebase.

The orchestrator gives `codex` / `claude` CLI subprocesses sandbox-bypass flags on the host, so the host is the real trust boundary — see [`architecture.md`](architecture.md#design-constraints). The controls below are layered on top of that assumption: repo-side checks catch issues before merge; GitHub-side controls catch issues that repo files cannot reach.

## Checklist mapping

| Control | Status | Where it lives |
| ------- | ------ | -------------- |
| Required human reviews for dependency changes | operator-owned | A PR that touches `pyproject.toml`, `uv.lock`, `.github/dependabot.yml`, or `.github/workflows/**` should require an approving review from a maintainer before it can merge. The enforcement is GitHub-side: branch protection's "require N approvals" plus a `CODEOWNERS` rule routing those paths to a named reviewer set. See [Required human reviews for dependency-touching changes](#required-human-reviews-for-dependency-touching-changes). |
| Automated dependency vulnerability scan | in repo + operator-owned for enforcement | [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) runs `actions/dependency-review-action` on every PR and fails on vulnerable / non-compliant deps. This is a **scan**, not a review — making the check block merge is a separate branch-protection setting. See [Required checks](#required-checks). |
| 2FA for all maintainers | operator-owned | GitHub account / org setting. Cannot be enforced from a repo file. See [2FA](#2fa). |
| Secret scanning + push protection | operator-owned | GitHub repo setting (`Settings → Code security`). See [Secret scanning and push protection](#secret-scanning-and-push-protection). |
| `main` (and any release branch) protected, no force-push | operator-owned | GitHub branch-protection rule on `main`. See [Branch protection](#branch-protection). |
| Required status checks | operator-owned | Branch-protection rule names the checks. The repo provides them: `CI` ([`../.github/workflows/ci.yml`](../.github/workflows/ci.yml)) and `Dependency Review` ([`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml)). See [Required checks](#required-checks). |
| Fork PRs cannot read repository secrets | in repo + operator-owned | Workflows declare `permissions: contents: read` at the top level and use no repo secrets, so the default-`GITHUB_TOKEN` minted for fork PRs is read-only and there are no secrets to leak (see [`configuration.md#continuous-integration`](configuration.md#continuous-integration)). Org-level "require approval for first-time contributors" is the matching GitHub-side belt-and-braces. See [Fork-PR secret policy](#fork-pr-secret-policy). |
| No CI publishing / deploys unless run on a protected ref | N/A today, policy below | The repo has no publishing or deploy workflow. If one is added, gate it on protected refs and `environments` with required reviewers. See [No CI publishing / deploys outside protected refs](#no-ci-publishing--deploys-outside-protected-refs). |
| Backup / restore drills | operator-owned | GitHub holds the durable state (code + per-issue workflow labels + pinned JSON comments). See [Backup and restore drills](#backup-and-restore-drills). |
| Review / tests / scans for AI-generated code | in repo | The orchestrator's own workflow already enforces this for every agent-produced PR — see [AI-generated code review, tests, and scans](#ai-generated-code-review-tests-and-scans). |
| npm / pnpm / package-registry hygiene (lockfiles, registry pinning, scoped tokens) | **N/A** | This is a Python repo. The only runtime dep (`PyGithub`) is declared in [`../pyproject.toml`](../pyproject.toml); exact versions live in [`../uv.lock`](../uv.lock); CI installs via `uv sync --locked` ([`configuration.md#continuous-integration`](configuration.md#continuous-integration)). There is no npm registry, no package publishing, and no JS package surface. |

## Operator-owned controls (GitHub / org settings)

The items below cannot be enforced by files inside this repo — an operator must configure them once on GitHub. Check this list when bootstrapping a fork, an org migration, or a new release branch.

### 2FA

- Require 2FA for every maintainer's GitHub account (`Settings → Password and authentication`).
- If the repo is owned by an organization, enable **"Require two-factor authentication for everyone in your organization"** at `https://github.com/organizations/<org>/settings/security`. Members without 2FA are removed when this is turned on.
- Prefer hardware security keys (WebAuthn) or a TOTP app over SMS.

### Secret scanning and push protection

Enable both at `Settings → Code security` on `https://github.com/geserdugarov/agent-orchestrator`:

- **Secret scanning** — GitHub alerts on tokens found in the repo's history.
- **Push protection** — blocks pushes that introduce a detected secret pattern before the commit lands on `main`. This is the most useful single switch: the orchestrator never reads `GITHUB_TOKEN` from `.env` ([`.env.example`](../.env.example)), but push protection is a defense in depth against an accidental paste.

These are repo-level settings on a public repo; on an org-owned repo they can also be set as defaults at the org level.

### Branch protection

Add a branch-protection rule for `main` (and any release branch) at `Settings → Branches`:

- **Require a pull request before merging** — the orchestrator only ever merges via PR (see [`architecture.md`](architecture.md)), so this matches the actual flow.
- **Require status checks to pass before merging** — list the checks named in [Required checks](#required-checks).
- **Require branches to be up to date before merging** — keeps the [`resolving_conflict`](architecture.md) detour honest.
- **Do not allow force pushes.** The orchestrator's own push path forbids force-pushes to `main` for safety; this is the GitHub-side enforcement that catches a human bypass too.
- **Do not allow deletions.**
- **Restrict who can push** to `main`. GitHub's push restriction applies to **every update of the protected branch — including PR merges**, not just direct `git push` (see [GitHub docs on protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)), so the right entry depends on who performs the merge:
  - **`AUTO_MERGE=off` (default).** A human clicks "Merge" on the PR, so the allowlist is the small named set of maintainers permitted to do break-glass fixes and to merge approved PRs. The orchestrator's PAT does **not** belong here — it never performs the merge in this mode, and granting it direct-push access would only widen blast radius if the PAT leaked.
  - **`AUTO_MERGE=on`.** The orchestrator's PAT identity is the merge actor (`_handle_in_review` calls `gh.merge_pr`, see [`configuration.md#auto-merge`](configuration.md#auto-merge)), so it **must** be on the allowlist — either by adding the PAT's GitHub account directly, or by listing the actor under "Allow specified actors to bypass required pull requests" in the same rule. Without this, `gh.merge_pr` fails with a 403 and the issue parks on `awaiting_human`. Keep the named-humans entries for manual merges and break-glass fixes alongside the PAT.

### Required human reviews for dependency-touching changes

A PR that adds, removes, or pins a dependency — or that edits a workflow file that pulls actions — should not merge on green CI alone; a maintainer must sign off. This is **separate from** the automated [Dependency Review scan](#required-checks): the scan flags known-vulnerable versions, the human review catches license, maintainership, and supply-chain judgment calls the scanner cannot.

Two GitHub-side controls combine to enforce this:

1. **Branch protection — "Require approvals" ≥ 1** in the `main` branch-protection rule. Combined with "Require a pull request before merging" (above), every merge to `main` then needs an approving review.
2. **`CODEOWNERS` for the dependency surface.** Add `.github/CODEOWNERS` listing the dependency-touching paths against the maintainer set, then enable **"Require review from Code Owners"** in the same branch-protection rule. The recommended pattern set for this repo is:

   ```
   /pyproject.toml          @<maintainer-handle>
   /uv.lock                 @<maintainer-handle>
   /.github/dependabot.yml  @<maintainer-handle>
   /.github/workflows/      @<maintainer-handle>
   ```

   Replace `@<maintainer-handle>` with the GitHub login(s) or team slug that should sign off. The `CODEOWNERS` file lives on `main`; GitHub then routes a review request to the listed owners automatically when a PR changes any of the matched paths, and branch protection blocks the merge until they approve.

The file is operator-owned because the right reviewer set varies by deployment (solo maintainer vs. team vs. org). The orchestrator does not create or maintain it.

### Required checks

Mark these checks **required** in the branch-protection rule (their job names as they appear on the PR):

- `ci` from [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) — `ruff check` + `pytest` on Python 3.12, installed from [`../uv.lock`](../uv.lock).
- `dependency-review` from [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) — fails when a PR introduces a vulnerable or non-compliant dep. This is the **automated scan**; the human-review requirement is configured separately, see [Required human reviews for dependency-touching changes](#required-human-reviews-for-dependency-touching-changes).

Both workflows run on `pull_request` and declare `permissions: contents: read` at the top level, so the `GITHUB_TOKEN` minted for each run is read-only and cannot publish artifacts, push tags, or comment on PRs.

### Fork-PR secret policy

- The repo's workflows already use no secrets and request only `contents: read`, so a fork-PR run has nothing to exfiltrate. Do **not** add `pull_request_target` triggers, `secrets.*` references, or higher token permissions to any workflow without a written justification.
- At `Settings → Actions → General → Fork pull request workflows from outside collaborators`, set **"Require approval for first-time contributors who are new to GitHub"** (or stricter). This is the org-side belt-and-braces against a hostile fork PR that tries to mutate workflow files.
- For org-owned repos, mirror the same default at the org level so future repos inherit it.

### No CI publishing / deploys outside protected refs

Today there are no publishing or deploy workflows in [`../.github/workflows/`](../.github/workflows/). If one is added later:

- Run it only on `push` to `main` (a protected branch) or on pushes of tags that are themselves covered by a **protected tag ruleset** (`Settings → Rules → Rulesets → New tag ruleset`). Never trigger publishing on `pull_request` or `pull_request_target`, and never on a tag pattern that any contributor can push — an unprotected tag is not a protected ref. If no protected tag ruleset exists, drop the tag trigger and publish from `push` to `main` only.
- The protected tag ruleset must restrict tag creation / update / deletion to the same named maintainer set as the `main` branch-protection rule, so an attacker who lands a benign PR cannot then push a release tag to trigger the deploy.
- Put the credentials the job needs behind a GitHub **environment** with required reviewers, so the deploy blocks until a human approves it. Repo-level `secrets.*` are too broad. Scope the environment to the protected branch / tag patterns above (`Settings → Environments → Deployment branches and tags → Selected branches and tags`) so secrets cannot be read from any other ref.
- Keep `permissions:` minimal — only the scopes the job actually needs (`id-token: write` for OIDC, `contents: read` for the checkout, etc.).
- Do not call `actions/upload-artifact` with sensitive content from a fork-PR-triggered job.

### Backup and restore drills

GitHub holds the durable state for this project:

- **Code and history** — the git repository on github.com.
- **Per-issue workflow state** — the workflow label + pinned `<!--orchestrator-state ...-->` JSON comment on each Issue (see [`architecture.md`](architecture.md)). The orchestrator process is stateless; restoring an Issue restores progress.

Operator drill checklist (run at least once after setup, then on a recurring cadence):

1. Confirm a current clone of the repo exists on a host other than the orchestrator's box, and that it tracks `main`.
2. Export the open / recently-closed Issues via the GitHub API (`gh issue list --state all --json …`) and store the JSON off-host. The pinned-state JSON comment is part of the issue body / comments and is included in that export.
3. Verify that you can re-clone the repo and re-run `./run.sh` against a fresh `WORKTREES_DIR` and recover the in-flight Issues from their labels + pinned comments alone — that is the documented restart contract (see [`configuration.md#what-survives-a-restart`](configuration.md#what-survives-a-restart)).
4. Confirm that `~/.config/<owner>/<repo>/token` (or whatever `ORCHESTRATOR_TOKEN_FILE` points at) is backed up out-of-band; the PAT is not stored in the repo and not recoverable from a code restore alone.

Worktrees under `WORKTREES_DIR` are cache, not state — losing them only forces the next tick to re-create the worktree from `origin/<base>`.

### AI-generated code review, tests, and scans

Every PR opened by the orchestrator is AI-generated, so the policy is the workflow's normal path, not an extra step:

- **Independent reviewer agent.** The `validating` stage spawns a fresh reviewer in its own session against `git diff origin/<base>...HEAD` ([`architecture.md`](architecture.md)). It is a different agent role from the implementer (`REVIEW_AGENT` vs. `DEV_AGENT`) and starts with no shared session state.
- **Local verify gate.** When the reviewer says `APPROVED`, the orchestrator runs `VERIFY_COMMANDS` in the per-issue worktree before relabeling to `in_review` ([`configuration.md#local-verification-gate`](configuration.md#local-verification-gate)). Set `VERIFY_COMMANDS=python3 -m pytest -q;ruff check .` (or your project equivalent) so an AI-produced regression is caught locally before the PR-side merge path even sees it.
- **CI on every PR.** [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) re-runs lint + tests on GitHub, and [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) blocks vulnerable / non-compliant deps. Both should be marked **required** in branch protection — see [Required checks](#required-checks).
- **Human merge by default.** `AUTO_MERGE` is `off` by default ([`configuration.md`](configuration.md)). Flip it on only after dogfooding the loop on the repo for long enough to trust the reviewer + verify gates.
- **Sandboxing reminder.** The agents are spawned with sandbox-bypass flags. The host (or container / VM) is the real trust boundary — agent env is stripped of GitHub tokens, production-secret-shaped vars, credential-file locators, and write-credential locators, but a hostile dependency executed inside a verify command still runs as the orchestrator's OS user. Keep the orchestrator on its own host or in a dedicated VM / container; do not co-locate it with other workloads' secrets on the same user account.

## Items deliberately marked N/A

- **npm / pnpm / package-registry items.** This is a Python project; there is no JavaScript package surface, no published npm package, and no Node lockfile. The Python equivalents are already in place: `PyGithub` is the only runtime dep in [`../pyproject.toml`](../pyproject.toml), [`../uv.lock`](../uv.lock) pins exact versions, and CI installs via `uv sync --locked` so the build is reproducible ([`configuration.md#continuous-integration`](configuration.md#continuous-integration)). Dependabot is configured for the `uv` and `github-actions` ecosystems in [`../.github/dependabot.yml`](../.github/dependabot.yml).
