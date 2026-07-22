# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync CLI option tests."""

import io


import re


import tempfile


import unittest


from dataclasses import dataclass


from datetime import datetime, timezone


from pathlib import Path


from unittest.mock import MagicMock, patch


from tests.analytics_sync_execution import (
    reset_root_logger as _reset_root_logger,
)


from tests.analytics_sync_reload import (
    reload_sync as _reload,
)


from tests.analytics_sync_payloads import (
    write_jsonl as _write_jsonl,
    sample_record as _sample_record,
)


CLI_CLOCK_TOLERANCE_SECONDS = 5


_LOG_PATH_ENV = "ANALYTICS_LOG_PATH"


_DB_URL_ENV = "ANALYTICS_DB_URL"


_SENTINEL_DISABLED = "off"


_STDOUT = "sys.stdout"


@dataclass(frozen=True)
class _CliStreams:
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _CliClock:
    stdout: datetime
    stderr: datetime
    stdout_label: str
    stderr_label: str


def _capture_cli_streams(test_case, analytics_sync) -> _CliStreams:
    error_buffer = io.StringIO()
    output_buffer = io.StringIO()
    test_case.addCleanup(_reset_root_logger)
    with patch("sys.stderr", error_buffer), patch(_STDOUT, output_buffer):
        test_case.assertEqual(analytics_sync.main([]), 0)
    return _CliStreams(output_buffer.getvalue(), error_buffer.getvalue())


def _parse_cli_clock(streams: _CliStreams) -> _CliClock:
    timestamp_pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")
    stdout_match = timestamp_pattern.search(streams.stdout)
    stderr_match = timestamp_pattern.search(streams.stderr)
    assert stdout_match is not None
    assert stderr_match is not None
    timestamp_format = "%Y-%m-%d %H:%M:%S"
    return _CliClock(
        datetime.strptime(stdout_match.group(1), timestamp_format),
        datetime.strptime(stderr_match.group(1), timestamp_format),
        stdout_match.group(1),
        stderr_match.group(1),
    )


class AnalyticsSyncCliTest(unittest.TestCase):
    """The CLI prints a one-line summary on success and exits 1 on
    failure so a cron / systemd unit can surface the error.
    """

    def test_cli_no_op_prints_zeros(self) -> None:
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: "",
            }
        )
        buf = io.StringIO()
        with patch(_STDOUT, buf):
            rc = analytics_sync.main([])
        self.assertEqual(rc, 0)
        self.assertIn("inserted=0", buf.getvalue())
        self.assertIn("duplicate=0", buf.getvalue())

    def test_cli_overrides_take_effect(self) -> None:
        # `--log-path` / `--db-url` should override the configured
        # values for one-off replays of archived logs.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rotated.jsonl"
            _write_jsonl(path, [_sample_record()])
            _, analytics_sync = _reload(
                {
                    _LOG_PATH_ENV: _SENTINEL_DISABLED,
                    _DB_URL_ENV: "",
                }
            )

            sync_mock = MagicMock(
                return_value=analytics_sync.SyncResult(inserted=1, total_lines=1),
            )
            with patch.object(
                analytics_sync,
                "sync_jsonl_to_postgres",
                sync_mock,
            ):
                buf = io.StringIO()
                with patch(_STDOUT, buf):
                    self.assertEqual(
                        analytics_sync.main(
                            [
                                "--log-path",
                                str(path),
                                "--db-url",
                                "postgresql://override/db",
                            ]
                        ),
                        0,
                    )
            self.assertIn("inserted=1", buf.getvalue())
            sync_mock.assert_called_once()
            self.assertEqual(sync_mock.call_args.kwargs["log_path"], path)
            self.assertEqual(
                sync_mock.call_args.kwargs["db_url"],
                "postgresql://override/db",
            )

    def test_cli_surfaces_failure_as_nonzero(self) -> None:
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: "",
            }
        )
        with patch.object(
            analytics_sync,
            "sync_jsonl_to_postgres",
            side_effect=RuntimeError("boom"),
        ):
            buf = io.StringIO()
            with patch(_STDOUT, buf):
                rc = analytics_sync.main([])
        self.assertEqual(rc, 1)

    def test_cli_logs_and_stdout_share_utc_clock(self) -> None:
        # Regression for the reviewer's TZ-skew finding: log lines used
        # to print in local time while the stdout summary printed UTC,
        # so on a TZ+7 host the two surfaces were 7 hours apart for the
        # same event. With both pinned to UTC + an explicit "UTC"
        # marker, mixing stdout/stderr stays a coherent time stream.
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: "",
            }
        )
        streams = _capture_cli_streams(self, analytics_sync)
        # Both surfaces must carry the explicit "UTC" marker so a
        # mixed-stream consumer (a piped `2>&1`) can tell the
        # timestamps share a timezone.
        self.assertIn(" UTC ", streams.stdout)
        self.assertIn(" UTC ", streams.stderr)
        # Extract one timestamp from each surface and confirm they
        # match within a few seconds. If the log had defaulted to
        # local time (the reviewer's TZ+7 bug), the delta would be
        # measured in hours.
        clock = _parse_cli_clock(streams)
        self.assertLess(
            abs((clock.stdout - clock.stderr).total_seconds()),
            CLI_CLOCK_TOLERANCE_SECONDS,
            f"stdout and stderr timestamps disagree: out={clock.stdout_label} err={clock.stderr_label}",
        )
        # Cross-check against `now()` to confirm the shared clock is
        # actually UTC, not just any single tz. A local-time formatter
        # would land outside this window on a TZ-skewed host.
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(
            abs((clock.stdout - now_utc).total_seconds()),
            CLI_CLOCK_TOLERANCE_SECONDS,
            "stdout summary timestamp is not UTC",
        )
        self.assertLess(
            abs((clock.stderr - now_utc).total_seconds()),
            CLI_CLOCK_TOLERANCE_SECONDS,
            "log timestamp is not UTC",
        )

    def test_stdout_has_timestamp_and_duration(self) -> None:
        # Operators run the sync from a terminal and expect a timestamped,
        # one-line summary with the elapsed wall-clock so a multi-thousand
        # record replay surfaces its cost without grepping the log lines.
        _, analytics_sync = _reload(
            {
                _LOG_PATH_ENV: _SENTINEL_DISABLED,
                _DB_URL_ENV: "",
            }
        )
        buf = io.StringIO()
        with patch(_STDOUT, buf):
            rc = analytics_sync.main([])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # The leading `YYYY-MM-DD HH:MM:SS UTC` timestamp gives an
        # operator mixing stdout + stderr the same wall-clock anchor
        # the log formatter prepends; the explicit "UTC" marker is
        # what makes the two streams comparable on a TZ-skewed host.
        # A missing timestamp -- or a missing tz marker -- is a
        # regression.
        self.assertRegex(
            out,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC analytics_sync:",
        )
        self.assertIn("duration_s=", out)
