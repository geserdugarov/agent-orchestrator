# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow stage prompts."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

Issue = _owner.Issue
config = _owner.config
_COMMIT_STYLE_NOTE = _state._COMMIT_STYLE_NOTE
_FOREGROUND_ONLY_NOTE = _state._FOREGROUND_ONLY_NOTE
_MAX_FILES_SHOWN = _state._MAX_FILES_SHOWN
_NO_BODY = _state._NO_BODY
_NO_PRIOR_COMMENTS = _state._NO_PRIOR_COMMENTS
_SECTION_SEP = _state._SECTION_SEP


def _build_conflict_resolution_prompt(
    base_ref: str, files: list[str]
) -> str:
    shown = files[:_MAX_FILES_SHOWN]
    files_md = "\n".join(f"- `{file_path}`" for file_path in shown)
    if len(files) > len(shown):
        elided = len(files) - len(shown)
        files_md = f"{files_md}\n- ... ({elided} more)"
    return (
        f"`git rebase {base_ref}` left {len(files)} conflicted "
        "file(s) in your worktree. Resolve each conflict and complete the "
        "rebase in your current worktree. Do NOT push -- the orchestrator "
        "pushes and re-runs the reviewer.\n\n"
        f"Conflicted paths:\n\n{files_md}\n\n"
        "Workflow: edit each file to a coherent resolution, `git add` it, "
        "then run `git rebase --continue`. Repeat until the rebase completes. "
        "If Git reports an empty commit because the change is already present, "
        "use `git rebase --skip`; use `git commit --allow-empty` only when "
        "an empty commit is intentional. Use `git rebase --abort` only as "
        "the escape hatch when you cannot make progress. "
        "Use `git status` to inspect the in-progress rebase.\n\n"
        "If you genuinely cannot resolve a conflict, end your final "
        "message with a question for the human and leave the worktree "
        "mid-rebase; the orchestrator will park the issue for human review.\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )


def _build_question_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[config.RepoSpec],
) -> str:
    """Compose the read-only prompt used by the `question` stage.

    The agent runs in the per-issue `issue-N` worktree with read-only
    expectations: it must answer the standing question (or ask a focused
    follow-up of its own) without touching code, committing, or pushing.
    The orchestrator parks on any commit / dirty-tree output, so the
    prompt is explicit about that contract.

    The tracked-repos awareness block is included for a multi-repo
    deployment; it lists the sibling checkouts as read-only references
    and does not soften this stage's own no-write contract (the block's
    framing defers write permission to the surrounding prompt, which
    grants none here).
    """
    body = issue.body or _NO_BODY
    convo = comments_text or _NO_PRIOR_COMMENTS
    tracked = _owner._build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are answering a standing question on GitHub issue "
        f"#{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Read the issue and the conversation above, inspect the codebase "
        "with read-only commands (`git ls-files`, `git log`, `cat`, "
        "`grep`, etc.), and write a focused answer to the open question. "
        "Cite file paths or commits when useful. You MUST NOT modify, "
        "create, delete, commit, or push any file -- this stage is "
        "purely informational.\n\n"
        "If you need more information from the human before you can "
        "answer, end your message with a single, focused follow-up "
        "question. Otherwise end with a clear answer that the human can "
        "act on (close the issue, relabel it to `implementing`, etc.)."
    )


def _build_question_followup_prompt(comments: list) -> str:
    """Compose the resume prompt the question stage sends back to its
    locked agent session after a human reply.

    Mirrors `_resume_developer_on_human_reply`'s shape -- a quote of the
    incoming comments -- but reiterates the read-only / no-commit
    contract so a multi-tick conversation cannot drift into the agent
    deciding to "just implement the fix".
    """
    body = _SECTION_SEP.join(
        _owner._quote_comment_line(comment) for comment in comments
    )
    quoted = _owner._as_blockquote(body)
    return (
        "The human replied on the issue thread. Continue the discussion "
        "and answer their reply.\n\n"
        f"Human reply:\n\n{quoted}\n\n"
        "Reminder: this is still the read-only question stage. Do NOT "
        "modify, create, delete, commit, or push any file. End with a "
        "clear answer or a single, focused follow-up question."
    )


def _build_pr_comment_followup(comments: list) -> str:
    """Compose a dev-fix prompt from new PR-side comments.

    The dev session has not seen any PR comment before (those live on a
    different surface than the issue thread it was fed at spawn time), so a
    short preamble is needed to frame the request -- otherwise a comment like
    "rename foo to bar" reads as freeform chatter without context.
    """
    body = _SECTION_SEP.join(
        _owner._quote_comment_line(comment) for comment in comments
    )
    quoted = _owner._as_blockquote(body)
    return (
        "New comments arrived on the open PR for this issue. Address each item, "
        "then COMMIT the fix in your current worktree. Do NOT push -- the "
        "orchestrator pushes and re-runs the reviewer.\n\n"
        f"PR comments:\n\n{quoted}\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        "If you genuinely disagree with a point, end your final message with a "
        "question for the human and leave that item un-fixed; the orchestrator "
        "will park the issue for human review.\n\n"
        "If the comments contain NO concrete, actionable change request -- e.g. "
        "a vague 'continue', 'ok', or 'ping' that names no specific defect -- "
        "and the branch already satisfies them, make NO commit and end your "
        "final message with a single line `ACK: <brief reason>`. The "
        "orchestrator will then return the PR to review-ready instead of "
        "parking it for a fix that is not warranted.\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )
