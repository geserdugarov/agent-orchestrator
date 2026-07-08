# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared trust helpers for GitHub-authored content.

The orchestrator feeds issue and PR comments to coding agents as
workflow-driving instructions. On a public repo that is an injection
surface: any account can post a comment that steers the agent. These
helpers centralize the "may this author supply workflow-driving content?"
decision so every consumer applies one allowlist policy.

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
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, TypeVar

from . import config

_T = TypeVar("_T")


def _allowed_logins(allowed: Optional[Iterable[str]]) -> set[str]:
    """Lower-cased allowlist set, defaulting to `config.ALLOWED_ISSUE_AUTHORS`.

    Falsy entries are dropped so a stray empty string in the configured
    tuple cannot match a user whose login failed to load (empty login).
    """
    if allowed is None:
        allowed = config.ALLOWED_ISSUE_AUTHORS
    return {h.lower() for h in allowed if h}


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
    comments: Iterable[_T], *, allowed: Optional[Iterable[str]] = None
) -> List[_T]:
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
        c for c in comments
        if is_trusted_author(getattr(c, "user", None), allowed=allowed_lower)
    ]
