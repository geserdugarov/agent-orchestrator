# Stage-lifecycle audit — workflow transitions and `documenting` triggers

Audit of every workflow-label state change in the current code (read-only;
no runtime changes in this child issue). Parent: #262.

Sources inspected: `orchestrator/workflow.py`, `orchestrator/stages/*`,
`orchestrator/workflow_drift.py`, `orchestrator/worktrees.py`,
`docs/architecture.md`, `docs/workflow.md`, and the per-stage tests in
`tests/test_workflow_*.py`.

## 1. Per-stage transition map (current code)

Each row is a workflow-label state change grouped by the handler it
runs from. That is every `gh.set_workflow_label(...)` call site PLUS
the initial-label paths where a label is first applied to a newly
created issue (`gh.create_child_issue(..., labels=[...])` in
`_handle_decomposing`). Terminal `done` / `rejected` finalize paths
are collapsed under a single "Terminal" entry per handler.

### `_handle_pickup` — no label

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| (none) | `decomposing` | workflow.py:678 | `DECOMPOSE=on` pickup |
| (none) | `implementing` | workflow.py:704 | `DECOMPOSE=off` pickup |

### `_handle_decomposing` — label `decomposing`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `decomposing` | `blocked` / `umbrella` | decomposition.py:259 | Half-finished recovery: finalize to `umbrella` when `umbrella=True` else `blocked` |
| `decomposing` | `implementing` | decomposition.py:305 | DECOMPOSE kill-switch bailout (operator restarted with `DECOMPOSE=off`) |
| `decomposing` | `ready` | decomposition.py:446 | Manifest decision `single` |
| `decomposing` | `blocked` / `umbrella` | decomposition.py:575 | Manifest decision `split` finalization |
| (new child issue) (none) | `blocked` | decomposition.py:483 (via `gh.create_child_issue(..., labels=["blocked"])`) | Manifest decision `split`: every child is created already labeled `blocked` so the parent can later activate it via the dep-graph walk |
| (child) `blocked` | `ready` | decomposition.py:587 | Same-tick activation of no-dep children |

### `_handle_ready` — label `ready`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `ready` | `decomposing` | workflow_drift.py:247 (via `_route_drift_to_decomposing`) | User-content drift before implementation starts |
| `ready` | `implementing` | decomposition.py:656 | Normal path; falls through into `_handle_implementing` on the same tick |

### `_handle_blocked` — label `blocked`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `blocked` | `decomposing` | workflow_drift.py:247 | User-content drift on the parent |
| `blocked` | `ready` | decomposition.py:814 | All children resolved to `done`; parent re-enters implementation |
| (child) `blocked` | `ready` | decomposition.py:834 | Dep-graph activation walk (parent ticks a sibling free) |
| (child) any in-flight label | `done` | workflow.py:781 (via `_finalize_if_pr_merged`, called from decomposition.py:779) | Stale closed-but-not-finalized child whose linked PR is already merged; flipped to `done` so the parent's aggregation can proceed |

### `_handle_umbrella` — label `umbrella`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `umbrella` | `decomposing` | workflow_drift.py:247 | User-content drift on the umbrella parent |
| `umbrella` | `done` | decomposition.py:957 | All children resolved (umbrella has no implementation of its own) |
| (child) `blocked` | `ready` | decomposition.py:983 | Dep-graph activation walk on umbrella children |
| (child) any in-flight label | `done` | workflow.py:781 (via `_finalize_if_pr_merged`, called from decomposition.py:930) | Stale closed-but-not-finalized child whose linked PR is already merged; flipped to `done` so the umbrella aggregation can proceed |

### `_handle_implementing` — label `implementing`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `implementing` | `done` | workflow.py:781 (via `_finalize_if_pr_merged`) | External PR merge while still implementing |
| `implementing` | `rejected` | workflow.py:875 (via `_finalize_if_issue_closed`) | Issue closed without merged PR |
| `implementing` | **`documenting`** | implementing.py:793 (in `_on_commits`) | **Dev produced commits, branch pushed, PR opened (or reused)** |

### `_handle_documenting` — label `documenting`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `documenting` | `done` / `rejected` | workflow.py:781 / 875 | External merge or issue close |
| `documenting` | `validating` | documenting.py:362 | Docs commit landed and pushed |
| `documenting` | `validating` | documenting.py:406 | Recovered docs commit pushed after a no-change confirmation |
| `documenting` | `validating` | documenting.py:440 | `DOCS: NO_CHANGE` verdict; nothing to push, advance directly |

### `_handle_validating` — label `validating`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `validating` | `done` / `rejected` | workflow.py:781 / 875 | External merge or issue close |
| `validating` | **`documenting`** | validating.py:678 | **User-content drift dev resume pushed** a new commit (`outcome == "pushed"`) |
| `validating` | **`documenting`** | validating.py:768 | **Transient-park recovery push** finished (`push_failed` retried, or `agent_timeout` that had actually committed) |
| `validating` | **`documenting`** | validating.py:807 | **Awaiting-human resume produced a pushed dev fix** |
| `validating` | `in_review` | validating.py:1056 | Reviewer `VERDICT: APPROVED` + verify gate clean + squash succeeded (or disabled) |
| `validating` | **`documenting`** | validating.py:1142 | **CHANGES_REQUESTED dev-fix loop pushed** a new commit |

### `_handle_in_review` — label `in_review`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `in_review` | `done` | in_review.py:260 | PR merged externally (or via AUTO_MERGE at in_review.py:793) |
| `in_review` | `rejected` | in_review.py:284 | PR closed without merge |
| `in_review` | `rejected` | in_review.py:331 | Open PR + issue closed manually (human stop signal) |
| `in_review` | `fixing` | in_review.py:469 | Fresh PR feedback on any of four surfaces (issue/PR-conv/inline/summary) |
| `in_review` | **`documenting`** | in_review.py:593 | **User-content drift dev resume pushed a commit** (`outcome == "pushed"`) |
| `in_review` | `validating` | in_review.py:595 | User-content drift dev resume returned an `ACK` (no commit) |
| `in_review` | `resolving_conflict` | in_review.py:747 | AUTO_MERGE on, no human CHANGES_REQUESTED, approved for current head, but PR is not mergeable (route fires BEFORE the combined-check gate at in_review.py:750, so green CI is not a precondition) |

### `_handle_fixing` — label `fixing`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `fixing` | `done` | fixing.py:105 | PR merged externally while fixing |
| `fixing` | `rejected` | fixing.py:129 / 155 | PR closed-without-merge OR issue closed manually |
| `fixing` | `validating` | fixing.py:262 | Rescan finds no unread feedback past the watermarks — skip docs (no fix work) |
| `fixing` | **`documenting`** | fixing.py:376 | **Dev resume pushed a fix in response to PR feedback** |

### `_handle_resolving_conflict` — label `resolving_conflict`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `resolving_conflict` | `done` / `rejected` | conflicts.py:118 / 142 / 183 | PR terminal arcs (merged externally, closed-without-merge, issue closed) |
| `resolving_conflict` | **`documenting`** | conflicts.py:244 | **User-content drift dev resume pushed** a resolution |
| `resolving_conflict` | **`documenting`** | conflicts.py:405 | **Recovered commit pushed** (ahead-of-remote crash recovery) |
| `resolving_conflict` | `validating` | conflicts.py:502 | Clean rebase, branch already up-to-date with base (no diff to docs) |
| `resolving_conflict` | **`documenting`** | conflicts.py:531 | **Clean rebase produced a new HEAD and pushed** |
| `resolving_conflict` | **`documenting`** | conflicts.py:655 | **Agent-resolved conflicts or awaiting-human resume pushed** |

### `_handle_question` — label `question`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `question` | `done` | question.py:205 | Issue closed manually (terminal signal) |

### Pre-tick PR-worktree refresh detour (`worktrees.py`)

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `validating` / `in_review` / `fixing` | `resolving_conflict` | worktrees.py:1933 | Pre-tick base refresh detected the PR-having worktree is behind base |

## 2. Every current entry into `documenting`

Today `documenting` is entered after **every code-changing branch update**.
That is far more than "after implementation" — the docs pass is rerun for
every fix, every drift resume, and every conflict-resolution push. The
complete entry set:

1. **implementing.py:793** — `_on_commits`: dev's initial implementation
   commits, PR opened.
2. **validating.py:678** — `_handle_validating` user-content drift dev
   resume pushed a commit.
3. **validating.py:768** — `_handle_validating` awaiting-human transient
   recovery (push_failed retry / agent_timeout that committed) finished
   landing the dev's fix.
4. **validating.py:807** — `_handle_validating` awaiting-human resume:
   human reply produced a clean dev-fix push.
5. **validating.py:1142** — `_handle_validating` CHANGES_REQUESTED fix
   loop pushed a clean dev fix.
6. **in_review.py:593** — `_handle_in_review` user-content drift dev
   resume pushed a commit (`outcome == "pushed"`).
7. **fixing.py:376** — `_handle_fixing` resumed the dev on PR comment
   feedback and pushed a clean fix.
8. **conflicts.py:244** — `_handle_resolving_conflict` user-content
   drift resume pushed a commit.
9. **conflicts.py:405** — `_handle_resolving_conflict` recovered (crash
   recovery) commit ahead of remote pushed.
10. **conflicts.py:531** — `_handle_resolving_conflict` clean rebase
    produced a new HEAD and pushed.
11. **conflicts.py:655** — `_handle_resolving_conflict` agent-resolved
    or awaiting-human resumed conflict push (`_post_conflict_resolution_result`).

Cross-cutting observations:

- Both validating and conflict-resolution dev resumes route through
  `documenting` because each can land code that the README / docs /
  plans must reflect.
- Two paths bypass `documenting` deliberately because no diff lands:
  fixing.py:262 (no unread feedback → straight back to `validating`)
  and conflicts.py:502 (base-up-to-date no-op).
- The `in_review` "ACK" drift outcome (in_review.py:595) likewise
  bounces directly to `validating`: nothing landed for the docs pass
  to react to.

## 3. Proposed simplification target (no code changes in this child)

Per the parent issue (#262) and this child's brief, the target shape is:

- **Implementation, fix, and conflict commits route to `validating`**
  (NOT `documenting`).
- **`documenting` runs once, after reviewer approval, before the issue
  enters `in_review`.**

Under that target, the transition map collapses to:

| From | To | Trigger |
| ---- | -- | ------- |
| `implementing` | `validating` | Dev's initial implementation commits, PR opened |
| `validating` | `validating` (rerun) | CHANGES_REQUESTED dev-fix push, awaiting-human resume push, drift resume push, transient-park recovery push |
| `validating` | **`documenting`** | Reviewer `VERDICT: APPROVED` (+ verify gate clean + squash succeeded) |
| `documenting` | `in_review` | Docs commit pushed OR `DOCS: NO_CHANGE` verdict |
| `in_review` | `fixing` / `resolving_conflict` / `done` / `rejected` | unchanged |
| `fixing` | `validating` | Dev-fix push (no docs hop) AND the no-unread-feedback bounce |
| `resolving_conflict` | `validating` | Every clean / recovered / agent-resolved / awaiting-human resumed push (no docs hop); the base-up-to-date no-op already targets `validating` |
| `in_review` drift "pushed" | `validating` | Symmetric with the new fixing/conflict routes — no docs hop until the reviewer approves the next round |

Concretely, the following call sites become `set_workflow_label(issue, "validating")`
(or are removed if redundant) under the target:

- implementing.py:793
- validating.py:678 / 768 / 807 / 1142
- in_review.py:593
- fixing.py:376
- conflicts.py:244 / 405 / 531 / 655

A new transition is added from `validating` (approval branch) into
`documenting`, and the `documenting` handler then advances to `in_review`
instead of `validating` (currently documenting.py:362 / 406 / 440 all set
`validating`).

The validating-side reviewer-approval branch (validating.py:1056) currently
sets `in_review` after squash + watermark seeding; under the target it
sets `documenting` and the squash + watermark-seeding bookkeeping moves to
the `documenting` -> `in_review` exit (or is duplicated there). That
relocation is a runtime change and is out of scope for this child; the
re-routing is captured here for the implementer in the follow-up child.

### Net contract change

- **One docs pass per merged PR** instead of one per code-changing push.
  Tokens spent on the docs agent drop linearly with the number of
  review rounds and conflict-resolution rounds.
- **Reviewer always evaluates the un-documented diff**, so reviewer
  feedback doesn't fight a stale docs commit.
- **Docs reflect the reviewer-approved (and optionally squashed) head**,
  so README / plans updates land against the SHA the PR will actually
  merge.

### Out of scope here

- Runtime behaviour changes (handlers, prompts, tests).
- Docs-stage prompt rewording — once the docs pass is post-approval the
  `DOCS: NO_CHANGE` marker still applies but the prompt context shifts;
  that is the implementer-child's job.
- The validating-side squash/watermark-seeding relocation when the
  approval exit moves to `documenting`.
