# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from functools import partial

from orchestrator.scheduler import IssueScheduler

from tests.scheduler_worker_helpers import _failing_worker

SCHEDULER_LOGGER = "orchestrator.scheduler"
WORKER_FAILURE = "worker exploded"
WORKER_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
TRIAL_HEAD_START = 20
TRIAL_TOTAL = 80


class _CallbackRegistrationRace:
    def __init__(self) -> None:
        self._scheduler = IssueScheduler(global_cap=2, per_repo_cap=2)
        self._register_gate = threading.Event()
        self._submit_done = threading.Event()
        self._shutdown_done = threading.Event()
        self._submitter: threading.Thread | None = None
        self._shutter: threading.Thread | None = None
        self._real_add = Future.add_done_callback

    def __get__(self, future: Future, _owner):
        return partial(self._gated_add, future)

    def reach_blocked_state(self, test_case) -> None:
        test_case.addCleanup(self._register_gate.set)
        self._submitter = threading.Thread(target=self._submit)
        self._submitter.start()
        time.sleep(0.1)
        test_case.assertFalse(self._submit_done.is_set())
        self._shutter = threading.Thread(target=self._shutdown)
        self._shutter.start()
        time.sleep(0.1)
        test_case.assertFalse(
            self._shutdown_done.is_set(),
            "shutdown must wait for done-callback registration",
        )

    def release_and_assert_finished(self, test_case) -> None:
        self._register_gate.set()
        self._submitter.join(timeout=WORKER_TIMEOUT_SECONDS)
        self._shutter.join(timeout=WORKER_TIMEOUT_SECONDS)
        test_case.assertFalse(self._submitter.is_alive())
        test_case.assertFalse(self._shutter.is_alive())

    def _gated_add(self, future: Future, callback) -> None:
        if not self._register_gate.is_set():
            self._register_gate.wait(timeout=WORKER_TIMEOUT_SECONDS)
        self._real_add(future, callback)

    def _submit(self) -> None:
        self._scheduler.submit("owner/repo", 1, _failing_worker)
        self._submit_done.set()

    def _shutdown(self) -> None:
        self._scheduler.shutdown()
        self._shutdown_done.set()


class _ShutdownTrial:
    def __init__(self, trial_number: int) -> None:
        self._trial_number = trial_number
        self._scheduler = IssueScheduler(global_cap=8, per_repo_cap=8)
        self._accepted = 0
        self._logged = 0

    def run(self, test_case) -> _ShutdownTrial:
        with test_case.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
            self._overlap_shutdown(test_case)
            log_output = logs.output
        self._logged = sum(WORKER_FAILURE in message for message in log_output)
        return self

    def _overlap_shutdown(self, test_case) -> None:
        self._submit_range(0, TRIAL_HEAD_START)
        shutdown_thread = threading.Thread(target=self._scheduler.shutdown)
        shutdown_thread.start()
        self._submit_range(TRIAL_HEAD_START, TRIAL_TOTAL)
        shutdown_thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        test_case.assertFalse(shutdown_thread.is_alive())

    def _submit_range(self, start: int, stop: int) -> None:
        for issue_number in range(start, stop):
            accepted = self._scheduler.submit(
                f"owner/repo-{self._trial_number}",
                issue_number,
                _failing_worker,
            )
            self._accepted += int(accepted)
