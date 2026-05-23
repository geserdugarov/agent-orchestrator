# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition stage handlers.

Covers the `decomposing` / `ready` / `blocked` / `umbrella` labels and
their stage-private helpers (decomposer session lookup, awaiting-human
resume, half-finished decomposition recovery, child issue creation,
dependency activation, and the DECOMPOSE kill-switch bailout).

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, `_handle_implementing`, the worktree plumbing, the drift /
manifest / messaging helpers re-exported into `workflow`) are reached
through the parent module via `from .. import workflow as _wf` at call
time. The compatibility surface tests rely on -- `patch.object(workflow,
"_foo")` -- has to keep working from inside the stage module too, so the
handlers must NOT direct-import these names from `workflow_drift` /
`workflow_messages` / `worktrees`; doing so would bind a stable
reference that test patches against `workflow.X` could not affect.
"""
from __future__ import annotations

from typing import Optional, Tuple

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..github import GitHubClient, PinnedState


def _read_decomposer_session(
    state: PinnedState,
) -> Tuple[str, str, tuple[str, ...], Optional[str]]:
    """Return (spec, backend, extra_args, decomposer_session_id) for an issue.

    Mirrors `_read_dev_session`: `spec` is the full configured agent
    command string the next run will use, returned so callers can
    persist it verbatim BEFORE invoking `run_agent` -- a fresh
    decomposer that produces a manifest without surfacing a session id
    (a backend hiccup in the JSONL output, an empty `-o` file) would
    otherwise leave `decomposer_agent` unset and a later
    `DECOMPOSE_AGENT` env flip could retarget the awaiting-human
    resume at a backend that never ran on this issue.

    Legacy bare-backend values (`"codex"` / `"claude"`) re-parse to
    `(backend, ())` and round-trip cleanly. When the issue has never
    been spawned, returns the current config's
    `(DECOMPOSE_AGENT_SPEC, DECOMPOSE_AGENT, DECOMPOSE_AGENT_ARGS, None)`.
    """
    stored = state.get("decomposer_agent")
    if stored:
        spec = str(stored)
        backend, args = config._parse_agent_spec("decomposer_agent", spec)
        sid = state.get("decomposer_session_id")
        return spec, backend, args, str(sid) if sid is not None else None
    return (
        config.DECOMPOSE_AGENT_SPEC,
        config.DECOMPOSE_AGENT,
        config.DECOMPOSE_AGENT_ARGS,
        None,
    )


def _resume_decomposer_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
) -> Optional[AgentResult]:
    """Resume the decomposer's locked-backend session with new comments.

    Returns the agent result, or None if there are no new comments since
    the last park (caller should return without writing state).

    Mirrors `_resume_developer_on_human_reply` but on the decomposer
    session. The backend is locked to whichever wrote
    `decomposer_session_id`; resuming across backends would need an
    inter-backend session bridge that does not exist.
    """
    from .. import workflow as _wf

    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    consumed_max = max(c.id for c in new_comments)
    state.set("last_action_comment_id", consumed_max)
    followup = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body}"
        for c in new_comments if c.body
    )
    wt = _wf._decompose_worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_decompose_worktree(spec, issue.number)
    _, decomposer_backend, decomposer_args, decomposer_sid = (
        _read_decomposer_session(state)
    )
    result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="decomposer",
        stage="decomposing",
        backend=decomposer_backend,
        prompt=followup,
        cwd=wt,
        resume_session_id=decomposer_sid,
        extra_args=decomposer_args,
        retry_count=state.get("retry_count"),
    )
    state.set("awaiting_human", False)
    return result


def _handle_decomposing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)

    # Track whether to keep the decomposer worktree past this tick. Set
    # True only in the dirty/commits park, where the operator may want to
    # inspect what the agent did. Every other exit (success or park)
    # cleans up via the finally below so the next consumer of this issue
    # number starts from current `origin/<base>`.
    keep_worktree = False
    try:
        # User-content drift FIRST. The spec requires "at the start of
        # every per-tick handler"; running this before the half-finished
        # recovery below is what stops the recovery branch from
        # finalizing to `blocked` / `umbrella` against a stale manifest
        # when the human edited the issue body during a crash window.
        # When drift IS detected we wipe the manifest tracking (children,
        # dep_graph, expected_children_count, umbrella) so the recovery
        # branch is bypassed and the fresh-spawn path below derives a
        # new manifest against the updated body. Previously-created
        # children are listed as orphans in the notice -- they remain
        # on GitHub but the orchestrator no longer tracks them.
        new_hash = _wf._detect_user_content_change(gh, issue, state)
        if new_hash is not None:
            orphans = list(state.get("children") or [])
            if orphans:
                orphan_list = ", ".join(f"#{n}" for n in orphans)
                notice = (
                    ":pencil2: issue content changed; re-running "
                    "decomposer against the updated body. The "
                    f"previously-tracked children ({orphan_list}) "
                    "will be ORPHANED -- the orchestrator no longer "
                    "tracks them; please close any that no longer "
                    "apply to the updated requirements."
                )
            else:
                notice = (
                    ":pencil2: issue content changed; re-running "
                    "decomposer against the updated body."
                )
            _wf._post_issue_comment(gh, issue, state, notice)
            state.set("user_content_hash", new_hash)
            # Drop only the SESSION id -- preserve `decomposer_agent`
            # (the locked role spec). Lock-on-first-spawn means a
            # mid-flight `DECOMPOSE_AGENT` env flip must not retarget
            # an in-flight issue at a different backend; the fresh
            # spawn below picks up the recorded spec via
            # `_read_decomposer_session`.
            state.set("decomposer_session_id", None)
            state.set("children", [])
            state.set("dep_graph", {})
            state.set("expected_children_count", None)
            state.set("umbrella", None)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            # Fall through: state is now clean (no children, no session),
            # so the half-finished recovery below is bypassed, the
            # awaiting-human branch is bypassed, and the fresh-spawn
            # branch runs the decomposer this tick.

        # Half-finished decomposition recovery. Two persistent markers
        # signal a prior tick crashed mid-split:
        #   * `expected_children_count` is written BEFORE any child is
        #     created, so a SIGKILL after `create_child_issue` returns
        #     but before the parent records the new child number leaves
        #     the parent with this marker AND zero recorded children
        #     while an orphan child issue exists on GitHub. Re-running
        #     the decomposer here would emit a different manifest and
        #     create duplicate children alongside the orphan.
        #   * `children` is written incrementally after each successful
        #     create + parent-state flush. Its presence covers a crash
        #     after at least one child was recorded.
        # Either marker present without the parent label having flipped
        # to `blocked` means we cannot safely respawn the decomposer.
        # Branch by whether the recorded count matches expectations:
        # equal -> finalize to `blocked`; less -> park awaiting human.
        # Legacy state from a deploy that pre-dates
        # `expected_children_count` still routes through the
        # `children`-only branch and finalizes.
        expected_raw = state.get("expected_children_count")
        children_recorded = state.get("children") or []
        if expected_raw is not None or children_recorded:
            if state.get("awaiting_human"):
                return
            if expected_raw is not None and len(children_recorded) < int(
                expected_raw
            ):
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposition crashed mid-way: "
                    f"{len(children_recorded)} of {expected_raw} children "
                    "recorded (an orphan child issue may exist on GitHub if "
                    "the crash landed between `create_child_issue` returning "
                    "and the parent state write); manual intervention needed "
                    "(close any partial children and re-decompose, or finish "
                    "creating the missing ones).",
                    reason="decomposition_crash",
                )
                gh.write_pinned_state(issue, state)
                return
            # Before finalizing to `blocked`, repair any child whose pinned
            # state was never seeded. A SIGKILL between the parent's
            # incremental `children` write and the child-state write at
            # the LAST child satisfies `len(children) == expected_children_count`
            # but leaves that child orphaned: no `parent_number`, and likely
            # already parked with `awaiting_human=True` by a prior
            # `_handle_blocked` tick that saw it as "unattributed blocked".
            # Without repair, the parent's later walk flips the orphan to
            # `ready`, but `_handle_implementing` reads the stale park and
            # sits waiting for a human reply that never comes.
            for child_number in children_recorded:
                try:
                    child_issue = gh.get_issue(int(child_number))
                    child_state = gh.read_pinned_state(child_issue)
                    if not child_state.get("parent_number"):
                        child_state.set("parent_number", issue.number)
                        if not child_state.get("created_at"):
                            child_state.set("created_at", _wf._now_iso())
                        child_state.set("awaiting_human", False)
                        child_state.set("park_reason", None)
                        gh.write_pinned_state(child_issue, child_state)
                except Exception:
                    _wf.log.exception(
                        "issue=#%s could not repair orphan child #%s during "
                        "decomposition recovery", issue.number, child_number,
                    )
                    _wf._park_awaiting_human(
                        gh, issue, state,
                        f"{config.HITL_MENTIONS} could not repair child "
                        f"#{child_number} during decomposition recovery "
                        "(seed `parent_number` on its pinned state); manual "
                        "intervention needed (check orchestrator logs).",
                        reason="child_seed_failed",
                    )
                    gh.write_pinned_state(issue, state)
                    return
            # `umbrella=True` is persisted alongside `expected_children_count`
            # before any child is created, so the recovery path here picks
            # it up and finalizes to `umbrella` instead of `blocked`. Without
            # this branch, a SIGKILL between the umbrella manifest's child
            # creation loop and the final label flip would resume as a
            # plain blocked parent and re-enter implementation after all
            # children resolved -- the opposite of what the manifest asked.
            finalize_label = (
                "umbrella" if state.get("umbrella") else "blocked"
            )
            gh.set_workflow_label(issue, finalize_label)
            gh.write_pinned_state(issue, state)
            return

        # DECOMPOSE kill-switch bailout. Every path below this point
        # spawns the decomposer (fresh or via the awaiting_human
        # resume), so an operator who restarts with DECOMPOSE=off after
        # `_handle_pickup` already labeled the issue `decomposing` --
        # or while it is parked there awaiting a human -- would still
        # see the disabled rollout create manifests and child issues.
        # Drop into the legacy implementing flow exactly as
        # `_handle_pickup` does on a freshly unlabeled issue. The
        # half-finished recovery above must keep running regardless of
        # the flag: abandoning orphan children (already on GitHub)
        # because new decompositions are now disabled would strand
        # work, which is not what a kill switch should do.
        if not config.DECOMPOSE:
            _wf._post_issue_comment(
                gh, issue, state,
                ":robot: decomposition is disabled; routing this issue "
                "to implementation.",
            )
            # Clear decomposer-side park state. Without this,
            # `_handle_implementing` reads `awaiting_human=True` and
            # tries to resume a dev session that was never spawned --
            # at best it stalls on `comments_after`, at worst the
            # follow-up text becomes the sole prompt instead of the
            # real implement prompt.
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            # Mark every comment visible at this transition as
            # "already consumed", mirroring `_handle_ready`'s ratchet.
            # `_handle_implementing` will read the full issue thread
            # via `_recent_comments_text` when it builds the implement
            # prompt, so the dev sees any decomposing-era human
            # feedback at spawn. Without this bump, the
            # validating->in_review watermark seed later sees those
            # same comments as fresh PR feedback (because they sit
            # AFTER the now-stale `last_action_comment_id` from the
            # decomposer-era park) and bounces the dev unnecessarily.
            # One-way ratchet so we never lower a higher prior value.
            latest = gh.latest_comment_id(issue)
            if isinstance(latest, int):
                prior = state.get("last_action_comment_id")
                if not isinstance(prior, int) or latest > prior:
                    state.set("last_action_comment_id", latest)
            gh.set_workflow_label(issue, "implementing")
            gh.write_pinned_state(issue, state)
            _wf._handle_implementing(gh, spec, issue)
            return

        # (User-content drift handled at the top of the try block above
        # so it runs BEFORE half-finished recovery -- otherwise the
        # recovery branch would finalize against a stale manifest when
        # the issue was edited during a crash window.)

        if state.get("awaiting_human"):
            result = _resume_decomposer_on_human_reply(gh, spec, issue, state)
            if result is None:
                # No human reply yet. Keep the worktree intact -- if a
                # prior tick parked on the dirty/commits reason, the
                # HITL message explicitly asks the operator to inspect
                # and reset it before resuming, and cleanup here would
                # silently delete that state on every subsequent poll.
                keep_worktree = True
                return
        else:
            if not _wf._check_and_increment_retry_budget(
                gh, issue, state, stage="decomposing"
            ):
                gh.write_pinned_state(issue, state)
                return
            wt = _wf._ensure_decompose_worktree(spec, issue.number)
            decomposer_spec, decomposer_backend, decomposer_args, _ = (
                _read_decomposer_session(state)
            )
            # Persist the spec BEFORE the spawn so a backend hiccup
            # that yields no `session_id` -- yet still produces a
            # manifest in the worktree or parks awaiting human -- does
            # not leave `decomposer_agent` unset. A later
            # `DECOMPOSE_AGENT` flip would otherwise retarget the next
            # awaiting-human resume at a backend that never ran on
            # this issue. Storing the parsed backend alone would also
            # strip configured CLI args on subsequent resumes.
            state.set("decomposer_agent", decomposer_spec)
            prompt = _wf._build_decompose_prompt(issue, _wf._recent_comments_text(issue))
            result = _wf._run_agent_tracked(
                gh, issue.number,
                agent_role="decomposer",
                stage="decomposing",
                backend=decomposer_backend,
                prompt=prompt,
                cwd=wt,
                extra_args=decomposer_args,
                retry_count=state.get("retry_count"),
            )
            if result.session_id:
                state.set("decomposer_session_id", result.session_id)

        state.set("last_agent_action_at", _wf._now_iso())

        if result.timed_out:
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer timed out after "
                f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
                reason="decomposer_timeout",
            )
            gh.write_pinned_state(issue, state)
            return

        # The decomposer is supposed to be read-only. If it committed or
        # left uncommitted changes, something has gone wrong (prompt
        # ignored, agent misbehaving, operator scratch). Park awaiting
        # human and KEEP the worktree past this tick so the operator can
        # inspect what the decomposer actually produced before resetting.
        wt = _wf._decompose_worktree_path(spec, issue.number)
        if _wf._has_new_commits(spec, wt) or _wf._worktree_dirty_files(wt):
            keep_worktree = True
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer left commits or uncommitted "
                "changes in the worktree, but it must be read-only. Reset the "
                "worktree before resuming.",
                reason="decomposer_dirty",
            )
            gh.write_pinned_state(issue, state)
            return

        last_msg = result.last_message or ""
        parsed, error = _wf._parse_manifest(last_msg)

        if parsed is None:
            # Either malformed manifest OR no manifest at all (question /
            # silence). Both park awaiting human; resume on the next
            # comment runs through the awaiting_human branch above.
            if error is not None:
                quoted = "> " + last_msg.strip().replace("\n", "\n> ")
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposer manifest invalid "
                    f"({error}); manual adjudication needed.\n\n"
                    f"_Last decomposer message:_\n\n{quoted}",
                    reason="decomposer_invalid_manifest",
                )
            else:
                stripped = last_msg.strip()
                raw = stripped or "(decomposer produced no final message)"
                quoted = "> " + raw.replace("\n", "\n> ")
                # Only attach stderr diagnostics on the silent path -- a
                # real content question from the decomposer doesn't need
                # the operator wading through subprocess noise.
                diag = (
                    "" if stripped
                    else _wf._format_stderr_diagnostics(result, "Decomposer")
                )
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposer needs your input to "
                    f"proceed:\n\n{quoted}{diag}",
                    reason="decomposer_silent" if not stripped else "decomposer_question",
                )
                if not stripped:
                    _wf.log.warning(
                        "issue=#%s decomposer produced no final message; "
                        "exit_code=%d timed_out=%s stderr_tail=%r",
                        issue.number, result.exit_code, result.timed_out,
                        _wf._stderr_log_tail(result),
                    )
            gh.write_pinned_state(issue, state)
            return

        if parsed["decision"] == "single":
            # `_parse_manifest` only checks the decision string for the
            # single branch, so `rationale` may be any JSON value (or
            # missing). Coerce non-strings to the placeholder rather than
            # crashing the handler at `.strip()` after the agent already ran.
            raw_rationale = parsed.get("rationale")
            if not isinstance(raw_rationale, str):
                raw_rationale = ""
            rationale = raw_rationale.strip() or "(no rationale provided)"
            _wf._post_issue_comment(
                gh, issue, state,
                f":mag: decomposer says this fits one context: {rationale}",
            )
            state.set("decomposed_at", _wf._now_iso())
            gh.set_workflow_label(issue, "ready")
            gh.write_pinned_state(issue, state)
            return

        # decision == "split". Crash-safe sequence:
        #   1. Persist `expected_children_count` BEFORE creating any
        #      child. The half-finished recovery uses this to tell a
        #      partial loop apart from a completed one.
        #   2. For each child: create the GitHub issue, then
        #      IMMEDIATELY record its number in parent state (before
        #      any further non-idempotent work). A SIGKILL between
        #      these two steps is unavoidable; persisting first means
        #      the worst case is an orphan child without seeded
        #      `parent_number`, not a duplicate child created by a
        #      decomposer respawn.
        #   3. Seed child pinned state. Failure here parks but parent
        #      state already records the child, so no respawn happens.
        #   4. After the loop: post the summary, label parent
        #      `blocked`. Activation (children blocked -> ready) only
        #      runs AFTER this final write, so a crash here cannot
        #      leave a runnable orphan child against a
        #      `decomposing`-labeled parent.
        children_manifest = parsed["children"]
        is_umbrella = bool(parsed.get("umbrella"))
        created: list[Tuple[int, dict]] = []
        dep_graph: dict[str, list[int]] = {}
        state.set("expected_children_count", len(children_manifest))
        # Persist the umbrella flag alongside the count so the half-finished
        # recovery path above can finalize to the right label after a
        # mid-loop SIGKILL. Always write it (including when False) so a
        # buggy state migration that left a stale True from a prior aborted
        # decomposition cannot survive into the recovery branch.
        state.set("umbrella", is_umbrella)
        gh.write_pinned_state(issue, state)
        for idx, child in enumerate(children_manifest):
            depends_on = list(child.get("depends_on") or [])
            try:
                new_issue = gh.create_child_issue(
                    title=child["title"],
                    body=child["body"],
                    parent_number=issue.number,
                    labels=["blocked"],
                )
            except Exception:
                _wf.log.exception(
                    "issue=#%s could not create child %d (%r)",
                    issue.number, idx, child.get("title"),
                )
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} could not create child issue "
                    f"index={idx} ({child.get('title')!r}); manual intervention "
                    "needed (check orchestrator logs).",
                    reason="child_create_failed",
                )
                gh.write_pinned_state(issue, state)
                return

            # Persist the child number on the parent BEFORE doing any
            # further work for this child. A SIGKILL between
            # `create_child_issue` returning and this write would leave
            # an orphan child on GitHub that the parent does not know
            # about; the next tick would re-spawn the decomposer and
            # create duplicates.
            created.append((new_issue.number, child))
            if depends_on:
                dep_graph[str(idx)] = depends_on
            state.set("children", [n for n, _ in created])
            if dep_graph:
                state.set("dep_graph", dep_graph)
            state.set("decomposed_at", _wf._now_iso())
            gh.write_pinned_state(issue, state)

            # Seed `parent_number` on the child. Mandatory: without
            # it `_handle_blocked` parks the child as "manual relabel
            # suspected" and that park leaves `awaiting_human=True`
            # behind even after the parent later flips the child's
            # label to `ready` -- the child's `_handle_implementing`
            # would then sit waiting for a human comment instead of
            # starting work.
            try:
                child_state = PinnedState()
                child_state.set("parent_number", issue.number)
                child_state.set("created_at", _wf._now_iso())
                gh.write_pinned_state(new_issue, child_state)
            except Exception:
                _wf.log.exception(
                    "issue=#%s could not seed pinned state on child #%d",
                    issue.number, new_issue.number,
                )
                # Parent already records the child (no duplicate
                # risk). Park so a human can either seed the child
                # manually or close it.
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} created child #{new_issue.number} "
                    f"({child.get('title')!r}) but could not seed its pinned "
                    "state with `parent_number`; manual intervention needed "
                    "(seed parent_number on the child or close it).",
                    reason="child_seed_failed",
                )
                gh.write_pinned_state(issue, state)
                return

        # children/dep_graph/decomposed_at are already durable from the
        # incremental writes in the loop above. Post the summary, flip
        # the parent label to `blocked` (or `umbrella` when the parent
        # has no implementation work of its own), and persist the new
        # orchestrator_comment_id. Activation (children blocked -> ready)
        # only runs AFTER this final write, so a crash here cannot leave
        # a runnable orphan child against a `decomposing`-labeled parent.
        summary = "\n".join(
            f"- #{n}: {child['title']}" for n, child in created
        )
        if is_umbrella:
            summary_intro = (
                f":bookmark_tabs: decomposer split this into {len(created)} "
                f"child issue(s); marking parent as `umbrella` (no "
                f"implementation of its own; will auto-resolve once every "
                f"child resolves):\n\n{summary}"
            )
            final_label = "umbrella"
        else:
            summary_intro = (
                f":bookmark_tabs: decomposer split this into {len(created)} "
                f"child issue(s):\n\n{summary}"
            )
            final_label = "blocked"
        _wf._post_issue_comment(gh, issue, state, summary_intro)
        gh.set_workflow_label(issue, final_label)
        gh.write_pinned_state(issue, state)

        # Activation: flip no-dep children from `blocked` to `ready`.
        # Best-effort -- if any flip fails the parent's `_handle_blocked`
        # walk handles it on the next tick (the walk treats a child with
        # no recorded deps as deps-satisfied).
        for idx, (child_number, _) in enumerate(created):
            if str(idx) in dep_graph:
                continue
            try:
                child_issue = gh.get_issue(child_number)
                gh.set_workflow_label(child_issue, "ready")
            except Exception:
                _wf.log.exception(
                    "issue=#%s could not flip child #%d to ready; the parent's "
                    "_handle_blocked walk will retry on the next tick",
                    issue.number, child_number,
                )
    finally:
        if not keep_worktree:
            _wf._cleanup_decompose_worktree(spec, issue.number)


def _handle_ready(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """`ready` is the entry point for an auto-created child or for a parent
    whose decomposer voted `single`. Both cases need the same pickup-state
    seeding the legacy `_handle_pickup` did before flipping to
    `implementing`, so the validating handoff watermark and the in_review
    legacy migration have an anchor comment they can key on.
    """
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    # User-content drift before implementation has started: route back to
    # decomposing so the manifest is re-derived against the new body. A
    # non-umbrella parent can reach `ready` after every child resolves
    # (`_handle_blocked`'s all-done branch flips `blocked` -> `ready`),
    # so the parent may STILL carry `children` / `dep_graph` /
    # `expected_children_count` from the prior manifest. Without clearing
    # those, the next `_handle_decomposing` tick's half-finished
    # recovery branch would fire and just flip the issue back to
    # `blocked` without re-running the decomposer. Route via
    # `_route_drift_to_decomposing` so the manifest tracking is wiped
    # alongside the locked decomposer session; the now-resolved children
    # are listed in the notice as orphans so the operator can close any
    # that no longer apply to the updated requirements.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        orphans = list(state.get("children") or [])
        _wf._route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
        gh.write_pinned_state(issue, state)
        return
    if state.get("pickup_comment_id") is None:
        if not state.get("created_at"):
            state.set("created_at", _wf._now_iso())
        pickup = _wf._post_issue_comment(
            gh, issue, state,
            ":robot: orchestrator picking this up; starting implementation.",
        )
        pickup_id = getattr(pickup, "id", None)
        if pickup_id is not None:
            state.set("pickup_comment_id", int(pickup_id))
    # Mark every comment visible right now as "already consumed". For a
    # parent that came through `decomposing` / `blocked`, `pickup_comment_id`
    # was anchored on the original "decomposing" comment, so any human
    # feedback posted while children were resolving sits AFTER pickup and
    # would be classified as post-pickup, unconsumed feedback by the
    # in_review watermark seed. The implementer reads the full thread via
    # `_recent_comments_text` at spawn, so by the time the PR reaches
    # `in_review` those comments have been incorporated; replaying them
    # would resume the dev and bounce the PR back to validating instead
    # of allowing merge. Bumping `last_action_comment_id` lets
    # `_seed_watermark_past_self`'s `consumed_through` walk advance past
    # them. The next park (or the validating handoff) will overwrite this
    # value, so it's a transient marker for the in-progress handoff only.
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get("last_action_comment_id")
        if not isinstance(prior, int) or latest > prior:
            state.set("last_action_comment_id", latest)
    gh.set_workflow_label(issue, "implementing")
    gh.write_pinned_state(issue, state)
    _wf._handle_implementing(gh, spec, issue)


def _handle_blocked(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children to decide whether the parent unblocks (or one of the
    children unblocks).

    The orchestrator's parallel tick path (see
    `workflow._FAMILY_AWARE_LABELS`) submits the whole family-aware
    bucket as a single drain task on one worker thread, so only one of
    `decomposing`, `blocked`, or `umbrella` runs at a time within a
    tick -- even when other issues fan out across worker threads. A
    child's `in_review -> done` label flip and this tick therefore
    still cannot race the parent's child-state writes; we read each
    child's current label fresh here. Issues outside the family-aware
    bucket (`implementing`, `validating`, `in_review`,
    `resolving_conflict`) may run concurrently alongside, but their
    handlers do not write across parent/child boundaries.
    """
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    children = state.get("children") or []

    # User-content drift detection. The hash baseline is initialized by
    # `_detect_user_content_change` itself on the first encounter, so a
    # legacy `blocked` issue still missing the field is durably seeded
    # here (via the helper's own `write_pinned_state`) rather than
    # silently absorbing the next edit as the new baseline. Per the spec
    # ("Before validating: route back to decomposing"), both parent and
    # child cases route to decomposing -- silently persisting the new
    # baseline for a child would let `_handle_ready` later see a matching
    # hash and skip the re-decomposer, even when the edited body now
    # needs splitting. Parents with in-flight children list those
    # children as orphans in the notice (the new manifest may overlap
    # with them; the operator closes the obsolete ones manually).
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        _wf._route_drift_to_decomposing(
            gh, issue, state, new_hash, list(children),
        )
        gh.write_pinned_state(issue, state)
        return

    if not children:
        # A blocked issue with `parent_number` recorded is a child waiting
        # on a sibling. The parent's `_handle_blocked` walks the dep graph
        # and flips the child to `ready` when its dependencies finish; this
        # tick has nothing to do. Without this branch the polling loop
        # would route every `blocked` child here, treat it as a parent
        # missing its `children` list, and park it as "manual relabel
        # suspected" -- leaving `awaiting_human=True` on the child even
        # after the parent later relabels it `ready`.
        if state.get("parent_number"):
            return
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `blocked` without recorded children; "
            "manual relabel suspected.",
            reason="blocked_no_children",
        )
        gh.write_pinned_state(issue, state)
        return

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)

    rejected = [n for n, lbl in child_labels.items() if lbl == "rejected"]
    if rejected:
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) rejected: "
            f"{', '.join(f'#{n}' for n in rejected)}; "
            "decide whether to re-decompose or close.",
            reason="child_rejected",
        )
        gh.write_pinned_state(issue, state)
        return

    # A child closed manually (e.g. via the GitHub UI) before reaching
    # `in_review` is invisible to `list_pollable_issues`, which only
    # sweeps closed issues for `in_review` (the externally-merged
    # path). Its workflow label stays frozen at whatever it was at
    # close -- ready/blocked/implementing/validating, or none at all
    # -- so without this branch the parent would read the stale label,
    # neither the rejected nor the all-done branch would fire, and the
    # parent would wait forever for a child that is gone. Treat it
    # like a rejected child so the operator can adjudicate. `in_review`
    # is intentionally allowed: a state=closed/label=in_review child is
    # the externally-merged transient that the closed-in_review sweep
    # finalizes on the next tick, NOT a manual override.
    manually_closed = [
        n for n, ci in child_issues.items()
        if getattr(ci, "state", "open") == "closed"
        and child_labels.get(n) not in ("done", "rejected", "in_review")
    ]
    if manually_closed:
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
            f"`done` or `rejected`: "
            f"{', '.join(f'#{n}' for n in manually_closed)}; "
            "decide whether to re-decompose or close.",
            reason="child_manually_closed",
        )
        gh.write_pinned_state(issue, state)
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        _wf._post_issue_comment(
            gh, issue, state,
            ":white_check_mark: all children resolved; ready for "
            "implementation.",
        )
        # Clear any stale park left by a prior `rejected`-child tick: the
        # operator may have re-implemented the rejected child since, and
        # the parent now reaches `ready` legitimately. Without this clear,
        # `awaiting_human=True` survives into `_handle_implementing`,
        # which would route through `_resume_developer_on_human_reply`
        # and either replay long-stale comments or sit silent until a new
        # human reply arrives -- instead of just starting the parent's
        # implementation.
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        gh.set_workflow_label(issue, "ready")
        gh.write_pinned_state(issue, state)
        return

    # Walk children: any `blocked` child whose recorded dependencies are
    # all `done` gets relabeled `ready`. A child with no recorded deps
    # also flips (vacuous all-done over an empty list) -- this recovers
    # any no-dep child that the decomposer's same-tick activation step
    # left as `blocked` (network blip, label-flip failure, etc.).
    dep_graph = state.get("dep_graph") or {}
    relabeled = False
    for idx, child_number in enumerate(children):
        cn = int(child_number)
        if child_labels.get(cn) != "blocked":
            continue
        deps = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(children[int(d)]) for d in deps if int(d) < len(children)
        ]
        if all(child_labels.get(dn) == "done" for dn in dep_numbers):
            gh.set_workflow_label(child_issues[cn], "ready")
            relabeled = True
    if relabeled:
        gh.write_pinned_state(issue, state)


def _handle_umbrella(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children on an umbrella parent that has no implementation of
    its own.

    Mirrors `_handle_blocked` for the rejected/manually-closed checks and
    the dep-graph activation walk, but the all-done branch resolves the
    umbrella to `done` and closes the issue instead of flipping it to
    `ready` -- there is no implementation pass for an umbrella, so the
    only terminal path is "every child resolved -> close".
    """
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)

    # User-content drift detection. An umbrella parent NEVER enters
    # implementation -- it just closes when every child resolves -- so a
    # body edit cannot be picked up by any later stage's drift check.
    # Per the spec ("Before validating: route back to decomposing"), the
    # umbrella is routed back to decomposing so the new manifest is
    # re-derived against the updated body. The previously-tracked
    # children become orphans on GitHub (the orchestrator no longer
    # tracks them); the notice lists them explicitly so the operator can
    # close any that no longer apply. Without this route-back, an
    # edited umbrella would silently close to `done` against the stale
    # manifest once the old children finished.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        orphans = list(state.get("children") or [])
        _wf._route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
        gh.write_pinned_state(issue, state)
        return

    children = state.get("children") or []
    if not children:
        # An umbrella with no recorded children is corrupt state (the
        # decomposer only applies the umbrella label after creating
        # children), but still surface to a human rather than silently
        # closing an issue with no aggregated work.
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `umbrella` without recorded children; "
            "manual relabel suspected.",
            reason="umbrella_no_children",
        )
        gh.write_pinned_state(issue, state)
        return

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)

    rejected = [n for n, lbl in child_labels.items() if lbl == "rejected"]
    if rejected:
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) rejected: "
            f"{', '.join(f'#{n}' for n in rejected)}; "
            "decide whether to re-decompose or close.",
            reason="child_rejected",
        )
        gh.write_pinned_state(issue, state)
        return

    manually_closed = [
        n for n, ci in child_issues.items()
        if getattr(ci, "state", "open") == "closed"
        and child_labels.get(n) not in ("done", "rejected", "in_review")
    ]
    if manually_closed:
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
            f"`done` or `rejected`: "
            f"{', '.join(f'#{n}' for n in manually_closed)}; "
            "decide whether to re-decompose or close.",
            reason="child_manually_closed",
        )
        gh.write_pinned_state(issue, state)
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        _wf._post_issue_comment(
            gh, issue, state,
            ":white_check_mark: all children resolved; closing umbrella issue.",
        )
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        state.set("umbrella_resolved_at", _wf._now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        try:
            issue.edit(state="closed")
        except Exception:
            _wf.log.exception(
                "issue=#%s could not close umbrella after children done",
                issue.number,
            )
        return

    # Same dep-graph activation walk as `_handle_blocked`: an umbrella's
    # children can still depend on each other, and a no-dep child stuck
    # at `blocked` after a same-tick activation hiccup needs to be
    # rescued here.
    dep_graph = state.get("dep_graph") or {}
    relabeled = False
    for idx, child_number in enumerate(children):
        cn = int(child_number)
        if child_labels.get(cn) != "blocked":
            continue
        deps = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(children[int(d)]) for d in deps if int(d) < len(children)
        ]
        if all(child_labels.get(dn) == "done" for dn in dep_numbers):
            gh.set_workflow_label(child_issues[cn], "ready")
            relabeled = True
    if relabeled:
        gh.write_pinned_state(issue, state)
