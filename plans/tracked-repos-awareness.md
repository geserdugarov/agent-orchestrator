# Tracked-Repos Awareness for Working Agents

Design for issue #460 — *Agents should be aware about the list of tracked
multiple repos*. Design only; no code lands with this doc.

## Goal

Give every working agent a compact, **read-only** awareness of the other
repos the orchestrator tracks: each repo's slug, its local source
checkout path, and its base branch. So an agent implementing an issue in
`owner/lance` that needs to reason about `owner/ray` knows Ray is also
monitored and that its source is checked out locally at a known path.

Two hard constraints from the issue drive the whole design:

1. **Cheap.** Don't spend much context "managing the list". The list is
   tiny and already lives in config; the cost should be a few prompt
   lines, and ideally zero for the common single-repo deployment.
2. **No critical security issue.** The feature must not hand the agent a
   capability it doesn't already have, must not leak secrets, and must
   not let cross-repo work escape the orchestrator's push containment.

## Where the data already lives

`config.default_repo_specs()` returns a validated `list[RepoSpec]`
(`slug`, `target_root`, `base_branch`, `remote_name`, `parallel_limit`),
built once at import from `REPOS` (or a single synthesized entry from
`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` when `REPOS` is unset). The
per-repo `workflow.tick(gh, spec)` already carries the *current* repo's
`RepoSpec`. So the entire "list of tracked repos" is:

- the full list — one `config.default_repo_specs()` call, cached, no
  GitHub round-trip, no new state, and
- the current repo — the `spec` the tick already holds.

No new config, no new persistence, no new pinned-state field. The
worktree subsystem (`worktree_lifecycle.py`) already lays each repo's
worktrees under a sanitized-slug parent, so cross-repo paths are stable
and discoverable.

## Security analysis (the load-bearing part)

The issue explicitly asks not to "create some critical security issues".
The key realization is that **this feature discloses information the
agent can already obtain — it does not grant a new capability.**

Agents run as the orchestrator's own OS user with sandbox bypass
(`codex --dangerously-bypass-approvals-and-sandbox`,
`claude --dangerously-skip-permissions`); per `docs/security.md` and
`docs/architecture.md#design-constraints` the **host is the trust
boundary**. Every other repo's `target_root` is already on that host and
already readable by the agent — it could enumerate them by walking the
filesystem today. Telling it the paths is disclosure, not escalation.

That framing lets us enumerate the *actual* net-new risks and show each
is contained:

| Risk | Mitigation |
|---|---|
| **No secrets in the list.** | The block carries only slugs, base branches, and `target_root` paths the operator themselves wrote into `REPOS`. No tokens, no `ORCHESTRATOR_TOKEN_FILE`, no provider keys, no remote URLs. There is nothing for `_redact_secrets` to catch because nothing secret-shaped is included by construction. |
| **Cross-repo writes can't be published.** | The orchestrator pushes only the *current* issue's branch from the current worktree, via an explicit refspec `HEAD:refs/heads/<branch>` under the hardened git envelope (`docs/architecture.md#push-path`). If a misled agent edits `../ray`'s checkout, nothing the orchestrator does publishes it; it surfaces as a dirty foreign tree, never as a PR. The prompt also states other checkouts are reference-only. |
| **Scope creep / prompt-injection blast radius.** | Untrusted issue/comment text could now point the agent at a named sibling path. But (a) the path was already discoverable, and (b) exfiltration still needs an egress channel, and `_filter_agent_env` already strips the GitHub token, secret-shaped vars, credential-file locators, and SSH/askpass write-credential locators — the agent keeps only its own model-provider auth. The net-new risk is a *map*, not a new *door*. The kill switch (below) is the operator escape hatch. |
| **Local absolute paths leaking into GitHub.** | An agent could quote a `target_root` into a PR body or a park comment. Paths aren't secret, but an operator who treats them as sensitive can either flip the kill switch or use the **slugs-only** mode (future refinement) that omits paths. |

Conclusion: the feature is *information disclosure of operator-configured,
non-secret data that the agent could already read*, with write-containment
unchanged. That is acceptable, and the kill switch keeps it reversible.

## Recommended design — compact, multi-repo-gated prompt block

A shared builder in `workflow_messages.py`:

```python
def _build_tracked_repos_context(
    current: RepoSpec, specs: list[RepoSpec]
) -> str:
    """Render the 'other tracked repos' awareness block, or '' when there
    is nothing useful to say (<=1 repo, or the kill switch is off)."""
```

Behavior:

- **Returns `""` when `EXPOSE_TRACKED_REPOS` is off or `len(specs) <= 1`.**
  The default single-repo deployment therefore sees *zero* change and
  *zero* added tokens — the project's "absent → today's behavior"
  reversibility, for free.
- When there are ≥2 repos, emit a short block listing each **other**
  repo on one line, plus a one-line framing. The framing must be
  **stage-neutral**: it states only that the *sibling* checkouts are
  read-only references, and says nothing about whether the agent may
  write in its own working directory. Current-worktree write permission
  is granted (or withheld) by the stage prompt itself — the implementer /
  documentation / fixing prompts say "commit your change", while the
  reviewer, decomposer, and question prompts are explicitly read-only. A
  block that said "do not modify any path *other than your current
  worktree*" would wrongly imply a write grant inside the read-only
  stages, so it must not. Example:

  ```
  This orchestrator also tracks the repositories below. Their source is
  checked out locally for cross-repo reference only — treat every path
  listed here as read-only and do NOT modify, commit, or push in any of
  them. (Whether you may write in your own working directory is governed
  by the rest of this prompt, not by this list.) Your task is on
  `owner/lance`.

  - owner/ray — source at /srv/repos/ray (base `main`)
  - owner/arrow — source at /srv/repos/arrow (base `master`)
  ```

- **Cap the list** (e.g. first 10 entries, then `- … and N more`) so a
  host driving dozens of repos can't blow the prompt. One line per repo
  keeps the steady-state cost at roughly *number-of-other-repos* lines.
- Expose **`target_root`** only — the durable canonical checkout — never
  the ephemeral per-issue `issue-N` worktrees.

Injection points (the prompts that do real reasoning):

| Prompt builder | Change |
|---|---|
| `_build_implement_prompt` | add `spec` + `specs` params, prepend/append block |
| `_build_decompose_prompt` | add `spec` + `specs` params, append block |
| `_build_review_prompt` | already takes `spec`; add `specs`, append block |
| `_build_documentation_prompt` | already takes `spec`; add `specs`, append block |
| `_build_question_prompt` | add `spec` + `specs` params, append block |
| `_build_fresh_respawn_preamble` | add `spec` + `specs` params, embed block (see fresh-respawn note below) |

Fix / conflict / followup prompts (`_build_fix_prompt`,
`_build_conflict_resolution_prompt`, `_build_pr_comment_followup`,
`_build_question_followup_prompt`) are sent to a session that **already
received the block at spawn time**, so when they genuinely *resume* that
session they can skip the block and save tokens. The stage handlers
already hold `spec`; they pass `config.default_repo_specs()` for `specs`
(cache it once per handler).

**Fresh-respawn caveat (do not skip the block here).** The "resume"
assumption breaks when `_resume_dev_with_text`
(`stages/implementing.py`) retires the live session and starts a
transcript-less fresh spawn — proactive `DEV_SESSION_MAX_RESUMES`
rotation, the consecutive-silent-park fallback, or poisoned-session
recovery (stale session / context overflow). In that path the followup
text is prefixed with `_build_fresh_respawn_preamble`, and the new agent
has **no prior transcript**, so it never saw the original spawn's block.
The same re-grounding that re-feeds the issue body and conversation must
therefore re-feed the tracked-repos block: include it inside (or
appended to) `_build_fresh_respawn_preamble`, which already runs only on
the `fresh_spawn` branch. Because that preamble re-grounds dev resumes
across `implementing`, `validating` (fix), `resolving_conflict`, and the
`fixing` route, a single injection there covers every fresh-respawn
case; the bare followup builders above stay block-free for true
resumes.

### Config knob

Add to `config.py`, parsed exactly like `DECOMPOSE` / `SQUASH_ON_APPROVAL`:

```python
EXPOSE_TRACKED_REPOS: bool = os.environ.get(
    "EXPOSE_TRACKED_REPOS", "on"
).strip().lower() in ("1", "true", "on", "yes")
```

**Default `on`, but inert for single-repo hosts** (the builder gates on
`len(specs) > 1`). So default-on only ever affects deliberately
multi-repo deployments — exactly the ones the issue is about — while a
security-conscious operator can force it off globally. The user asked for
the awareness to exist, and the disclosed data is operator-configured and
non-secret, so default-on-when-multi-repo is the right posture; the kill
switch preserves reversibility.

### Token cost

- Single-repo (the default): **0 tokens, 0 behavior change.**
- Multi-repo: ~1 line per other repo + a 3-line preamble, capped. For a
  realistic 2–5 repo host that's well under ~10 lines per prompt.

## Alternative — on-demand env-var pointer (deferred)

If prompt-token pressure ever shows up on a many-repo host, evolve to a
near-zero-prompt-cost variant:

- Pass the full list via `extra_env` as `ORCHESTRATOR_TRACKED_REPOS`
  (newline-delimited `slug\ttarget_root\tbase_branch`), and put only a
  **one-line pointer** in the prompt: *"Other tracked repos are listed in
  the `ORCHESTRATOR_TRACKED_REPOS` env var; consult it only if this task
  needs cross-repo context."*
- The agent pays the token cost only when it actually reads the list.
- Wiring cost: `agents.run_agent` already accepts and forwards
  `extra_env` (down to `_agent_env`), so only `_run_agent_tracked`
  (`workflow.py`) needs new threading — it currently builds its
  `run_agent_kwargs` from `extra_args` / `resume_session_id` / `timeout`
  and drops any `extra_env`, so an `extra_env` parameter must be added
  there and passed through. The variable is **not** secret-shaped, so
  `_filter_agent_env` passes it through untouched — confirm with a test
  rather than relying on it implicitly.
- Trade-off: lowest context cost, but worse discoverability (the agent
  may ignore the pointer) and a slightly larger surface. Ship the prompt
  block first; adopt this only if measured token cost justifies it. A
  hybrid (env var holds the full list; prompt carries the pointer plus
  the first few repos inline) is also available.

## Considered but rejected

- **Write a `tracked-repos` file into the worktree.** A file like
  `.agent-orchestrator/tracked-repos.md` would show as untracked in
  `git status`, risk being committed into the PR, and could trip the
  worktree-dirty probes — the codebase already hit exactly this class of
  bug with codex's `-o` tempfile (now kept outside the worktree). Prompt
  text / env vars leave no on-disk residue.
- **Expose per-issue worktree paths.** Those are ephemeral and
  per-issue; the durable, useful path is `target_root`. Listing worktrees
  would be noisier and staler.
- **A new "context repos" config list.** No new config surface — reuse
  `REPOS` / `default_repo_specs()`. The set of tracked repos *is* the set
  the orchestrator already drives.
- **Inject remote URLs or clone instructions.** Not needed (the checkout
  is already local) and a remote URL is closer to sensitive than a path.
- **A pinned-state field per issue.** The list is process-global config,
  not per-issue state; persisting it would just add drift surface.

## Wiring summary (for the eventual implementation pass)

- `config.py` — add `EXPOSE_TRACKED_REPOS` (kill switch).
- `workflow_messages.py` — add `_build_tracked_repos_context(spec, specs)`;
  thread `spec` + `specs` into the five reasoning-prompt builders above
  **and into `_build_fresh_respawn_preamble`** so transcript-less
  fresh dev respawns carry the block too.
- stage handlers (`stages/implementing.py`, `decomposition.py`,
  `validating.py`, `documenting.py`, `question.py`) — pass
  `config.default_repo_specs()` as `specs` (cache once per handler call).
  In `implementing.py`, the `_resume_dev_with_text` fresh-spawn branch
  (`_spawn_prompt(fresh=True)` calling `_build_fresh_respawn_preamble`)
  must thread `spec` + `specs` through; the resume branch
  (`_spawn_prompt(fresh=False)`, bare `followup_text`) stays block-free.
- docs — note the new env var in `docs/configuration.md`, the disclosure
  analysis in `docs/security.md`, and the prompt content in
  `docs/workflow.md`; add a one-line roadmap entry.
- (env-var variant only) `workflow.py::_run_agent_tracked` — add an
  `extra_env` parameter and forward it; `agents.run_agent` already
  forwards `extra_env`, so it needs no change.

## Tests to add when implemented

- `_build_tracked_repos_context` returns `""` for 0/1 repos and when
  `EXPOSE_TRACKED_REPOS` is off; lists the *other* repos, marks the
  current one, and honors the cap for ≥2 repos.
- The five prompt builders embed the block only in the multi-repo case
  and never in single-repo.
- `_build_fresh_respawn_preamble` embeds the block in the multi-repo case
  (and omits it for single-repo / kill-switch-off), so a re-grounded
  fresh agent is told about the sibling repos.
- Fresh-respawn integration: `_resume_dev_with_text` includes the block
  on the `fresh_spawn` path (rotation, silent-park fallback, and
  poisoned-session retry) and omits it on the plain-resume path — assert
  on the prompt passed to the spawn in each branch so a future refactor
  that re-grounds without the block fails the test.
- No `target_root` leaks for the current repo beyond the "your task is on
  X" marker; worktree paths never appear.
- (env-var variant) `_filter_agent_env` preserves
  `ORCHESTRATOR_TRACKED_REPOS`; `_run_agent_tracked` forwards `extra_env`.

## Sequencing

Land the prompt-block builder + `EXPOSE_TRACKED_REPOS` kill switch first
— small, additive, reversible, and inert for single-repo hosts. Defer the
env-var / on-demand variant and the slugs-only mode until a concrete
token-cost or path-sensitivity concern shows up on a real multi-repo
deployment.
