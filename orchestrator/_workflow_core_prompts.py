# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow core prompts."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

Issue = _owner.Issue
config = _owner.config
_COMMIT_STYLE_NOTE = _state._COMMIT_STYLE_NOTE
_FOREGROUND_ONLY_NOTE = _state._FOREGROUND_ONLY_NOTE
_NO_BODY = _state._NO_BODY
_NO_PRIOR_COMMENTS = _state._NO_PRIOR_COMMENTS


def _build_implement_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[config.RepoSpec],
) -> str:
    body = issue.body or _NO_BODY
    convo = comments_text or _NO_PRIOR_COMMENTS
    tracked = _owner._build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are the implementer for GitHub issue #{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Implement the change in the current working directory (a fresh git worktree on a "
        "new branch). When done, COMMIT your changes with a clear message. Do NOT push - "
        "the orchestrator pushes and opens the PR.\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        f"{_FOREGROUND_ONLY_NOTE}\n\n"
        "If you cannot proceed because of missing information, leave the working tree "
        "uncommitted (no commits) and end your response with a clear question for the human."
    )


def _build_fresh_respawn_preamble(
    spec: config.RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[config.RepoSpec],
) -> str:
    """Re-grounding header prepended to a FRESH dev spawn that REPLACES a
    retired or poisoned session mid-issue (proactive rotation, silent-park
    fallback, or stale/overflow recovery).

    The previous session's in-memory reasoning is gone, but its committed work
    survives on the current branch, so the fresh agent is pointed at the branch
    as the source of truth and re-grounded in the issue requirements +
    conversation. Without this the rotation regresses into a context-starved
    spawn that could re-implement from scratch or ignore the original spec.
    The caller appends the stage-specific instruction (fix feedback, drift,
    conflict, ...) after this block.
    """
    body = issue.body or _NO_BODY
    convo = comments_text or _NO_PRIOR_COMMENTS
    tracked = _owner._build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are resuming work on GitHub issue #{issue.number}: {issue.title!r}. "
        "A previous agent session worked on this issue and its commits are "
        "already on the current branch (your working directory); that session's "
        "history is NOT available to you. Before doing anything, re-ground "
        "yourself: inspect what has already been done with `git log --oneline` "
        "and `git diff` against the base branch, and continue from there -- do "
        "NOT restart the implementation from scratch.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Your immediate task follows.\n"
        "----------------------------------------"
    )


def _build_review_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[config.RepoSpec],
    dev_backend: str = "agent",
) -> str:
    body = issue.body or _NO_BODY
    convo = comments_text or _NO_PRIOR_COMMENTS
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    tracked = _owner._build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are an automated code reviewer for GitHub issue #{issue.number}: {issue.title!r}. "
        f"A separate {dev_backend} session has implemented this issue and committed to the current "
        f"branch. The base branch is `{base_ref}`.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Inspect the change with:\n"
        f"  git log --oneline {base_ref}..HEAD\n"
        f"  git diff {base_ref}...HEAD\n\n"
        "Review the change against the issue requirements. Flag correctness bugs, missing "
        "tests, scope creep, obvious style issues, and anything that would block a human "
        "approver. Do NOT edit or commit anything -- you are a reviewer only.\n\n"
        "Your final message MUST end with exactly one of these markers, alone on its own line:\n"
        "  VERDICT: APPROVED\n"
        "  VERDICT: CHANGES_REQUESTED\n\n"
        "If CHANGES_REQUESTED, list the specific items above the verdict line as a numbered "
        "list so the implementer can address them one by one. If the change is acceptable as "
        "is, write VERDICT: APPROVED with a one-line justification above it."
    )


def _build_documentation_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[config.RepoSpec],
) -> str:
    """Prompt for the documentation pass that runs as the final-docs
    handoff between reviewer approval and `in_review`.

    Reuses the dev agent role -- the documentation pass commits to the same
    branch as the implementer, so it is operating as a developer and not a
    reviewer. No separate backend env var is introduced for this stage;
    the stage handler invokes the existing dev backend on the PR worktree.
    """
    body = issue.body or _NO_BODY
    convo = comments_text or _NO_PRIOR_COMMENTS
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    tracked = _owner._build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are the documentation pass for GitHub issue #{issue.number}: "
        f"{issue.title!r}. A separate session has implemented this issue and "
        f"committed to the current branch. The base branch is `{base_ref}`.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Inspect the change with:\n"
        f"  git log --oneline {base_ref}..HEAD\n"
        f"  git diff {base_ref}...HEAD\n\n"
        "Compare the branch diff against `README.md` and the `docs/` tree. "
        "If any user-facing description or architectural note needs to be "
        "updated to match the code that landed in this branch, UPDATE the "
        "relevant files and COMMIT the change in the current worktree. Do "
        "NOT push -- the orchestrator pushes once this stage finishes. Do "
        "NOT inspect or modify the `plans/` tree or roadmap entries: those "
        "are working notes owned by humans and are out of scope for the "
        "final-docs pass.\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        "If the branch genuinely requires no documentation change, do NOT "
        "commit and end your final message with EXACTLY this marker, alone "
        "on its own line:\n\n"
        "  DOCS: NO_CHANGE\n\n"
        "Place a one-sentence justification on the line above the marker. "
        "The orchestrator will NOT accept ambiguous phrasing like "
        "'no changes needed' as success without the explicit marker; an "
        "agent message that neither commits nor emits the marker is parked "
        "for human review.\n\n"
        "If you genuinely cannot decide because of missing information, "
        "leave the worktree uncommitted, omit the marker, and end your "
        "final message with a question for the human; the orchestrator "
        "will park the issue for human review.\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )


def _build_fix_prompt(review_feedback: str) -> str:
    feedback = review_feedback.strip() or "(reviewer left no detail)"
    quoted = _owner._as_blockquote(feedback)
    return (
        "An automated reviewer requested changes on your implementation. Address each item "
        "below, then COMMIT the fix in your current worktree. Do NOT push -- the orchestrator "
        "pushes and re-runs the review.\n\n"
        f"Review feedback:\n\n{quoted}\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        f"{_FOREGROUND_ONLY_NOTE}\n\n"
        "If you genuinely disagree with a point, end your final message with a question for "
        "the human and leave that item un-fixed; the orchestrator will park the issue for "
        "human review. Otherwise, fix all items (a single commit is fine)."
    )
