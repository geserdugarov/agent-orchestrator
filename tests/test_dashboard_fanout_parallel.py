# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Parallel dashboard reader fan-out tests."""

import threading


import time


import unittest


from functools import partial


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_analytics_read as _analytics_read_module,
)

_BEATS_SEQUENTIAL_SUM_ELAPSED = 2.5


def _raise_read_error(
    message: str,
    calls: list[str] | None = None,
    call_name: str | None = None,
) -> None:
    read_error = _analytics_read_module().AnalyticsReadError
    if calls is None or call_name is None:
        raise read_error(message)
    calls.append(call_name)
    raise read_error(message)


def _return_value(payload: int) -> int:
    return payload


def _record_threaded_reader(
    name: str,
    calls: dict[str, int],
    threads: set[int],
    lock,
) -> str:
    with lock:
        calls[name] = calls.get(name, 0) + 1
        threads.add(threading.get_ident())
    return name


def _sleep_then_return(delay: float, payload: str) -> str:
    time.sleep(delay)
    return payload


def _timed_parallel_fanout(dashboard, delay: float) -> tuple[dict, float]:
    readers = [
        (f"r{index}", partial(_sleep_then_return, delay, "ok"))
        for index in range(4)
    ]
    started_at = time.perf_counter()
    read_results = dashboard._fan_out_reads(readers, parallel=True, max_workers=4)
    return read_results, time.perf_counter() - started_at


class FanOutReadsParallelTest(unittest.TestCase):
    """The parallel branch dispatches readers across a
    `ThreadPoolExecutor`. Each worker thread is responsible for its
    own analytics connection (the thread-local cache from #383); the
    helper itself only owns dispatch + result collection.
    """

    def test_all_results_returned_keyed_by_name(self) -> None:
        _, dashboard = _reload()

        readers = [(f"r{idx}", partial(_return_value, idx)) for idx in range(5)]
        read_results = dashboard._fan_out_reads(readers, parallel=True, max_workers=4)
        self.assertEqual(read_results, {f"r{idx}": idx for idx in range(5)})

    def test_each_reader_runs_once_on_worker(self) -> None:
        # Re-entrant workers must not re-submit a reader (and
        # the dispatch logic must not double-collect). The set of
        # observed thread ids should be > 1 to confirm actual
        # parallelism, but the exact count depends on scheduling so
        # we only assert it ran on a non-main thread when more than
        # one reader was submitted.
        _, dashboard = _reload()
        calls: dict[str, int] = {}
        threads: set[int] = set()
        lock = threading.Lock()

        readers = [
            (
                f"r{idx}",
                partial(_record_threaded_reader, f"r{idx}", calls, threads, lock),
            )
            for idx in range(8)
        ]
        dashboard._fan_out_reads(readers, parallel=True, max_workers=4)
        self.assertEqual(set(calls.values()), {1})
        self.assertEqual(set(calls), {f"r{idx}" for idx in range(8)})
        self.assertNotIn(threading.get_ident(), threads)

    def test_parallel_wall_clock_beats_sequential_sum(self) -> None:
        # Smoke: with 4 workers and 4 readers each sleeping ~80 ms, the
        # wall-clock should be much closer to one reader's runtime than
        # to the sum. Pin a loose ceiling so the test is not flaky on a
        # busy CI host but still fails if the executor degenerates to
        # the sequential path.
        _, dashboard = _reload()
        delay = 0.08
        read_results, elapsed = _timed_parallel_fanout(dashboard, delay)
        self.assertEqual(len(read_results), 4)
        # Sequential sum would be 4 * delay = 320 ms; one wave on
        # four workers should land well under 2 * delay.
        self.assertLess(elapsed, delay * _BEATS_SEQUENTIAL_SUM_ELAPSED)

    def test_reader_exception_propagates(self) -> None:
        # `AnalyticsReadError` raised in a worker must surface from
        # the helper so the caller's `try/except AnalyticsReadError`
        # in `main()` can render a single `st.error` and stop.
        _, dashboard = _reload()
        read_error = _analytics_read_module().AnalyticsReadError

        readers = [
            ("ok", partial(_return_value, 1)),
            ("boom", partial(_raise_read_error, "query failed")),
        ]
        with self.assertRaisesRegex(read_error, "query failed"):
            dashboard._fan_out_reads(readers, parallel=True, max_workers=2)
