# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating verify."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_PARK_REASON = _state._PARK_REASON
_SHORT_SHA_LEN = _state._SHORT_SHA_LEN
_VERIFY_STATUS_TO_REASON = _state._VERIFY_STATUS_TO_REASON


def _verify_failure_detail(verify) -> str:
    """One-line description of a non-ok local-verify result, naming the
    failing command and its failure mode.

    The `head_changed` branch surfaces both short SHAs so the operator can
    `git show` the stray commit and decide whether to keep it (re-spawn the
    reviewer on the new HEAD) or revert it before re-trying.
    """
    if verify.status == "timeout":
        return (
            f"`{verify.command}` timed out after "
            f"{config.VERIFY_TIMEOUT}s"
        )
    if verify.status == "dirty":
        files = ", ".join(
            f"`{file_path}`" for file_path in verify.dirty_files[:10]
        )
        if len(verify.dirty_files) > 10:
            elided = len(verify.dirty_files) - 10
            files = f"{files}, … (+{elided} more)"
        return f"`{verify.command}` left the worktree dirty: {files}"
    if verify.status == "head_changed":
        before = (verify.head_before or "")[:_SHORT_SHA_LEN] or "(no HEAD)"
        after = (verify.head_after or "")[:_SHORT_SHA_LEN] or "(no HEAD)"
        return (
            f"`{verify.command}` moved HEAD ({before} -> {after}); "
            "verify commands must not commit"
        )
    exit_display = "?" if verify.exit_code is None else verify.exit_code
    return (
        f"`{verify.command}` exited with code "
        f"{exit_display}"
    )


def _park_verify_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    verify,
) -> None:
    """Park `validating` on a local-verify failure.

    The park comment names the failing command, its exit code (or
    timeout), and a redacted / truncated tail of the captured output so
    the operator can triage without pulling the orchestrator's logs.
    `park_reason` is set to a stable token (`verify_failed`,
    `verify_timeout`, or `verify_dirty`) so dashboards and future
    transient-recovery logic can branch on the failure mode.
    """
    from orchestrator import workflow as _wf

    reason = _VERIFY_STATUS_TO_REASON.get(verify.status, "verify_failed")
    detail = _owner._verify_failure_detail(verify)

    message = (
        f"{config.HITL_MENTIONS} local verification failed; PR not handed "
        f"off to in_review. {detail}."
    )
    # `verify.output` is already redacted-then-truncated by the runner;
    # re-redacting here would be a no-op for any match `_redact_secrets`
    # already collapsed to `***`, AND would not catch a partial secret
    # that straddled the truncation cut -- the only safe way to handle
    # that case is the redact-before-truncate pass inside the runner.
    output = verify.output or ""
    if output.strip():
        quoted = _wf._as_blockquote(output.rstrip())
        message = f"{message}\n\n_Verify output (tail):_\n\n{quoted}"

    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set(_PARK_REASON, reason)


def _ratchet_watermark(prev, seeded):
    """Combine a previously-persisted in_review watermark with a freshly-seeded
    one, never moving backward.

    A prior in_review tick may have already advanced the persisted watermark
    past PR feedback the dev has since fixed; `_seed_watermark_past_self` stops
    at the first post-pickup human comment, so without the max() that consumed
    comment would replay as "new". Returns the max of the two when both are
    present, the one that exists otherwise, or 0 when neither does -- 0 means
    "scan all from the beginning" and marks the surface as already seeded so the
    in_review legacy migration does not advance past historical human feedback.
    """
    if isinstance(prev, int):
        return prev if seeded is None else max(seeded, prev)
    return 0 if seeded is None else seeded


def _finalize_validating_terminal(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Terminal short-circuits checked before the reviewer runs; True when one
    fired and the caller must return.

    External merge: a human merged the PR while the reviewer was queued.
    Finalize to `done` rather than running the reviewer against a branch that
    already landed. Closed-issue counterpart: the closed-`validating` sweep
    yields issues a human closed without a merged PR (the change was rejected
    mid-review, or the PR was closed-without-merge); flip to `rejected` so the
    reviewer does not spawn against a closed issue and the PR is not relabeled
    back to `in_review`. The in_review / fixing handlers carry equivalent
    terminal checks.
    """
    from orchestrator import workflow as _wf

    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return True
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return True
    return False
