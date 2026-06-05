# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting stage handler.

The documenting stage runs exactly once per reviewer-approval handoff,
between reviewer approval and `in_review`: after the reviewer agent
emits `VERDICT: APPROVED` and `_handle_validating` finishes the
local-verify + squash + watermark seed, it relabels to `documenting`.
The docs pass commits any README / docs edits, pushes them, and
advances to `in_review`. The `plans/` tree and roadmap entries are
deliberately out of scope: those are working notes owned by humans, so
the docs prompt instructs the agent to compare only against `README.md`
and the `docs/` tree. A PR can therefore visit `documenting`
more than once over its life: if PR feedback later bounces the issue
to `fixing` and the dev pushes a fix, the next reviewer approval
triggers another final-docs pass before the next `in_review` handoff.
There is no pre-approval entry: every `_handle_implementing` PR open,
every pushed dev fix in `_handle_validating` / `_handle_fixing` /
`_handle_in_review`'s drift exit, and every `_handle_resolving_conflict`
pushed exit hand straight back to `validating` so the reviewer re-runs
against the new branch.

Locking and session semantics mirror `implementing`'s dev role: the
documentation pass operates AS the developer (it commits to the dev's
branch), so it shares the dev session id and backend recorded in pinned
state. A locked-backend resume is used for any human reply that
follows a park.

Outcomes the handler distinguishes:
  * A `docs:` commit landed on the worktree -> push + advance to
    `in_review`.
  * The agent emitted the explicit `DOCS: NO_CHANGE` marker against a
    remote-clean head -> persist the verdict, post a one-liner, advance
    to `in_review` without pushing.
  * No commit and no marker -> park awaiting human via `_on_question`.
  * Timeout / dirty worktree / push failure -> park with the same
    `park_reason` tokens implementing and validating use.
  * User-content drift mid-hop -> the prior approval was for stale
    requirements, so the handler resets `review_round=0` and relabels
    back to `validating` without spawning the docs agent. The reviewer
    re-evaluates the updated body on the next tick.

Restart idempotency: on re-entry the helper reuses the existing PR
worktree. If the worktree carries commits ahead of `<remote>/<branch>`
from a previous tick whose push failed, those commits are pushed and
the issue advances without re-spawning the agent.

Open `documenting` issues touch only their own pinned state and
worktree, so the label is deliberately NOT listed in
`workflow._FAMILY_AWARE_LABELS` and `tick()` routes it through the
fan-out bucket.

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, the worktree plumbing, the docs prompt + verdict parser
re-exported into `workflow`) are reached through the parent module via
`from .. import workflow as _wf` at call time. Tests patch the
compatibility surface as `patch.object(workflow, "_foo")`, so the
handler must NOT direct-import those names from
`workflow_messages` / `worktrees`; binding a stable reference would
defeat the patch.
"""
from __future__ import annotations

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..github import GitHubClient, PinnedState


def _ratchet_in_review_watermark_for_final_docs(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Ratchet `pr_last_comment_id` past issue-thread comments the docs
    pass already consumed during the final-docs hop.

    During documenting's awaiting-human resume the handler advances
    `last_action_comment_id` past the human reply it fed into the
    `_build_documentation_prompt` resume. The final-docs handoff then
    relabels to `in_review`, which scans `comments_after(issue,
    pr_last_comment_id)` and falls back to `last_action_comment_id`
    only when `pr_last_comment_id is None`. Without this ratchet a
    `pr_last_comment_id` validating seeded BEFORE the human's reply
    keeps the older value, the consumed reply replays as fresh PR
    feedback, and in_review bounces the issue to `fixing` over work
    the dev has already addressed.

    Reuse `_latest_pr_comment_ids` (the same seed-walk validating uses
    at its approval handoff) so a PR-conversation comment with id
    between the prior `pr_last_comment_id` and the consumed-through
    threshold is NOT swallowed -- the walk stops at the first unread
    non-orchestrator comment on either surface. `consumed_through` is
    applied to the issue thread only inside the walk, which is what
    keeps PR-conversation feedback visible to in_review's
    fresh-feedback scan. Ratchets via `max` so a previous in_review
    tick's higher watermark is never regressed.

    A PR fetch failure is treated as best-effort: log and skip, so the
    docs handoff itself still advances. In the worst case in_review
    will route to `fixing` and the rescan there is debounced and
    correct on its own.
    """
    from .. import workflow as _wf

    pr_number = state.get("pr_number")
    if pr_number is None:
        return
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception as e:
        _wf.log.warning(
            "issue=#%s could not fetch PR #%s to ratchet "
            "`pr_last_comment_id` on the final-docs handoff: %s",
            issue.number, pr_number, e,
        )
        return

    candidate, _ = _wf._latest_pr_comment_ids(gh, issue, pr, state)
    prev_wm = state.get("pr_last_comment_id")
    if isinstance(prev_wm, int):
        candidate = (
            prev_wm if candidate is None
            else max(candidate, prev_wm)
        )
    if candidate is None:
        return
    state.set("pr_last_comment_id", candidate)


def _advance_after_docs_push(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Route the issue forward after a successful docs push.

    Advance to `in_review` -- the approval comment, squash comment, and
    PR watermarks set by validating remain on state untouched, with the
    in-review issue-comment watermark ratcheted past anything the
    awaiting-human resume already consumed.
    """
    _ratchet_in_review_watermark_for_final_docs(gh, issue, state)
    gh.set_workflow_label(issue, "in_review")


def _advance_after_docs_no_change(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Route the issue forward after a clean no-change docs verdict.

    No commit landed, so the PR head is unchanged. Ratchet the in-review
    issue-comment watermark past any issue-thread reply the
    awaiting-human resume already consumed, and advance to `in_review`.
    """
    _ratchet_in_review_watermark_for_final_docs(gh, issue, state)
    gh.set_workflow_label(issue, "in_review")


def _handle_documenting(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # External merge short-circuit: if the PR was merged before the docs
    # pass ran, finalize to `done` rather than fetching the branch and
    # running the documenting agent against an already-landed PR.
    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return

    # Closed-issue counterpart: the closed-`documenting` sweep yields
    # issues a human closed without a merged PR. Flip to `rejected` so
    # the docs agent does not run against a closed issue.
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return

    if pr_number is None:
        # Documenting only runs against an existing PR worktree.
        # Without a pinned `pr_number` we cannot anchor on the dev's
        # branch and must not branch off the base (that would orphan
        # the docs commit from the implementing PR). Park once and
        # let the operator relabel; idempotency by `awaiting_human`
        # mirrors `_handle_in_review`'s missing-pr-number guard.
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `documenting` without a pinned "
            "`pr_number`; the documenting stage runs against an existing "
            "PR worktree. Relabel back to `implementing` (the dev's PR "
            "opens there) after fixing.",
            reason="missing_pr_number",
        )
        gh.write_pinned_state(issue, state)
        return

    branch = _wf._branch_name(issue.number)

    # User-content drift: a human edited the issue title/body while the
    # final-docs hop was in flight. The reviewer approved the OLD
    # requirements, so the docs pass would be running against a body the
    # reviewer never saw. Mirror `_handle_in_review`'s drift invalidation:
    # reset `review_round=0`, post the notice, mark issue-thread comments
    # consumed, refresh the baseline hash, and relabel to `validating` so
    # the reviewer re-evaluates the updated body on the next tick. Do NOT
    # spawn the docs agent: the prior approval is gone and a docs commit
    # on top would just need to be re-reviewed alongside any impl change.
    #
    # If a recovered local docs commit is sitting in the worktree (a
    # prior tick committed but parked before the push landed -- ahead
    # > 0 vs. `<remote>/<branch>`), DISCARD it before handing back to
    # validating. The commit was authored against the OLD body, and
    # leaving it on disk would let the next final-docs tick's
    # recovered-commit shortcut push it without ever spawning a fresh
    # docs agent against the new requirements -- especially under
    # `SQUASH_ON_APPROVAL=off`, where the reviewer-approved head is
    # the dev's PR head (no rewrite gap), so the recovered docs
    # commit applies cleanly on top of the next approval. Reset to
    # `<remote>/<branch>` after a successful fetch so the next
    # approved round starts from the actual PR head. On fetch
    # failure, park instead of relabeling -- a stale local commit
    # silently riding into the next approval is worse than parking.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    fresh_drift = new_hash is not None
    pending_unwind = bool(state.get("docs_drift_unwind_pending"))
    # If a prior tick's drift unwind couldn't finish (the worktree
    # reconcile failed and parked) and nothing fresh has happened
    # since, stay silent. The fast-path check here means the parked
    # state survives operator inspection without re-posting the same
    # park comment every tick. Operator unpark (`awaiting_human=False`)
    # OR new human comments fall through to the unwind-retry block
    # below, which idempotently retries the reconcile + relabel.
    if pending_unwind and not fresh_drift and state.get("awaiting_human"):
        last_action_id = state.get("last_action_comment_id")
        if not gh.comments_after(issue, last_action_id):
            return
    if fresh_drift or pending_unwind:
        if fresh_drift:
            state.set("user_content_hash", new_hash)
            _wf._post_issue_comment(
                gh, issue, state,
                ":pencil2: issue body changed; routing back to "
                "`validating` so the reviewer re-evaluates the "
                "updated requirements.",
            )
            _wf._mark_drift_comments_consumed(gh, issue, state)
        # Set/keep the drift-unwind sentinel. The marker survives
        # every park inside this block, so an operator unpark or a
        # later human comment (without a fresh drift) re-enters this
        # block on the next tick and retries the reconcile + relabel.
        # The marker is cleared ONLY on the success path that
        # relabels to `validating`; without it, an operator unpark on
        # a failed reconcile would fall through to the normal flow
        # below and (via the recovered-commit shortcut or a fresh
        # docs spawn) advance to `in_review` against the OLD body --
        # skipping the required `validating` re-review.
        state.set("docs_drift_unwind_pending", True)
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        # Clear `review_round` BEFORE any fallible cleanup (fetch /
        # reset). Drift means the prior reviewer approval is stale
        # regardless of whether the on-disk reset succeeds, so the
        # round counter must drop now. If we park on fetch failure
        # below, an operator unpark or manual relabel must not be able
        # to ride the stale approval into a new final-docs handoff that
        # skips the required re-review.
        state.set("review_round", 0)
        wt = _wf._worktree_path(spec, issue.number)
        if wt.exists():
            fetch_branch = _wf._authed_fetch(
                spec,
                f"+refs/heads/{branch}:"
                f"refs/remotes/{spec.remote_name}/{branch}",
                cwd=wt,
            )
            if fetch_branch.returncode != 0:
                _wf.log.error(
                    "issue=#%d documenting drift fetch failed: %s",
                    issue.number, (fetch_branch.stderr or "").strip(),
                )
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} `git fetch "
                    f"{spec.remote_name} {branch}` failed while routing "
                    "documenting drift back to `validating`; the local "
                    "worktree may carry an unpushed docs commit against "
                    "the OLD body -- see orchestrator logs.",
                    reason="fetch_failed",
                )
                state.set("park_reason", "fetch_failed")
                gh.write_pinned_state(issue, state)
                return
            # Run the ahead/behind probe inline so a probe failure is
            # distinguishable from a real "in sync" result.
            # `_branch_ahead_behind` swallows git errors as `(0, 0)`,
            # which would silently let an unpushed local docs commit
            # against the OLD body survive into the next final-docs
            # hop's recovered-commit shortcut. Use the same git
            # invocation that helper uses but check the exit code +
            # parse here.
            probe = _wf._git_hardened(
                "rev-list", "--left-right", "--count",
                f"refs/remotes/{spec.remote_name}/{branch}...HEAD",
                cwd=wt,
            )
            ahead = None
            behind = None
            if probe.returncode == 0:
                parts = (probe.stdout or "").strip().split()
                if len(parts) == 2:
                    try:
                        behind = int(parts[0])
                        ahead = int(parts[1])
                    except ValueError:
                        behind = None
                        ahead = None
            if ahead is None or behind is None:
                _wf.log.error(
                    "issue=#%d documenting drift ahead/behind probe "
                    "failed (rc=%s stderr=%s stdout=%s)",
                    issue.number, probe.returncode,
                    (probe.stderr or "").strip(),
                    (probe.stdout or "").strip(),
                )
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} could not probe local vs. "
                    f"`{spec.remote_name}/{branch}` while routing "
                    "documenting drift back to `validating`; the local "
                    "worktree may carry an unpushed docs commit against "
                    "the OLD body -- see orchestrator logs.",
                    reason="worktree_reset_failed",
                )
                state.set("park_reason", "worktree_reset_failed")
                gh.write_pinned_state(issue, state)
                return
            # Also reconcile uncommitted edits: a prior docs run may
            # have edited files without committing (parked via
            # `_on_dirty_worktree` / `_on_question` / `agent_timeout`)
            # before the body edit landed. `_worktree_dirty_files`
            # surfaces both modified-tracked AND untracked paths, so
            # treat any non-empty list as a cleanup trigger. Without
            # this the dirty docs edits would ride into the next
            # reviewer round under the new body.
            #
            # And reconcile when the remote PR head moved past local
            # HEAD (`behind > 0`): the reviewer must re-evaluate the
            # actual PR head, not a stale local snapshot of an older
            # commit. Without the reset, the next reviewer round
            # would `git diff` against the un-fetched local HEAD and
            # silently miss commits the remote already has.
            dirty = _wf._worktree_dirty_files(wt)
            if ahead > 0 or behind > 0 or dirty:
                reset = _wf._git_hardened(
                    "reset", "--hard",
                    f"{spec.remote_name}/{branch}",
                    cwd=wt,
                )
                if reset.returncode != 0:
                    _wf.log.error(
                        "issue=#%d documenting drift reset failed "
                        "(rc=%s stderr=%s)",
                        issue.number, reset.returncode,
                        (reset.stderr or "").strip(),
                    )
                    _wf._park_awaiting_human(
                        gh, issue, state,
                        f"{config.HITL_MENTIONS} `git reset --hard "
                        f"{spec.remote_name}/{branch}` failed while "
                        "routing documenting drift back to "
                        "`validating`; the local worktree still "
                        "carries docs work against the OLD body -- "
                        "see orchestrator logs.",
                        reason="worktree_reset_failed",
                    )
                    state.set("park_reason", "worktree_reset_failed")
                    gh.write_pinned_state(issue, state)
                    return
                # `git reset --hard` does not remove untracked files,
                # so any untracked docs edits would still survive.
                # `git clean -fd` removes them. The `-d` also clears
                # untracked directories, which matters when the docs
                # agent created new under-`docs/` subdirs that the
                # reviewer never approved.
                clean = _wf._git_hardened(
                    "clean", "-fd", cwd=wt,
                )
                if clean.returncode != 0:
                    _wf.log.error(
                        "issue=#%d documenting drift clean failed "
                        "(rc=%s stderr=%s)",
                        issue.number, clean.returncode,
                        (clean.stderr or "").strip(),
                    )
                    _wf._park_awaiting_human(
                        gh, issue, state,
                        f"{config.HITL_MENTIONS} `git clean -fd` "
                        "failed while routing documenting drift back "
                        "to `validating`; the local worktree may "
                        "still carry untracked docs files against "
                        "the OLD body -- see orchestrator logs.",
                        reason="worktree_reset_failed",
                    )
                    state.set("park_reason", "worktree_reset_failed")
                    gh.write_pinned_state(issue, state)
                    return
        # Reconcile succeeded (or the worktree didn't exist): the
        # drift unwind is complete, clear the sentinel and relabel.
        state.set("docs_drift_unwind_pending", False)
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    # Already-parked, no-new-input fast path: when `awaiting_human` is
    # set and no human comment has arrived since the park (and drift
    # above did not clear the flag), there is nothing to act on. Skip
    # the fetch + ahead/behind check entirely so a transient failure
    # mode (fetch_failed / diverged_branch) does NOT re-post its park
    # comment every tick -- non-recoverable parks (agent_question /
    # dirty_worktree / agent_silent) likewise stay silent until a
    # human reply. Validating uses the same shape via its
    # transient-park recovery branch; documenting has no transient
    # recovery yet, so the early return alone is enough.
    if state.get("awaiting_human"):
        # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
        # the `_sync_pr_worktree_to_base` retry loop -- the operator's
        # new comment is the "retry the rebase" signal, NOT a
        # documenting-stage trigger. Stay silent so the refresh keeps
        # ownership of the comment.
        if state.get("park_reason") in _wf._AUTO_REBASE_PARK_REASONS:
            return
        last_action_id = state.get("last_action_comment_id")
        if not gh.comments_after(issue, last_action_id):
            return

    wt = _wf._ensure_pr_worktree(spec, issue.number)

    # Refresh `<remote>/<branch>` BEFORE the ahead/behind check. A stale
    # local remote-tracking ref would mis-classify a "remote moved out
    # from under us" situation as in-sync, and the eventual
    # `_push_branch` (which uses `--force-with-lease` against the local
    # view of the remote) would clobber the real PR head. Mirrors the
    # fetch-then-check pattern in `_handle_resolving_conflict`.
    fetch_branch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch_branch.returncode != 0:
        _wf.log.error(
            "issue=#%d documenting branch fetch failed: %s",
            issue.number, (fetch_branch.stderr or "").strip(),
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `git fetch {spec.remote_name} "
            f"{branch}` failed during documenting; see orchestrator logs.",
            reason="fetch_failed",
        )
        # `_park_awaiting_human` clears `park_reason` by contract; re-set
        # the durable tag so future ticks / dashboards can branch on it.
        state.set("park_reason", "fetch_failed")
        gh.write_pinned_state(issue, state)
        return

    ahead, behind = _wf._branch_ahead_behind(spec, wt, branch)
    if behind > 0:
        # Stale or diverged worktree. The reviewer's PR head has commits
        # we never saw, so pushing local state (even a clean recovery
        # push) would overwrite them. Refuse to act -- the same shape
        # `_handle_resolving_conflict`'s diverged-branch guard uses.
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} worktree on `{branch}` is {ahead} "
            f"ahead and {behind} behind `{spec.remote_name}/{branch}`; "
            "refusing to push a stale documenting branch over the "
            "real PR head. Manual intervention needed.",
            reason="diverged_branch",
        )
        state.set("park_reason", "diverged_branch")
        gh.write_pinned_state(issue, state)
        return

    if state.get("awaiting_human"):
        # Rerun the FULL documentation prompt on every awaiting-human
        # resume. The generic `_resume_developer_on_human_reply` helper
        # builds the followup from ONLY the new human comments, which
        # is the right shape for implementing/validating (the dev has
        # an in-context docs spec already) but wrong for documenting:
        # a `fetch_failed` / `agent_timeout` / `agent_silent` resume
        # may be the FIRST time this session sees the docs-stage
        # instructions (the DOCS: NO_CHANGE marker, what files to
        # inspect, what to commit). Without those, the dev could
        # emit a stray `DOCS: NO_CHANGE` it learned from an earlier
        # spawn and the issue would advance to validating without
        # ever running a real docs pass. `_build_documentation_prompt`
        # quotes the issue body AND the full conversation via
        # `_recent_comments_text`, so the human's latest reply is
        # naturally included.
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        if not new_comments:
            return
        consumed_max = max(c.id for c in new_comments)
        state.set("last_action_comment_id", consumed_max)
        # Anchor `before_sha` from the just-fetched PR worktree BEFORE
        # the resume so the post-spawn check below sees a real
        # difference if (and only if) the resumed dev produced a new
        # commit. `_resume_dev_with_text`'s `_ensure_worktree` fallback
        # is harmless here because we already restored the PR-anchored
        # worktree above.
        before_sha = _wf._head_sha(wt)
        # Persist `docs_checked_sha` BEFORE the spawn for the same
        # reason the fresh-spawn branch does: a no-change verdict on
        # this resume relies on this watermark to identify what
        # commit the dev confirmed.
        state.set("docs_checked_sha", before_sha or "")
        prompt = _wf._build_documentation_prompt(
            spec, issue, _wf._recent_comments_text(issue),
        )
        wt, result = _wf._resume_dev_with_text(
            gh, spec, issue, state, prompt,
        )
        recovered = False
    elif ahead > 0:
        # Recovered worktree: a previous tick committed docs but
        # crashed before the push. Build a synthetic result and fall
        # through to the unified commit/dirty/push branch so an
        # uncommitted file left alongside the recovered commit parks
        # via `_on_dirty_worktree` instead of being silently dropped
        # by the push (which only ships staged work). A drift event
        # this tick would have routed back to `validating` above
        # before reaching this branch, so the recovered commit is
        # always against the still-valid approved body.
        _wf.log.info(
            "issue=#%d documenting: %d recovered docs commit(s); "
            "skipping agent spawn and pushing",
            issue.number, ahead,
        )
        _, _, _, dev_sid = _wf._read_dev_session(state)
        result = AgentResult(
            session_id=dev_sid,
            last_message=(
                "(orchestrator restart: pushing previously committed docs)"
            ),
            exit_code=0,
            timed_out=False,
            stdout="",
            stderr="",
        )
        # Empty `before_sha` makes the post-spawn check below treat
        # the recovered HEAD as a fresh commit. `docs_checked_sha` is
        # updated to the recovered HEAD on a successful push.
        before_sha = ""
        recovered = True
    else:
        before_sha = _wf._head_sha(wt)
        state.set("docs_checked_sha", before_sha or "")
        dev_spec, dev_backend, dev_args, dev_sid = (
            _wf._read_dev_session(state)
        )
        # Persist the spec so a backend hiccup that yields no session
        # id still leaves a durable role-identity record; matches
        # `_handle_implementing`'s fresh-spawn branch.
        state.set("dev_agent", dev_spec)
        prompt = _wf._build_documentation_prompt(
            spec, issue, _wf._recent_comments_text(issue),
        )
        result = _wf._run_agent_tracked(
            gh, issue.number,
            agent_role="developer",
            stage="documenting",
            backend=dev_backend,
            prompt=prompt,
            cwd=wt,
            agent_spec=dev_spec,
            resume_session_id=dev_sid,
            extra_args=dev_args,
            review_round=state.get("review_round"),
            retry_count=state.get("retry_count"),
        )
        if result.session_id:
            state.set("dev_session_id", result.session_id)
        state.set("branch", branch)
        recovered = False

    state.set("last_agent_action_at", _wf._now_iso())

    if result.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after "
            f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            reason="agent_timeout",
        )
        # Mirror `_handle_dev_fix_result`'s tagging: a documenting timeout
        # is recoverable in principle (the docs prompt is idempotent and a
        # later tick can rerun it), so persist a transient reason so
        # dashboards / future recovery logic can branch on it. The pinned
        # `park_reason` is also what implementing's awaiting-human resume
        # uses to distinguish stale park flags after a relabel.
        state.set("park_reason", "agent_timeout")
        gh.write_pinned_state(issue, state)
        return

    wt = _wf._worktree_path(spec, issue.number)
    after_sha = _wf._head_sha(wt)
    committed = bool(after_sha) and after_sha != before_sha

    # A dirty worktree blocks every downstream outcome -- commit + push
    # would publish a branch that omits the dirty files, and the
    # no-change / on_question paths would silently leave docs edits
    # behind on disk that the eventual reviewer never sees. Check
    # before any other decision so an agent that edited files without
    # committing (and then either emitted `DOCS: NO_CHANGE`, asked a
    # question, or produced nothing) cannot slip past.
    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _wf._on_dirty_worktree(gh, issue, state, result, dirty)
        gh.write_pinned_state(issue, state)
        return

    if committed:
        if not _wf._push_branch(spec, wt, branch):
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} git push failed; see "
                "orchestrator logs.",
                reason="push_failed",
            )
            state.set("park_reason", "push_failed")
            gh.write_pinned_state(issue, state)
            return
        state.set("docs_checked_sha", after_sha)
        state.set("docs_verdict", "updated")
        state.set("silent_park_count", 0)
        notice = (
            ":books: documenting pass: pushed recovered docs commit(s)."
            if recovered
            else ":books: documenting pass: pushed docs commit."
        )
        try:
            _wf._post_pr_comment(gh, int(pr_number), state, notice)
        except Exception:
            _wf.log.exception(
                "issue=#%s could not post docs-pushed notice to PR #%s",
                issue.number, pr_number,
            )
        _advance_after_docs_push(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return

    # No new commit and a clean tree -- the agent either declared no
    # change or asked a question. The explicit DOCS: NO_CHANGE marker
    # is the only signal that confirms the diff was checked and
    # nothing was needed.
    verdict, body = _wf._parse_documentation_verdict(result.last_message or "")
    if verdict == "no_change":
        if ahead > 0:
            # A previous tick committed docs but parked before the
            # push landed (push_failed / agent_timeout / dirty). The
            # resumed dev added nothing new and confirmed no further
            # change is needed, but the requirement is to advance
            # only after the docs commit is PUSHED or a true
            # no-change verdict is parsed for the REMOTE PR state.
            # The local-only commit must reach the remote first;
            # otherwise the reviewer agent at validating would never
            # see the docs in the diff.
            if not _wf._push_branch(spec, wt, branch):
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} git push failed; see "
                    "orchestrator logs.",
                    reason="push_failed",
                )
                state.set("park_reason", "push_failed")
                gh.write_pinned_state(issue, state)
                return
            state.set("docs_checked_sha", after_sha)
            state.set("docs_verdict", "updated")
            state.set("silent_park_count", 0)
            try:
                _wf._post_pr_comment(
                    gh, int(pr_number), state,
                    ":books: documenting pass: pushed recovered docs "
                    "commit(s) after no-change confirmation.",
                )
            except Exception:
                _wf.log.exception(
                    "issue=#%s could not post recovered-docs notice to "
                    "PR #%s", issue.number, pr_number,
                )
            _advance_after_docs_push(gh, issue, state)
            gh.write_pinned_state(issue, state)
            return
        # Persist the SHA the dev evaluated even on a "nothing
        # changed" outcome. The fresh-spawn branch writes
        # `docs_checked_sha = before_sha` BEFORE the spawn (so a
        # no-change outcome there leaves it correct), but the
        # awaiting-human resume path uses the same pre-spawn write
        # ABOVE; setting it here too makes the post-condition
        # explicit and covers the case where neither pre-spawn
        # write fired (e.g. a future entry path that bypasses
        # them). `after_sha == before_sha` in this branch by
        # construction (no commit means no SHA delta).
        state.set("docs_checked_sha", after_sha)
        state.set("docs_verdict", "no_change")
        state.set("silent_park_count", 0)
        justification = body.strip()
        if justification:
            quoted = "> " + justification.replace("\n", "\n> ")
            note = (
                ":books: documenting pass: no docs changes "
                f"required.\n\n{quoted}"
            )
        else:
            note = (
                ":books: documenting pass: no docs changes required."
            )
        try:
            _wf._post_pr_comment(gh, int(pr_number), state, note)
        except Exception:
            _wf.log.exception(
                "issue=#%s could not post docs-no-change notice to PR #%s",
                issue.number, pr_number,
            )
        _advance_after_docs_no_change(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return

    # Unknown verdict: either a real question, an ambiguous message,
    # or an empty / silent run. Reuse `_on_question`, which posts the
    # HITL ping, distinguishes the silent-crash case via stderr
    # diagnostics, and tags `silent_park_count` so a poisoned session
    # can be dropped on the next resume.
    _wf._on_question(gh, issue, state, result)
    gh.write_pinned_state(issue, state)
