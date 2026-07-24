# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Authenticated repository client composed from the mixin leaves.

``GitHubClient`` subclasses the composed inventory base, so this module imports
``_github_api`` -- and thus the full mixin chain -- at load time. It is imported
lazily by ``orchestrator.github`` (through the package ``__getattr__``) rather
than from the package initializer, so a chain leaf that reaches
``orchestrator.github.pinned_state`` never re-enters the initializer mid-import.
"""
from __future__ import annotations

from typing import Optional

from github import Auth, Github
from github.Label import Label
from github.Repository import Repository

from orchestrator import _github_api, config


class GitHubClient(_github_api.GitHubClientBase):
    """Authenticated repository client with a worker-safe clone seam."""

    def __init__(
        self,
        token: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_spec: Optional["config.RepoSpec"] = None,
        *,
        bot_login: Optional[str] = None,
    ) -> None:
        slug = repo_slug or config.REPO if repo_spec is None else repo_spec.slug
        if token is None:
            token = config._resolve_github_token(slug)
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is empty. Export it in the orchestrator's "
                "environment or write it to "
                f"~/.config/{slug}/token "
                "(override path with ORCHESTRATOR_TOKEN_FILE). "
                "Do NOT put it in REPO_ROOT/.env -- the implementer agent "
                "can read that file.",
            )
        self._gh = Github(auth=Auth.Token(token))
        self.repo: Repository = self._gh.get_repo(slug)
        self._repo_slug = slug
        self._token = token
        self._bot_login = (
            self._gh.get_user().login
            if bot_login is None
            else bot_login
        )
        self.recorded_events: list[dict] = []
        self._label_cache: dict[str, Label] = {}
        self._pollable_calls = 0
