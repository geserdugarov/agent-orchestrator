# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Signal and shutdown probes for polling-loop tests."""

from __future__ import annotations

import threading


_ALPHA_REPO = "alpha/one"
_WORKER_WAIT_SECONDS = 5.0


def unexpected_dispatch() -> None:
    raise AssertionError("post-signal submit must not dispatch")


class SignalSubmitTick:
    def __init__(self, main_module) -> None:
        self._main = main_module
        self.submit_results: list[bool] = []

    def __call__(self, gh, spec, *, scheduler=None) -> None:
        self.submit_results.append(scheduler.submit(spec.slug, 1, lambda: None))
        self._main._shutdown(self._main.signal.SIGINT, None)
        self.submit_results.append(
            scheduler.submit(spec.slug, 2, unexpected_dispatch),
        )


class MultiRepoSignalTick:
    def __init__(self, main_module) -> None:
        self._main = main_module
        self._both_inside = threading.Barrier(2, timeout=_WORKER_WAIT_SECONDS)
        self._signal_fired = threading.Event()
        self._lock = threading.Lock()
        self.beta_results: list[bool] = []

    def __call__(self, gh, spec, *, scheduler=None) -> None:
        self._both_inside.wait()
        if spec.slug == _ALPHA_REPO:
            self._main._shutdown(self._main.signal.SIGINT, None)
            self._signal_fired.set()
            return
        signal_seen = self._signal_fired.wait(timeout=_WORKER_WAIT_SECONDS)
        if signal_seen:
            accepted = scheduler.submit(spec.slug, 7, unexpected_dispatch)
            with self._lock:
                self.beta_results.append(accepted)
            return
        raise AssertionError("signal did not fire within timeout")


class FirstTickShutdown:
    def __init__(self, main_module, signum: int) -> None:
        self._main = main_module
        self._signum = signum
        self._shutdown_done = threading.Event()

    def __call__(self, gh, spec, *, scheduler=None) -> None:
        if self._shutdown_done.is_set():
            return
        self._shutdown_done.set()
        self._main._shutdown(self._signum, None)


class WaitRecorder:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def __call__(self, timeout=None) -> bool:
        self.timeout = timeout
        return True
