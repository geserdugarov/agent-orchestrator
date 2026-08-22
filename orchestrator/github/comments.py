# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trust policy for GitHub-authored content.

The orchestrator feeds issue and PR comments to coding agents as
workflow-driving instructions. On a public repo that is an injection
surface: any account can post a comment that steers the agent. These
helpers centralize the "may this author supply workflow-driving content?"
decision so every consumer applies one allowlist policy.

The decision is about an author on a GitHub thread, so it belongs beside
the readers that produce those threads rather than above them: the git
base-sync owners and the workflow stage leaves both gate on it, and
neither should have to reach up into the workflow layer to ask.

Policy (keyed on `config.ALLOWED_ISSUE_AUTHORS`):

* Empty (the default) -- no allowlist configured. Preserve the legacy
  single-user behavior: every author is trusted.
* Populated -- only accounts whose login is in the allowlist are trusted,
  compared case-insensitively (GitHub logins are case-insensitive). This
  gates Bot / GitHub-App accounts too: a bot is trusted only when its own
  login is explicitly listed, so a stray CI or dependency bot cannot
  inject workflow-driving content, while an intentionally allowlisted
  automation account still can.

The low-level readers (`GitHubClient.comments_after`, the PR comment /
review readers) stay raw. Callers that want the allowlist applied filter
their result through `filter_trusted`, or gate a single author on
`is_trusted_author`.

`carries_own_marker` answers a different question and lives here for the same
reason: whether a hidden marker on a thread is one this orchestrator wrote.
That question is asked wherever a comment is the receipt for an effect that
cannot be made one operation with recording it, and the author is part of it --
a marker anybody may post is a marker anybody may use to suppress the sentence
it stands for. A client with no authenticated login to compare against checks
the marker alone, which is the same fallback the pinned-state read takes.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, TypeVar

from orchestrator import config

_CommentT = TypeVar("_CommentT")


def _allowed_logins(allowed: Optional[Iterable[str]]) -> set[str]:
    """Lower-cased allowlist set, defaulting to `config.ALLOWED_ISSUE_AUTHORS`.

    Falsy entries are dropped so a stray empty string in the configured
    tuple cannot match a user whose login failed to load (empty login).
    """
    if allowed is None:
        allowed = config.ALLOWED_ISSUE_AUTHORS
    return {login.lower() for login in allowed if login}


def is_trusted_author(
    user: Any, *, allowed: Optional[Iterable[str]] = None
) -> bool:
    """True if `user` may supply workflow-driving content.

    `user` is any object exposing a `.login` attribute -- a PyGithub
    `NamedUser`, the test `FakeUser`, or `None` for a comment whose author
    failed to load. `allowed` defaults to `config.ALLOWED_ISSUE_AUTHORS`;
    pass an explicit iterable to exercise the policy without patching config.

    An empty allowlist trusts everyone (legacy behavior). A populated
    allowlist trusts only logins it contains, compared case-insensitively;
    a missing user or empty login is untrusted. Bot / App accounts follow
    the same rule -- trusted only when their login is explicitly allowlisted.
    """
    allowed_lower = _allowed_logins(allowed)
    if not allowed_lower:
        return True
    login = getattr(user, "login", None) or ""
    return login.lower() in allowed_lower


def filter_trusted(
    comments: Iterable[_CommentT], *, allowed: Optional[Iterable[str]] = None
) -> List[_CommentT]:
    """Keep only comments whose author is trusted (see `is_trusted_author`).

    Each item is any object exposing a `.user` attribute. Input order is
    preserved. With no allowlist configured every item is kept, so this is
    a safe drop-in over a raw `comments_after` / PR-reader result that
    changes behavior only once an operator opts into the allowlist.
    """
    allowed_lower = _allowed_logins(allowed)
    if not allowed_lower:
        return list(comments)
    return [
        comment for comment in comments
        if is_trusted_author(getattr(comment, "user", None), allowed=allowed_lower)
    ]


def carries_own_marker(
    comments: Iterable[Any], marker: str, *, bot_login: Optional[str],
) -> bool:
    """Whether one of these comments is OURS and carries `marker`.

    Both halves are required. The marker says which effect the comment is the
    receipt for, and it has to be scoped by its caller to the one episode it
    belongs to -- a marker shared across episodes reads a previous one's
    receipt as this one's. The author says the receipt is ours: an HTML
    comment is invisible in the rendered thread and trivially copied, so
    without the check a third party could post the marker and silence
    whatever the receipt gates.
    """
    for comment in comments:
        if marker not in (getattr(comment, "body", "") or ""):
            continue
        if bot_login is None:
            return True
        author = getattr(getattr(comment, "user", None), "login", None)
        if author == bot_login:
            return True
    return False
