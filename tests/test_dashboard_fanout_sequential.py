# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sequential dashboard reader fan-out tests."""

import unittest


from functools import partial


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_analytics_read as _analytics_read_module,
)

_FIRST_VALUE = 'a'
_THIRD_VALUE = 'c'
_SECOND_VALUE = 'b'


def _record_reader_call(name: str, payload: int, calls: list[str]) -> int:
    calls.append(name)
    return payload


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


def _increment_reader_count(name: str, counts: dict[str, int]) -> str:
    counts[name] = counts.get(name, 0) + 1
    return name


class FanOutReadsSequentialTest(unittest.TestCase):
    """The sequential branch of `_fan_out_reads` runs each reader in
    submission order on the calling thread and returns results keyed
    by reader name. The helper lets each staged wave dispatch its bound
    cached-reader tasks through one path and lets tests inject fake
    readers without booting Streamlit.
    """

    def test_results_keep_name_and_submit_order(self) -> None:
        _, dashboard = _reload()
        order: list[str] = []

        readers = [
            (_FIRST_VALUE, partial(_record_reader_call, _FIRST_VALUE, 1, order)),
            (_SECOND_VALUE, partial(_record_reader_call, _SECOND_VALUE, 2, order)),
            (_THIRD_VALUE, partial(_record_reader_call, _THIRD_VALUE, 3, order)),
        ]
        read_results = dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(read_results, {_FIRST_VALUE: 1, _SECOND_VALUE: 2, _THIRD_VALUE: 3})
        # Sequential path runs in submission order so a deterministic
        # log line / error message references the right reader.
        self.assertEqual(order, [_FIRST_VALUE, _SECOND_VALUE, _THIRD_VALUE])

    def test_first_failing_reader_propagates(self) -> None:
        # Sequential path stops at the first error so the caller
        # surfaces one user-friendly message instead of a stack of
        # errors.
        _, dashboard = _reload()
        read_error = _analytics_read_module().AnalyticsReadError
        called: list[str] = []

        readers = [
            (_FIRST_VALUE, partial(_record_reader_call, "ok", 1, called)),
            (_SECOND_VALUE, partial(_raise_read_error, "connection refused", called, "boom")),
            (_THIRD_VALUE, partial(_record_reader_call, "never", 2, called)),
        ]
        with self.assertRaises(read_error):
            dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(called, ["ok", "boom"])

    def test_each_reader_runs_exactly_once(self) -> None:
        _, dashboard = _reload()
        counts = {_FIRST_VALUE: 0, _SECOND_VALUE: 0}

        readers = [
            (_FIRST_VALUE, partial(_increment_reader_count, _FIRST_VALUE, counts)),
            (_SECOND_VALUE, partial(_increment_reader_count, _SECOND_VALUE, counts)),
        ]
        dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(counts, {_FIRST_VALUE: 1, _SECOND_VALUE: 1})
