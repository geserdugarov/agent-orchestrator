# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow prompt comments."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

Issue = _owner.Issue
Optional = _owner.Optional
is_trusted_author = _owner.is_trusted_author
_SECTION_SEP = _state._SECTION_SEP


def _quote_comment_line(comment: object, label: str = "") -> str:
    """Quote one already-selected comment as `@author[label]: body`.

    Shared by the resume/followup prompt builders and the stage handlers that
    fold fresh issue or PR comments into an agent prompt; `label` inserts a
    surface tag (e.g. ` (PR comment)`) after the author.
    """
    author = comment.user.login if comment.user else "user"
    body = comment.body or ""
    return f"@{author}{label}: {body}"


def _prompt_comment_chunk(issue_comment: object) -> Optional[str]:
    """Format one trusted, non-state issue comment for an agent prompt."""
    body = getattr(issue_comment, "body", None) or ""
    if "<!--orchestrator-state" in body:
        return None
    user = getattr(issue_comment, "user", None)
    if not is_trusted_author(user):
        return None
    login = user.login if user else "user"
    return f"@{login}: {body}"


def _recent_comments_text(issue: Issue, max_chars: int = 4000) -> str:
    """Conversation text fed to every agent prompt (implement, review,
    documentation, decompose, question, and the drift-resume prompt).

    An untrusted author's comment is dropped whole -- its body and any URLs
    it contains never reach the prompt -- so once `ALLOWED_ISSUE_AUTHORS`
    is set an outsider on a public repo cannot smuggle workflow-driving
    instructions into a coding agent through the issue thread. With no
    allowlist configured `is_trusted_author` trusts every author, so the
    default single-user deployment sees the full thread unchanged.
    """
    chunks: list[str] = []
    for issue_comment in issue.get_comments():
        chunk = _owner._prompt_comment_chunk(issue_comment)
        if chunk is not None:
            chunks.append(chunk)
    text = _SECTION_SEP.join(chunks)
    return text[-max_chars:] if len(text) > max_chars else text
