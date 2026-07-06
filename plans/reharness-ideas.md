# Reharness Ideas — What's Worth Borrowing

## Context

Sergei Belousov's [*Reasoning Compiler*](https://bes-dev.com/posts/reasoning-compiler/)
and the [`bes-dev/reharness`](https://github.com/bes-dev/reharness) project
describe a **compiler** that spends a model's intelligence once, at compile
time, to emit a deterministic finite-state-machine pipeline. Natural-language
requests (or recorded traces, or amendments) are distilled into a PRD, a human
approves the *intent*, and a one-pass design step lowers that into an FSM whose
states are mostly ordinary code — only a few marked `agent` leaves call an LLM
at runtime. The headline claim: *"the cheapest agent is the one that never
calls the model."*

`agent-orchestrator` is **adjacent but not the same thing**. Both drive a task
through an FSM to a reviewed deliverable. But Reharness's FSM is a *compiled
artifact per task class* — generated, statically analyzed, dry-runnable for
zero tokens, and self-evolving. Our FSM is a *single hand-authored graph*
([`orchestrator/state_machine.py`](../orchestrator/state_machine.py),
[`docs/state-machine.md`](../docs/state-machine.md)), fixed across all issues,
with state living in GitHub (one label + one pinned JSON comment) so the process
stays stateless. We do not generate the topology, we do not compile a dataflow
bus, and we deliberately keep the model *in* the runtime loop (the implementer,
reviewer, and decomposer are the point, not an overhead to compile away).

**Does Claude Code / its GitHub-Actions + hooks + skills stack implement the
same functionality?** No. Those are runtime-configured, model-in-the-loop
workflows — closer to what we already do than to Reharness. None of them add a
compile step that emits a deterministic FSM you can statically verify and
dry-run for zero tokens. So the interesting question is not "adopt Reharness,"
it's "which of its *compiler-discipline* ideas close a real gap in our
hand-authored machine." Two survive a picky review. Almost everything else is
either the compiler thesis itself (out of scope by design) or already present.

## Filter

The issue's constraint is explicit: *be picky; reject everything that is just
nice to have; keep functionality limited and optimised for usefulness.* Each
candidate must:

1. **Close a gap we can actually name** in the current machine, not a
   hypothetical one.
2. **Stay small, additive, reversible.** Zero new deps; absent/off → today's
   behavior byte-for-byte.
3. **Not fork the model.** The hand-authored label graph stays the source of
   truth; state stays in GitHub; the model stays in the runtime loop. We borrow
   Reharness's *discipline* (verify the graph statically; account for cost),
   never its *machinery* (generated topology, compiled dataflow, evolve ledger).

## Proposal 1 — Static reachability + terminal-liveness lint on the transition graph

Reharness ships two lightweight static engines (78 lines total): a
**reachability** BFS (every state reachable from start; every state reaches a
terminal) and a monotone **dataflow** analysis. The reachability half maps
directly onto a gap in our graph; the dataflow half does not (we have no scalar
bus — see rejects).

### Gap

[`ALLOWED_TRANSITIONS`](../orchestrator/state_machine.py) is the declared,
guarded state graph, and the runtime guard (`guard_transition`,
`WORKFLOW_TRANSITION_GUARD` = `off`/`warn`/`enforce`, default `warn`) checks one
`current → new` edge at a time at write time. `tests/test_state_machine.py`
already covers the *structural* properties well:

- every state + the `None` entry is a key (`test_keys_cover_every_state_plus_entry`);
- terminals have no outgoing edges; every target is a real `WorkflowLabel`;
- `question` has no inbound edge; the entry is not terminalizable;
- the detour set matches `base_sync`;
- **every `set_workflow_label(...)` call site targets a label that appears as
  *some* edge's target** (`test_every_emitted_target_is_reachable`).

What is **not** checked is the graph's *global* well-formedness — the two BFS
directions Reharness runs:

- **Forward reachability (transitive).** The existing
  `test_every_emitted_target_is_reachable` is a 1-hop *set-membership* check:
  target `X` passes as long as it is *somebody's* target. It cannot catch an
  orphaned island — e.g. a future edit that leaves `{X → Y, Y → X}` where
  neither is reachable from the entry frontier still passes (each is the
  other's target) even though a real BFS from `None` never arrives.
- **Terminal liveness (co-reachability).** *Nothing* asserts every non-terminal
  state has a path to `done`/`rejected`. A future edit could introduce a
  non-terminal sink or an exit-less cycle — an issue that can enter a state it
  can never leave toward a terminal — and no test would notice.

The state graph is public contract that live issues carry, and it is edited
carefully — so this is a low-probability class of bug. But the check is ~15
lines, runs in the existing test module at zero runtime cost, and turns "we
reviewed the diagram carefully" into a mechanical invariant. That trade is
worth making before any further state-machine work (the roadmap's `specifying`,
architectural-review, and dynamic-workflow entries all add states/edges).

### Sketch

Two meta-tests added to `TransitionTableTest` in
[`tests/test_state_machine.py`](../tests/test_state_machine.py) (no production
code changes; the check is a property of the existing table):

```python
def test_every_state_reachable_from_entry(self) -> None:
    # BFS forward from the real entry frontier: the None pseudo-entry
    # plus operator-applied entry labels. `question` has no inbound edge
    # (it is operator-applied only), so it must be seeded as already-seen
    # or it reports as an orphan; `blocked` IS reachable via `decomposing`.
    seen = {WorkflowLabel.QUESTION}
    frontier = [None, WorkflowLabel.QUESTION]
    while frontier:
        state = frontier.pop()
        for target in ALLOWED_TRANSITIONS.get(state, frozenset()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    self.assertEqual(set(WorkflowLabel) - seen, set())  # no orphan island

def test_every_nonterminal_reaches_a_terminal(self) -> None:
    # BFS on the reversed graph from the terminals: liveness.
    terminals = {WorkflowLabel.DONE, WorkflowLabel.REJECTED}
    reverse: dict = {}
    for src, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            reverse.setdefault(target, set()).add(src)
    seen = set(terminals)
    frontier = list(terminals)
    while frontier:
        state = frontier.pop()
        for pred in reverse.get(state, set()):
            if pred is not None and pred not in seen:
                seen.add(pred)
                frontier.append(pred)
    self.assertEqual(set(WorkflowLabel) - seen, set())  # no exit-less sink
```

### Cost

- Zero runtime cost, zero new deps, zero production-code change.
- The seed-frontier nuance (`question` is operator-only; the reversed BFS must
  skip the `None` pseudo-entry) is the only subtlety — encode it in the test
  and comment it, exactly as the existing `test_question_has_no_inbound_edge`
  documents the same fact.

### Explicitly out of this proposal

- **Bounded-cycle verification.** Reharness requires every `loop` to declare a
  `max`. Our cycles (`validating ↔ fixing`, `validating ↔ resolving_conflict`,
  `in_review → fixing → validating`, the `decomposing` self-loop) *are* each
  bounded — by `MAX_REVIEW_ROUNDS`, `MAX_CONFLICT_ROUNDS`, `MAX_RETRIES_PER_DAY`,
  `DEV_SESSION_MAX_RESUMES` — but the caps live in handler logic, not on the
  edge table, so this is not mechanically checkable from `ALLOWED_TRANSITIONS`
  alone. Documenting the "every cycle passes through a capped edge" invariant in
  `docs/state-machine.md` is the honest slice; a synthetic cap-exhaustion test
  per loop is a separate, larger piece of work. Do not fake a graph check here.

## Proposal 2 — Per-issue cumulative token / cost verdict

Reharness reports a **verdict** at the end of every run: actual token count and
dollar cost, down to `0 agent runs · 0 tokens · $0.0000` when a task compiles to
pure code. The discipline behind the slogan — *you cannot optimise a cost you
cannot see per unit of work* — is worth borrowing even though we will never
compile the model away.

### Gap

We already parse per-**run** usage: `usage.parse_agent_usage`
([`orchestrator/usage.py`](../orchestrator/usage.py)) decodes tokens, model, and
`cost_usd`/`cost_source` from each agent's stdout, and `_run_agent_tracked`
([`orchestrator/workflow.py`](../orchestrator/workflow.py)) appends one
`agent_exit` analytics record per spawn, carrying `repo`/`issue`/`stage`/
`agent_role`. That sink is on by default: `ANALYTICS_LOG_PATH` resolves to
`LOG_DIR/analytics.jsonl` when unset and only disables on empty / `off` /
`disabled` / `none` (see
[`docs/observability.md`](../docs/observability.md#analytics-sink-analytics_log_path)).
So the raw per-run data almost always *exists on disk*.

The gap is not that the data is missing — it is that **no cumulative per-issue
total is ever surfaced on the GitHub issue itself.** To answer "what did this PR
cost to produce?" today, an operator must either `grep`/aggregate the local
JSONL by hand or stand up the Postgres sync + Streamlit dashboard (in the
separate `dashboard` dependency group, excluded from the default
`uv sync --locked`). Even the durable pinned-state comment is an *invisible* HTML
marker (`<!--orchestrator-state ...-->`), so nothing human-readable on the issue
carries a running spend. That inline, at-a-glance total is exactly what
Reharness's verdict provides — and what lets an operator make the cost/quality
call the issue asks us to optimise for (codex vs claude, which model, is
decomposition paying for itself).

Our runaway-cost guards (R3: `AGENT_TIMEOUT`, `MAX_RETRIES_PER_DAY`,
`MAX_REVIEW_ROUNDS`, `MAX_CONFLICT_ROUNDS`) bound *spawns* and *wall-clock* —
not *tokens* or *dollars*. A cheap model looping within the caps, or an
expensive model taking several legitimate rounds, produces a real spend the
operator never sees per issue.

### Sketch

Accumulate a tiny counter in the durable pinned state, folded in by the caller
that already writes that state, and surface it on a terminal comment:

- **Accumulate via the caller, not inside the runner.** `_run_agent_tracked`
  already parses `usage.parse_agent_usage` for its analytics record, but it
  returns an `AgentResult` and never receives the caller's `PinnedState` —
  and every handler writes its own preexisting state *after* the agent returns,
  so an internal `write_pinned_state` inside the runner would be clobbered by
  that later write. Instead, surface the parsed `UsageMetrics` back to the caller
  (return it alongside the result, or attach it to `AgentResult`) and have each
  handler fold `+= tokens` / `+= cost_usd` / `+= 1` into the SAME `PinnedState`
  object it already persists. That keeps the single-writer discipline the state
  machine relies on. New pinned keys `issue_total_tokens` /
  `issue_total_cost_usd` / `issue_agent_runs` (add to the pinned-state schema in
  [`docs/state-machine.md#pinned-state`](../docs/state-machine.md)).
- **Surface on a real terminal surface.** The umbrella close already posts a
  human-visible `:white_check_mark: all children resolved` comment
  ([`orchestrator/stages/decomposition.py`](../orchestrator/stages/decomposition.py))
  and can carry the verdict inline. The PR-merged / rejected finalizations in
  `_drain_review_pr_terminals` post *no* comment today, so surfacing there means
  adding one minimal terminal line — a small, explicitly-new surface, not a
  pretend-existing one. Format mirrors Reharness's verdict:
  `:receipt: this issue: 7 agent runs · 214k tokens · $1.83 (est.)`, honouring
  `cost_source` (mark `(est.)` when any run's cost was table-estimated, `unknown`
  when a SKU was unpriced, exactly as `usage.py` already classifies).

### Cost & risks

- Zero new deps (`usage.py` already produces every field). The new surface is
  three pinned-state keys, a verdict line on the already-posted umbrella close
  comment, and one minimal new terminal comment on the PR-merged / rejected path.
- **Idempotency is the one thing to get right.** Because the increment rides the
  caller's single, existing `write_pinned_state` after a real agent exit — never
  a second writer — a straddling-tick re-entry that re-parses an existing session
  must not re-add: fold only on the paths that publish genuinely new work, not on
  a bare resume that produced no new exit to count. An `interrupted` run whose
  pinned state is intentionally *not* written (the shutdown-sweep contract)
  simply won't accrue — a slight, acceptable undercount on killed runs, and the
  analytics sink still has the ground truth. Unit-test the accumulator against a
  resume and an interrupted exit.
- Keep it a **meter, not a breaker.** Do *not* add a per-issue cost ceiling that
  parks — that is a control change chasing a pain we have not yet felt, and it
  needs the visibility to exist first. Ship the number; revisit a budget only if
  a real overspend shows up (open question below).

## Considered but rejected

- **The reasoning compiler itself / generated FSM topology.** Our label graph is
  hand-authored public contract that live issues carry. Generating it per task
  class is the opposite of the design and would throw away the "state is
  observable on github.com" property. This is the whole thesis, and it is out of
  scope by construction.
- **`skeleton.xml` IR, the 12 state types, and topology-derived dataflow
  wiring.** We have no compiled dataflow bus; "wiring" is the pinned JSON comment
  plus the issue thread, deliberately not a second compiled source of truth.
  The dataflow/definite-assignment half of Reharness's static analysis has *no
  analog* here — there is no scalar bus to run use-before-def over — which is why
  Proposal 1 borrows only the reachability half.
- **PRD + single approval gate / the three frontiers (`compile` / `amend` /
  `--from-session`).** The decomposer's single-vs-split decision plus operator
  relabels already provide the "human approves intent, not structure"
  checkpoint. A separate approved-PRD artifact is a new document class for a
  checkpoint we already have on the issue itself.
- **`evolve` / profile-guided self-improvement (self-heal, tool acquisition,
  skill refinement, utility-problem archiving).** This needs a persistent
  cross-run ledger and learning loop — precisely the stateful "future-proofing
  abstraction" `CLAUDE.md` forbids, and it fights our stateless-process design.
  The roadmap's *repo memory across issues* entry is the bounded, defensible
  slice of "carry something between runs"; the full evolve loop is not.
- **Synthesized tool registry (`tools/<cmdId>/`).** No concrete orchestrator
  pain: agents already get shell via bypass-sandbox flags. General tool
  acquisition without a named gap is exactly what the issue tells us to reject.
- **Provider abstraction layer (`src/runtime/providers.ts`: argv lowering, RPC
  framing, event normalization, add-a-backend-with-one-class).** `agents.py`
  already dispatches to `codex`/`claude`; a general adapter framework is surface
  the two-backend reality does not justify. (Distinct from Proposal 2, which is
  not an abstraction — it reuses the existing parser.)
- **The liftable `reharness/` bundle (`skeletons/`, `prds/`, `commands/`,
  `lib/`, `agents/`, `tools/`, `skills/`).** Our deliverable is a PR on GitHub;
  there is no compiled artifact to package and lift.
- **Runtime `--dry-run` that stubs agents/shell and walks the FSM.** The routing
  analog is already covered: the pytest suite drives every stage handler's
  routing against stubbed agent verdicts via the in-memory fakes
  (`tests/fakes.py`). A live-GitHub dry-run command would need synthetic issues
  and new surface for what tests already give us. The *static* graph slice is
  Proposal 1; the runtime command is not worth it.
- **`REHARNESS_RUN_RETENTION` bounded-disk pruning.** Per-issue worktrees are
  already force-removed on terminal cleanup; append-only analytics/event JSONL
  growth is an ops concern (`logrotate`), not orchestrator surface. Revisit only
  on a concrete disk-pressure report.
- **Secret redaction across traces/output/state.** Already present: agent/verify
  env scrubbing, prompt redaction (`tests/test_workflow_prompt_redaction.py`),
  and `_format_stderr_diagnostics` truncation. Nothing to borrow.
- **Provider-transient auto-retry with jittered backoff.** Tempting — a nightly
  provider 429/503 currently parks awaiting a human comment. But (a) the
  `codex`/`claude` CLIs plausibly handle provider backoff internally; (b)
  cleanly separating "transient" from "the agent is genuinely stuck" is the
  exact thing the machine deliberately punts on ("we cannot distinguish a real
  question from nothing-to-change by inspection"); (c) `MAX_RETRIES_PER_DAY` plus
  the awaiting-human resume already provides eventual recovery. Rejected as
  picky — but it is the *most* reconsider-able reject here (see open questions).
- **Per-state `timeoutMs` / `--param` structural knobs.** We already have
  `AGENT_TIMEOUT` / `REVIEW_TIMEOUT` / `VERIFY_TIMEOUT` and the round/retry caps.
  Per-state timeout tuning is finer granularity than the flow needs.

## Open questions

- **Cap-exhaustion tests for each cycle.** Proposal 1 documents the
  "every cycle is bounded by a cap" invariant but does not mechanically prove
  it. Is a per-loop synthetic exhaustion test (drive `validating ↔ fixing` to
  `MAX_REVIEW_ROUNDS` and assert the park) worth its weight, or does the
  existing per-handler cap coverage suffice?
- **Cost budget as a breaker.** If Proposal 2's meter reveals real per-issue
  overspend, a `MAX_ISSUE_COST_USD` park (parallel to `MAX_RETRIES_PER_DAY`) is
  the natural follow-on. Do not build it speculatively — wait for the number to
  show a pain.
- **Transient-blip parks.** If operators report that provider 429/503 blips
  routinely park issues overnight, revisit the rejected auto-retry — scoped
  narrowly to a small allowlist of provider-transient exit signatures classified
  in `agents.run_agent`, with a bounded in-tick backoff *before* the existing
  park, never touching the content-failure path.

## Sequencing

Land **Proposal 1 first**: it is a pure test addition with zero runtime risk,
and it hardens the public-contract graph *before* the roadmap's state-adding
work (`specifying`, architectural review, dynamic workflow) can silently orphan
a state. **Proposal 2 second**: it touches the hot `agent_exit` path and extends
the pinned-state schema, so it wants both the graph confidence Proposal 1 buys
and its own careful once-per-run idempotency test. Both are opt-in in spirit and
absent/off reduce to today's behavior.
