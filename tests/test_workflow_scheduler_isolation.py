# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator import base_sync, workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
)

from tests.scheduler_routing_workers import (
    _record_current_thread,
)

from tests.scheduler_routing_fakes import (
    _FakeWorktreeRoot,
    _WorkerClientFactory,
)

from tests.scheduler_routing_test_support import (
    _SchedulerWorkflowTest,
)

REPO_SLUG = "acme/widget"
TARGET_ROOT = Path("/tmp/orchestrator-test-target-root")
PROCESS_ISSUE = "_process_issue"
REFRESH_BASE = "_refresh_base_and_worktrees"
FANOUT_START_TIMEOUT_MESSAGE = "implementing fanout #1 did not start"
POLL_INTERVAL_SECONDS = 0.01
EVENT_TIMEOUT_SECONDS = 2.0
WORKER_TIMEOUT_SECONDS = 5.0
DEFERRED_ISSUE_NUMBERS = (10, 11, 12)
FAMILY_ISSUE_NUMBER = 42
RELABELLED_FANOUT_ISSUE_NUMBER = 50


class TickExecutionIsolationTest(_SchedulerWorkflowTest):
    def test_legacy_path_used_when_scheduler_is_none(self) -> None:
        # `scheduler=None` must keep the existing synchronous in-thread
        # behavior intact. The legacy path runs `_process_issue` on the
        # caller thread for `parallel_limit=1`, never touches the
        # scheduler, and -- crucially -- never calls `_for_worker_thread`
        # on that path.
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))

        caller_thread = threading.get_ident()
        worker_threads: list[int] = []

        clone = MagicMock(
            side_effect=AssertionError(
                "_for_worker_thread must not be called on the legacy path",
            )
        )
        with (
            patch.object(gh, "_for_worker_thread", clone),
            patch.object(
                workflow,
                REFRESH_BASE,
            ),
            patch.object(
                workflow,
                PROCESS_ISSUE,
                side_effect=lambda *args: _record_current_thread(worker_threads, *args),
            ),
        ):
            workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(worker_threads, [caller_thread])
        clone.assert_not_called()

    def test_refresh_skips_active_issue_on_next_tick(self) -> None:
        # The "active issues are skipped until completion" requirement
        # has to hold for the pre-tick base refresh too, not just the
        # scheduler.submit gate. The refresh iterates per-issue
        # worktrees and either rebases (pre-PR) or relabels /
        # state-mutates (PR-having); racing that against a still-
        # running handler corrupts the worktree under the agent or
        # clobbers pinned state mid-write.
        #
        # Drive two ticks: tick 1 dispatches the issue and the worker
        # holds inside `_process_issue`. Tick 2 calls the refresh
        # helper -- but because the scheduler reports the issue as
        # active, the refresh must skip its per-worktree sync. This
        # test inspects how `_refresh_base_and_worktrees` (the real
        # one, not a mock) treats the active-issue case by patching
        # only the inner `_sync_worktree_with_base` step, which is
        # what would actually mutate the worktree / pinned state.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label=LABEL_IMPLEMENTING))

        process = self._processor(7)

        # Stub fetch + iterdir so the real `_refresh_base_and_worktrees`
        # runs but never touches the filesystem or the network. The
        # scheduler-aware skip lives in the per-worktree loop; if it
        # regressed, `sync` would be called for the still-active
        # issue.
        refresh = SimpleNamespace(
            sync=MagicMock(),
            fetch_result=MagicMock(returncode=0, stderr=""),
            root=_FakeWorktreeRoot(),
        )

        self._assert_active_refresh_skipped(gh, sched, process, refresh)

        with self._patched_refresh(refresh, None):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertEqual(refresh.sync.call_args.args[3], 7)

    def test_workers_use_own_clients_and_refetch(
        self,
    ) -> None:
        # The scheduler dispatch must mirror the legacy parallel path:
        # mint a worker-thread client via `_for_worker_thread()` and
        # refetch the Issue against that client so PyGithub's
        # Requester chain isn't shared across threads.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))

        client_factory = _WorkerClientFactory()
        process = MagicMock()

        with (
            patch.object(gh, "_for_worker_thread", client_factory),
            patch.object(
                workflow,
                REFRESH_BASE,
            ),
            patch.object(workflow, PROCESS_ISSUE, process),
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self._wait_idle(sched, REPO_SLUG)

        self.assertEqual(len(client_factory.clients), 1)
        # The parent client is NOT what the worker saw.
        worker_client = process.call_args.args[0]
        self.assertIsNot(worker_client, gh)
        self.assertIs(worker_client, client_factory.clients[0])

    def _assert_active_refresh_skipped(
        self,
        client,
        scheduler,
        process,
        refresh,
    ) -> None:
        with self._patched_refresh(refresh, process):
            workflow.tick(client, self._spec(), scheduler=scheduler)
            self.assertTrue(
                process.starts[7].wait(timeout=EVENT_TIMEOUT_SECONDS),
                "worker never entered _process_issue",
            )
            refresh.sync.reset_mock()
            workflow.tick(client, self._spec(), scheduler=scheduler)
            refresh.sync.assert_not_called()
            process.releases[7].set()
        self._wait_idle(scheduler)

    @contextlib.contextmanager
    def _patched_refresh(self, refresh, processor):
        with (
            patch.object(
                base_sync,
                "_authed_target_fetch",
                return_value=refresh.fetch_result,
            ),
            patch.object(
                base_sync,
                "_repo_worktrees_root",
                return_value=refresh.root,
            ),
            patch.object(
                base_sync,
                "_sync_worktree_with_base",
                refresh.sync,
            ),
            patch.object(workflow, PROCESS_ISSUE, side_effect=processor),
        ):
            yield
