# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics retention logging and append-coordination tests."""

import contextlib


import json


import os


import tempfile


import threading


import unittest


from datetime import datetime, timezone


from pathlib import Path


from unittest.mock import MagicMock, patch


from tests.analytics_reload_helpers import reload_analytics as _reload


from tests.analytics_jsonl_helpers import (
    read_lines as _read_lines,
    timestamp_days_ago as _ts_days_ago,
)


_REPO_KEY = 'repo'


_TIMESTAMP_KEY = 'ts'


_ISSUE_KEY = 'issue'


_EVENT_KEY = 'event'


_PRUNE_NOW_DAY = 25


_PRUNE_NOW_HOUR = 12


_APPEND_TIMEOUT = 5.0


_FINISH_TIMEOUT = 5.0


DEFAULT_RETENTION_DAYS = 90


_YEAR = 2026


PRUNE_NOW = datetime(_YEAR, 5, _PRUNE_NOW_DAY, _PRUNE_NOW_HOUR, 0, 0, tzinfo=timezone.utc)


FRESH_RECORD_AGE_DAYS = 1


VERY_OLD_RECORD_AGE_DAYS = 200


_REPO_SHORT = "o/r"


_STAGE_IMPLEMENTING = "implementing"


_STAGE_ENTER = "stage_enter"


_ENCODING = "utf-8"


class _PruneAppendRace:
    def __init__(self, analytics, timestamp: str) -> None:
        self.analytics = analytics
        self.timestamp = timestamp
        self.after_read = threading.Event()
        self.appender_done = threading.Event()
        self._real_replace = os.replace

    def replace(self, source, destination):
        self.after_read.set()
        self.appender_done.wait(timeout=0.5)
        return self._real_replace(source, destination)

    def append(self) -> None:
        self.after_read.wait(timeout=_APPEND_TIMEOUT)
        self.analytics.append_record(
            {
                _TIMESTAMP_KEY: self.timestamp,
                _REPO_KEY: _REPO_SHORT,
                _ISSUE_KEY: 99,
                _EVENT_KEY: _STAGE_ENTER,
            }
        )
        self.appender_done.set()

    def finish(self, thread: threading.Thread) -> None:
        self.after_read.set()
        thread.join(timeout=_FINISH_TIMEOUT)


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


_ANALYTICS_RETENTION_DAYS = "ANALYTICS_RETENTION_DAYS"


_DEFAULT_RETENTION_STR = str(DEFAULT_RETENTION_DAYS)


def _write_json_records(path: Path, records: list[dict]) -> None:
    lines = "\n".join(json.dumps(record) for record in records)
    path.write_text(
        "{0}\n".format(lines),
        encoding=_ENCODING,
    )


def _run_prune_race(analytics, fresh_timestamp: str) -> int:
    race = _PruneAppendRace(analytics, fresh_timestamp)
    appender_thread = threading.Thread(target=race.append)
    appender_thread.start()
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(race.finish, appender_thread)
        with patch.object(analytics.os, "replace", race.replace):
            return analytics.prune_old_records(now=PRUNE_NOW)


def _issue_numbers(path: Path) -> list[int]:
    records = [json.loads(line) for line in _read_lines(path)]
    return sorted(record[_ISSUE_KEY] for record in records)


@contextlib.contextmanager
def _reject_github_mutations(client_type, method_names: tuple[str, ...]):
    with contextlib.ExitStack() as guards:
        for method_name in method_names:
            guards.enter_context(
                patch.object(
                    client_type,
                    method_name,
                    MagicMock(
                        side_effect=AssertionError(
                            f"prune must not call GitHubClient.{method_name}"
                        ),
                    ),
                )
            )
        yield


class PruneWithRetentionLoggingTest(unittest.TestCase):
    """`prune_with_retention_logging` is the per-tick wrapper that
    `main._run_tick` calls. It delegates to `prune_old_records`, catches
    runaway exceptions so an analytics misconfiguration cannot abort the
    polling loop, and logs the removed-record count. The helper itself
    is local-filesystem only -- the prune never imports `github`, so it
    cannot mutate pinned GitHub state regardless of where it is called
    from.
    """

    def test_delegates_to_prune_old_records(self) -> None:
        _, analytics = _reload()
        with patch.object(
            analytics,
            "prune_old_records",
            return_value=0,
        ) as prune:
            analytics.prune_with_retention_logging()
            prune.assert_called_once_with()

    def test_exception_is_swallowed(self) -> None:
        # A runaway error inside `prune_old_records` must not propagate
        # -- analytics is observability, never authoritative workflow
        # state, so a misconfiguration must not abort the polling loop.
        _, analytics = _reload()
        with patch.object(
            analytics,
            "prune_old_records",
            side_effect=RuntimeError("boom"),
        ):
            # No raise: the wrapper logs and swallows.
            analytics.prune_with_retention_logging()

    def test_parallel_append_survives_prune(self) -> None:
        # Regression: under the scheduler-driven dispatch in
        # `main._run_tick`, `workflow.tick` returns as soon as the
        # per-issue callables have been submitted to the scheduler,
        # so `analytics.prune_with_retention_logging()` can run while
        # scheduler workers are still calling `append_record()`.
        # Without a shared lock, an append that landed between
        # `prune_old_records`'s read and its `os.replace` would be
        # written to the soon-unlinked inode and silently lost.
        # The fix takes `_FILE_LOCK` around both operations.
        #
        # This test forces the race by patching the file ops inside
        # `prune_old_records` so the read happens, then the appender
        # thread fires, then the rewrite (`os.replace`) finishes --
        # exactly the window the lock has to close. With the lock in
        # place, the appender blocks until the prune releases it, so
        # its line is preserved.
        with tempfile.TemporaryDirectory(prefix="analytics-race-") as td:
            path = Path(td) / "analytics.jsonl"
            fresh_timestamp = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=PRUNE_NOW)
            # One old record (will be pruned) plus one recent record
            # (the prune rewrite must keep it). After the rewrite, an
            # appender adds a fresh record concurrently; the prune
            # must NOT drop it.
            _write_json_records(
                path,
                [
                    {
                        _TIMESTAMP_KEY: _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=PRUNE_NOW),
                        _REPO_KEY: _REPO_SHORT,
                        _ISSUE_KEY: 1,
                        _EVENT_KEY: _STAGE_ENTER,
                    },
                    {
                        _TIMESTAMP_KEY: fresh_timestamp,
                        _REPO_KEY: _REPO_SHORT,
                        _ISSUE_KEY: 2,
                        _EVENT_KEY: _STAGE_ENTER,
                    },
                ],
            )
            analytics = _reload(
                {
                    _ANALYTICS_LOG_PATH: str(path),
                    _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                }
            )[1]

            # The replace callback opens the real post-read race window while
            # the append callback contends on analytics' file lock.
            self.assertEqual(_run_prune_race(analytics, fresh_timestamp), 1)
            # The old record (issue=1) is gone. Both the kept record
            # (issue=2) and the concurrent append (issue=99) survive.
            self.assertEqual(_issue_numbers(path), [2, 99])

    def test_prune_rewrites_without_github_writes(self) -> None:
        # "Analytics is not authoritative workflow state" enforced at
        # the boundary: the prune helper takes no GitHub client and the
        # real `prune_old_records` implementation never imports `github`
        # at all. This pairs with the main-loop wiring tests in
        # `tests/test_main.py`: those verify the wrapper is called once
        # per tick; this verifies that calling it cannot mutate pinned
        # state through any client method.
        from orchestrator.github import GitHubClient

        with tempfile.TemporaryDirectory(prefix="analytics-retention-") as td:
            path = Path(td) / "analytics.jsonl"
            _write_json_records(
                path,
                [
                    {
                        _TIMESTAMP_KEY: _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=PRUNE_NOW),
                        _REPO_KEY: _REPO_SHORT,
                        _ISSUE_KEY: 1,
                        _EVENT_KEY: _STAGE_ENTER,
                        "stage": _STAGE_IMPLEMENTING,
                    },
                    {
                        _TIMESTAMP_KEY: _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=PRUNE_NOW),
                        _REPO_KEY: _REPO_SHORT,
                        _ISSUE_KEY: 2,
                        _EVENT_KEY: "stage_evaluation",
                        "stage": "validating",
                        "duration_s": 0.001,
                        "result": "ok",
                    },
                ],
            )
            analytics = _reload(
                {
                    _ANALYTICS_LOG_PATH: str(path),
                    _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                }
            )[1]
            # Patch every GitHub-mutating method on the class so the
            # prune cannot side-effect through any client instance that
            # some future refactor accidentally routes it through.
            with _reject_github_mutations(
                GitHubClient,
                (
                    "write_pinned_state",
                    "comment",
                    "set_workflow_label",
                    "create_child_issue",
                    "open_pr",
                    "pr_comment",
                    "merge_pr",
                    "delete_remote_branch",
                    "emit_event",
                ),
            ):
                self.assertEqual(analytics.prune_old_records(now=PRUNE_NOW), 1)
            self.assertEqual(_issue_numbers(path), [2])
