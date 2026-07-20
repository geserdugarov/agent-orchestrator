# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fakes for the polling-loop entry-point tests (`test_main.py`).

The recorders stand in for the two collaborators `main.main` / `main._run_tick`
call out to -- the `GitHubClient` constructor and `workflow.tick` -- so a test
can drive the loop and then assert on what it dispatched. They live here so
`test_main.py` stays a set of test classes rather than test-plus-fixtures.
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock


class _ClientFactory:
    """`GitHubClient` side_effect for `main.main`: builds one slug-tagged
    MagicMock per `RepoSpec` and records each by slug in `by_slug`, so a test
    can assert on the client `main` paired with a given repo.
    """

    def __init__(self) -> None:
        self.by_slug: dict[str, MagicMock] = {}

    def __call__(self, *, repo_spec):
        client = MagicMock()
        client.slug = repo_spec.slug
        self.by_slug[repo_spec.slug] = client
        return client


class _TickRecorder:
    """`workflow.tick` side_effect that thread-safely records every tick's
    `(spec.slug, gh.slug)` pairing, the scheduler it was handed, and the
    worker-thread id, then runs an optional `on_tick(gh, spec)` hook for
    per-test side effects (raise, barrier, shutdown). Ticks run on fan-out
    worker threads, so all recording is guarded by a lock.
    """

    def __init__(self, on_tick=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.schedulers: list[object] = []
        self.threads: list[int] = []
        self._on_tick = on_tick
        self._lock = threading.Lock()

    def __call__(self, gh, spec, *, scheduler=None):
        with self._lock:
            self.calls.append((spec.slug, gh.slug))
            self.schedulers.append(scheduler)
            self.threads.append(threading.get_ident())
        if self._on_tick is not None:
            self._on_tick(gh, spec)

    @property
    def slugs(self) -> list[str]:
        with self._lock:
            return [slug for slug, _ in self.calls]


def _raise_on_slug(spec, target_slug: str, message: str) -> None:
    """Tick hook: raise `RuntimeError(message)` when `spec` is `target_slug`,
    simulating one repo's tick failing while the others keep advancing.
    """
    if spec.slug == target_slug:
        raise RuntimeError(message)


def _build_clients(slugs):
    """Mirror `main`'s startup: build one MagicMock GitHubClient per slug and
    pair it with the matching `RepoSpec`. The dispatch tests never call
    `ensure_workflow_labels`, so the mock surface is intentionally minimal.
    """
    from orchestrator.config import RepoSpec
    clients = []
    for slug in slugs:
        spec = RepoSpec(
            slug=slug,
            target_root=Path("/tmp"),
            base_branch="main",
        )
        gh = MagicMock()
        gh.slug = slug
        clients.append((spec, gh))
    return clients
