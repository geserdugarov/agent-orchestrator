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
| (child) any in-flight label | `done` | workflow.py:786 (via `_finalize_if_pr_merged`, called from decomposition.py:779) | Stale closed-but-not-finalized child whose linked PR is already merged; flipped to `done` so the parent's aggregation can proceed |

### `_handle_umbrella` — label `umbrella`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `umbrella` | `decomposing` | workflow_drift.py:247 | User-content drift on the umbrella parent |
| `umbrella` | `done` | decomposition.py:957 | All children resolved (umbrella has no implementation of its own) |
| (child) `blocked` | `ready` | decomposition.py:983 | Dep-graph activation walk on umbrella children |
| (child) any in-flight label | `done` | workflow.py:786 (via `_finalize_if_pr_merged`, called from decomposition.py:930) | Stale closed-but-not-finalized child whose linked PR is already merged; flipped to `done` so the umbrella aggregation can proceed |

### `_handle_implementing` — label `implementing`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `implementing` | `done` | workflow.py:786 (via `_finalize_if_pr_merged`) | External PR merge while still implementing |
| `implementing` | `rejected` | workflow.py:880 (via `_finalize_if_issue_closed`) | Issue closed without merged PR |
| `implementing` | **`documenting`** | implementing.py:793 (in `_on_commits`) | **Dev produced commits, branch pushed, PR opened (or reused)** |

### `_handle_documenting` — label `documenting`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `documenting` | `done` / `rejected` | workflow.py:786 / 880 | External merge or issue close |
| `documenting` | `validating` / **`in_review`** | documenting.py (via `_advance_after_docs_push`) | Docs commit landed and pushed: route to `in_review` when `docs_final_pending=True` (and update `agent_approved_sha` to the new head), otherwise `validating` |
| `documenting` | `validating` / **`in_review`** | documenting.py (via `_advance_after_docs_push`) | Recovered docs commit pushed after a no-change confirmation: same final-docs marker discriminator as above |
| `documenting` | `validating` / **`in_review`** | documenting.py (via `_advance_after_docs_no_change`) | `DOCS: NO_CHANGE` verdict; nothing to push: route to `in_review` when `docs_final_pending=True` (head unchanged so `agent_approved_sha` already matches), otherwise `validating` |

### `_handle_validating` — label `validating`

| From | To | File:line | Trigger |
| ---- | -- | --------- | ------- |
| `validating` | `done` / `rejected` | workflow.py:786 / 880 | External merge or issue close |
| `validating` | **`documenting`** | validating.py:678 | **User-content drift dev resume pushed** a new commit (`outcome == "pushed"`); no `docs_final_pending` marker — pre-approval pass |
| `validating` | **`documenting`** | validating.py:768 | **Transient-park recovery push** finished (`push_failed` retried, or `agent_timeout` that had actually committed); no marker |
| `validating` | **`documenting`** | validating.py:807 | **Awaiting-human resume produced a pushed dev fix**; no marker |
| `validating` | **`documenting`** | validating.py:1067 | **Reviewer `VERDICT: APPROVED` + verify gate clean + squash succeeded (or disabled)** — sets `docs_final_pending=True` so the docs pass hands off to `in_review` (NOT back to `validating`) |
| `validating` | **`documenting`** | validating.py:1153 | **CHANGES_REQUESTED dev-fix loop pushed** a new commit; no marker |

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

Today `documenting` is entered after **every code-changing branch update**,
**plus** the new final-docs hop after reviewer approval. The docs pass is
rerun for every fix, every drift resume, every conflict-resolution push,
AND once more between approval and `in_review`. The complete entry set:

1. **implementing.py:793** — `_on_commits`: dev's initial implementation
   commits, PR opened.
2. **validating.py:678** — `_handle_validating` user-content drift dev
   resume pushed a commit.
3. **validating.py:768** — `_handle_validating` awaiting-human transient
   recovery (push_failed retry / agent_timeout that committed) finished
   landing the dev's fix.
4. **validating.py:807** — `_handle_validating` awaiting-human resume:
   human reply produced a clean dev-fix push.
5. **validating.py:1067** — `_handle_validating` reviewer `VERDICT:
   APPROVED` + verify gate clean + squash succeeded (or disabled). The
   only entry that sets `docs_final_pending=True`; the docs pass on
   this trip advances to `in_review` rather than back to `validating`.
6. **validating.py:1153** — `_handle_validating` CHANGES_REQUESTED fix
   loop pushed a clean dev fix.
7. **in_review.py:593** — `_handle_in_review` user-content drift dev
   resume pushed a commit (`outcome == "pushed"`).
8. **fixing.py:376** — `_handle_fixing` resumed the dev on PR comment
   feedback and pushed a clean fix.
9. **conflicts.py:244** — `_handle_resolving_conflict` user-content
   drift resume pushed a commit.
10. **conflicts.py:405** — `_handle_resolving_conflict` recovered (crash
    recovery) commit ahead of remote pushed.
11. **conflicts.py:531** — `_handle_resolving_conflict` clean rebase
    produced a new HEAD and pushed.
12. **conflicts.py:655** — `_handle_resolving_conflict` agent-resolved
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

### Status note (after issue #266)

Issue #266 has landed the **final-docs handoff** half of the target above:
the `validating` -> `documenting` -> `in_review` chain now exists on the
approval branch via the `docs_final_pending=True` marker, with
`_handle_documenting`'s success exits routing to `in_review` (and updating
`agent_approved_sha` to the new head when a docs commit lands AND the
companion sentinel `final_docs_approval_seeded` confirms validating
actually persisted a non-empty approval SHA this round — both
`gh.get_pr()` succeeded AND `_head_sha()` returned a non-empty local
SHA — so the AUTO_MERGE invariant survives; when either fails the
sentinel is absent and any stale `agent_approved_sha` left over from a
prior round stays untouched so AUTO_MERGE remains gated until the next
reviewer round explicitly approves). The pre-approval `documenting` entries
(rows 1–4, 6–12 in section 2) are still present — collapsing those into
direct `validating` routes is the remainder of the parent #262 work and
lives in subsequent children.

Concretely, the following call sites become `set_workflow_label(issue, "validating")`
(or are removed if redundant) under the target:

- implementing.py:793
- validating.py:678 / 768 / 807 / 1153
- in_review.py:593
- fixing.py:376
- conflicts.py:244 / 405 / 531 / 655

A new transition from `validating` (approval branch) into `documenting`,
with the `documenting` handler then advancing to `in_review` instead of
`validating`, **has landed in #266**. The documenting success exits now
branch on `docs_final_pending`: when set (final-docs handoff), they route
to `in_review` via `_advance_after_docs_push` / `_advance_after_docs_no_change`;
otherwise they keep the legacy route to `validating`.

The validating-side reviewer-approval branch (validating.py:1067) **now**
sets `docs_final_pending=True` and flips to `documenting` after squash +
watermark seeding (it previously flipped directly to `in_review`). The
squash + approval-comment + watermark-seeding bookkeeping stays in
validating, where `agent_approved_sha` and the companion sentinel
`final_docs_approval_seeded` are set together inside the `else` arm of
its `gh.get_pr()` try AND inside the `if new_head_sha:` block — so an
empty local SHA leaves both untouched and the sentinel stays False;
documenting only ratchets `pr_last_comment_id` on the handoff for any
issue-thread reply the awaiting-human resume consumed, and updates
`agent_approved_sha` to the new pushed head when a docs commit lands
AND `final_docs_approval_seeded` confirms this round actually persisted
a non-empty approval SHA (so the AUTO_MERGE
`agent_approved_sha == pr.head.sha` invariant survives). The remainder
of the parent #262 target — collapsing the pre-approval `documenting`
entries listed above into direct `validating` routes — is out of scope
for this child and lives in subsequent children.

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

- Collapsing the pre-approval `documenting` entries (rows 1–4, 6–12 in
  section 2) into direct `validating` routes — those code-changing
  pushes still hop through `documenting` before the reviewer re-runs.
- Docs-stage prompt rewording for the post-approval pass — the existing
  prompt is still used unchanged on the final-docs hop.
- The validating-side squash/watermark-seeding relocation; squash +
  approval + watermark seed all still happen in validating before the
  final-docs hop, and the documenting handler only ratchets the
  watermark for resume-consumed replies.
