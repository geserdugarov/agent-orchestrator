# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Reusable execution boundary for polling-dispatch tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from unittest import mock

from tests import main_helpers as _helpers


@dataclass(frozen=True)
class DispatchContext:
    main_module: ModuleType
    scheduler: object
    clients: list

    def run(self, tick_effect) -> None:
        with mock.patch.object(
            self.main_module.workflow,
            _helpers._TICK_ATTR,
            side_effect=tick_effect,
        ):
            self.main_module._run_tick(self.clients, self.scheduler)

    def run_and_capture_reap(self):
        with (
            mock.patch.object(
                self.main_module.workflow,
                _helpers._TICK_ATTR,
                return_value=None,
            ),
            mock.patch.object(
                self.scheduler,
                "reap",
            ) as reap,
        ):
            self.main_module._run_tick(self.clients, self.scheduler)
            return reap

    def run_real_and_capture_reap(self, workflow_module: ModuleType):
        for _repo_spec, github_client in self.clients:
            github_client.list_pollable_issues.return_value = iter([])
        with (
            mock.patch.object(workflow_module, "_refresh_base_and_worktrees"),
            mock.patch.object(
                self.scheduler,
                "reap",
            ) as reap,
        ):
            self.main_module._run_tick(self.clients, self.scheduler)
            return reap


@contextmanager
def dispatch_context(main_module: ModuleType, slugs: list[str]):
    scheduler = main_module.IssueScheduler(global_cap=4, per_repo_cap=4)
    try:
        yield DispatchContext(main_module, scheduler, _helpers._build_clients(slugs))
    finally:
        scheduler.shutdown()
