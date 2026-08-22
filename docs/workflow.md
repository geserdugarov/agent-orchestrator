# Workflow — agent roles and command specs

This area documents who runs an issue: which stage invokes which agent role, what that role's prompt grants and
forbids, how the role command specs (`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`) are parsed, and how the spec an
in-flight issue runs under is pinned for the life of its session. Label semantics, transitions, and per-handler
behavior belong to [`state-machine.md`](state-machine.md).

## The pages in this area

- [`workflow/roles.md`](workflow/roles.md) — the three roles and the stages that spawn each one, where a handler and
  its spawn live, the session-reuse rules per role, what the late adjudication of an oversized committed candidate is
  asked and may answer, the owner read a finished run passes before anything acts on it and what each verdict earns
  past it, the order a cleared `split` runs its snapshot, children, supersession, and cleanup in, what a human's edit
  or answer changes while that candidate is frozen, and the local verify gate that is a stage step rather than a
  role.
- [`workflow/conversations.md`](workflow/conversations.md) — the two operator-applied conversation labels: what the
  `question` and `discussion` prompts grant and forbid, what a round may leave behind, the plan PR a confirmed design
  earns, and the tracked-repository awareness block the working-agent prompts carry.
- [`workflow/command-specs.md`](workflow/command-specs.md) — the spec grammar, backend selection and `CODEX_BIN` /
  `CLAUDE_BIN`, worked examples, and what pinning a spec buys.

For the higher-level design (multi-repo dispatch, push hardening, agent subprocess shape), see
[`architecture.md`](architecture.md). For the audit event log and analytics sink, see
[`observability/event-streams.md`](observability/event-streams.md); for the usage parser over them, see
[`observability/usage.md`](observability/usage.md), and for the map over every observability surface,
[`observability.md`](observability.md). For env vars and the operator runbooks beside them, see
[`configuration.md`](configuration.md). For the user-facing summary, see [`../README.md`](../README.md).

Stage and label names are spelled apart across this area as they are in
[`state-machine/labels-and-state.md`][workflow-labels]: a bare tag names the **stage** — the handler, the subpackage
under `orchestrator/workflow/stages/` holding it, and the identifier a session's analytics row is attributed to —
while `workflow:<tag>` is the **wire label** the GitHub issue carries. `in_review`, `question`, `discussion`, and the
`done` / `rejected` terminals were never namespaced, so those read the same on both sides.

## Roles at a glance

- **Decomposer** — `DECOMPOSE_AGENT` (default `claude`), spawned by the decomposing, question, and discussion stages,
  each of which pins its own session per issue.
- **Implementer / dev** — `DEV_AGENT` (default `claude`), spawned by implementing and resumed by documenting,
  validating, fixing, and conflicts; locked per issue after the first spawn.
- **Reviewer** — `REVIEW_AGENT` (default `codex`), spawned fresh by validating every round, so the current config
  always wins.

The defaults (`claude` decomposes, `claude` implements, `codex` reviews) use both backends; both CLIs need to be
authenticated on the host before the orchestrator starts. Per-role detail:
[`workflow/roles.md`](workflow/roles.md#the-three-roles).

## Question stage — read-only Q&A on the `question` label

The `question` workflow label is operator-applied: there are no automatic transitions in or out. `_handle_question`
runs the configured `DECOMPOSE_AGENT` backend in the issue's per-issue worktree with a read-only prompt that forbids
modifying, committing, or pushing files, posts the answer as a comment pinging `HITL_HANDLE`, opens no PR, and resumes
the session pinned to `question_agent` / `question_session_id` on every human reply. A read-only violation parks
awaiting human and preserves the worktree; closing the issue flips it to `done`. Full contract:
[`workflow/conversations.md#question-stage`](workflow/conversations.md#question-stage). Per-`park_reason` semantics:
[`state-machine/conversation-stages.md#_handle_question-label-question`][question-handler].

## Discussion stage — architecture discussion on the `discussion` label

The `discussion` label is operator-applied like `question` and reuses the decomposer's backend to explore a design as
a tree, closing each round with numbered questions a human can answer or overrule by number. It is read-only in the
same way up to one point: once a human confirms on the thread that they and the agent understand the design the same
way, the agent may commit `plans/issue-<number>.md` alone, and the stage publishes that file as a plan pull request
whose verdict is the issue's ending — merged is `done`, closed unmerged is `rejected`. Full contract:
[`workflow/conversations.md#discussion-stage`](workflow/conversations.md#discussion-stage). Publication, recovery, and
park semantics:
[`state-machine/conversation-stages.md#_handle_discussion-label-discussion`][discussion-handler].

## Tracked-repos awareness in working-agent prompts

When the orchestrator drives more than one repo (`REPOS`) and `EXPOSE_TRACKED_REPOS` is on (the default), the
reasoning-prompt builders prepend a compact, read-only awareness block naming the *other* repos this process tracks
and where their durable checkouts are. The framing is stage-neutral, so it never widens what the surrounding prompt
granted, and a single-repo host gets an empty string and zero added tokens. Which builders embed it and what the block
contains:
[`workflow/conversations.md#tracked-repository-awareness-in-working-agent-prompts`][tracked-repos]. Disclosure
analysis: [`security.md#cross-repo-awareness-disclosure-expose_tracked_repos`][disclosure].

## Examples

Any of the lines below is a valid value for any of the three role env vars; the full set, including the codex quoting
rules, is in [`workflow/command-specs.md#examples`](workflow/command-specs.md#examples).

```dotenv
DEV_AGENT=claude --model claude-opus-4-7 --effort high
REVIEW_AGENT=codex -m gpt-5.5-codex -c 'model_reasoning_effort="high"'
DECOMPOSE_AGENT=claude
```

## In-flight session lock — pinned full spec until the session ends

The parsed spec is persisted to pinned state as the durable identity of a **session**, and the pin is the full spec —
backend AND args — so a mid-flight resume cannot lose the model / reasoning-effort the session was started with. The
lock is per pin, not per issue: `dev_agent` is written on the first implementing spawn and read by every dev-side
resume for the life of the issue, while `decomposer_agent`, `question_agent`, and `discussion_agent` are each seeded
from `DECOMPOSE_AGENT` on their own first spawn. So a flip reaches any spawn that has no pin to read yet — including
the first question or discussion round on an issue whose decomposing session is already locked — and never a session
that has already pinned one. The reviewer is spawned fresh every round, so `REVIEW_AGENT` takes effect on the next
validating tick. Per-role keys, the resume path, and the legacy values still honored on read:
[`workflow/command-specs.md#in-flight-session-lock`][session-lock].

[tracked-repos]: workflow/conversations.md#tracked-repository-awareness-in-working-agent-prompts
[disclosure]: security.md#cross-repo-awareness-disclosure-expose_tracked_repos
[session-lock]: workflow/command-specs.md#in-flight-session-lock
[workflow-labels]: state-machine/labels-and-state.md#workflow-labels
[question-handler]: state-machine/conversation-stages.md#_handle_question-label-question
[discussion-handler]: state-machine/conversation-stages.md#_handle_discussion-label-discussion
