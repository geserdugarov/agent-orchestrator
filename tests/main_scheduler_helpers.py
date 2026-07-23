# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Scheduler and cross-poll probes for polling-loop tests."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager


_ALPHA_REPO = "alpha/one"
_BETA_REPO = "beta/two"
_WORKER_WAIT_SECONDS = 5.0
_FAST_WAIT_SECONDS = 2.0
_SCHEDULER_POLL_SECONDS = 0.01


class BarrierTick:
    def __init__(self, parties: int) -> None:
        self._barrier = threading.Barrier(parties, timeout=_WORKER_WAIT_SECONDS)
        self._lock = threading.Lock()
        self.completed: list[str] = []

    def __call__(self, gh, spec, *, scheduler=None) -> None:
        self._barrier.wait()
        with self._lock:
            self.completed.append(spec.slug)


class SchedulerFactory:
    def __init__(self, scheduler_class) -> None:
        self._scheduler_class = scheduler_class
        self.built: list[object] = []

    def __call__(self, *args, **kwargs):
        scheduler = self._scheduler_class(*args, **kwargs)
        self.built.append(scheduler)
        return scheduler


class GlobalCapProbe:
    def __init__(self) -> None:
        self._counter_lock = threading.Lock()
        self._received_lock = threading.Lock()
        self._admitted = threading.Semaphore(0)
        self._release = threading.Event()
        self._in_flight = 0
        self.max_in_flight = 0
        self.received: list[object] = []

    def worker(self) -> None:
        with self._counter_lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        self._admitted.release()
        self._release.wait(timeout=_WORKER_WAIT_SECONDS)
        with self._counter_lock:
            self._in_flight -= 1

    def tick(self, gh, spec, *, scheduler=None) -> None:
        with self._received_lock:
            self.received.append(scheduler)
        scheduler.submit(spec.slug, 1, self.worker)

    def release_when_two_admitted(self) -> None:
        for _ in range(2):
            admitted = self._admitted.acquire(timeout=_WORKER_WAIT_SECONDS)
            if admitted:
                continue
            raise AssertionError("fewer than 2 workers admitted within timeout")
        time.sleep(0.1)
        self._release.set()

    def finish(self, releaser: threading.Thread) -> None:
        self._release.set()
        releaser.join(timeout=_WORKER_WAIT_SECONDS)

    @contextmanager
    def releasing(self):
        releaser = threading.Thread(target=self.release_when_two_admitted)
        releaser.start()
        try:
            yield
        finally:
            self.finish(releaser)


class CrossPollProbe:
    def __init__(self) -> None:
        self.alpha_started = threading.Event()
        self.alpha_release = threading.Event()
        self.beta_done = threading.Event()
        self.current_pass = 0

    def slow_alpha(self) -> None:
        self.alpha_started.set()
        self.alpha_release.wait(timeout=_WORKER_WAIT_SECONDS)

    def quick_beta(self) -> None:
        self.beta_done.set()

    def tick(self, gh, spec, *, scheduler=None) -> None:
        if self.current_pass == 1 and spec.slug == _ALPHA_REPO:
            scheduler.submit(spec.slug, 1, self.slow_alpha)
        elif self.current_pass == 2 and spec.slug == _BETA_REPO:
            scheduler.submit(spec.slug, 2, self.quick_beta)


class DuplicateActiveProbe:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.run_count = 0
        self.submit_results: list[bool] = []

    def worker(self) -> None:
        with self.lock:
            self.run_count += 1
        self.started.set()
        self.release.wait(timeout=_WORKER_WAIT_SECONDS)

    def tick(self, gh, spec, *, scheduler=None) -> None:
        self.submit_results.append(scheduler.submit(spec.slug, 7, self.worker))


class FinishedWorkerProbe:
    def __init__(self) -> None:
        self.done_events: list[threading.Event] = []
        self.lock = threading.Lock()
        self.run_count = 0
        self.submit_results: list[bool] = []

    def worker(self) -> None:
        with self.lock:
            self.run_count += 1
        self.done_events[-1].set()

    def tick(self, gh, spec, *, scheduler=None) -> None:
        self.done_events.append(threading.Event())
        self.submit_results.append(scheduler.submit(spec.slug, 3, self.worker))


def wait_until_inactive(scheduler, repo: str, issue_number: int) -> None:
    deadline = time.monotonic() + _FAST_WAIT_SECONDS
    while scheduler.is_active(repo, issue_number):
        if time.monotonic() <= deadline:
            time.sleep(_SCHEDULER_POLL_SECONDS)
            continue
        raise AssertionError("in-flight marker not cleared after worker exit")
