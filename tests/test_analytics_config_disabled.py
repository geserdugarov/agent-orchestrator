# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics disabled-sink tests."""

import tempfile


import unittest


from pathlib import Path


from tests.analytics_reload_helpers import reload_analytics as _reload


_EVENT_VALUE = 'x'


_REPO_SHORT = "o/r"


_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"


class AnalyticsDisabledModeTest(unittest.TestCase):
    """With the sink disabled, both `append_record` and
    `prune_old_records` are silent no-ops -- no file is ever opened,
    pinned GitHub state is untouched, and the helpers do not raise.
    """

    def test_append_creates_no_file_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            _, analytics = _reload({_ANALYTICS_LOG_PATH: ""})
            analytics.append_record(analytics.build_record(repo=_REPO_SHORT, issue=1, event=_EVENT_VALUE))
            self.assertFalse(sentinel.exists())
            # Directory should also stay empty.
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_prune_returns_zero_when_disabled(self) -> None:
        _, analytics = _reload({_ANALYTICS_LOG_PATH: "off"})
        self.assertEqual(analytics.prune_old_records(), 0)

    def test_disabled_sink_does_not_create_log_dir(self) -> None:
        # Important: disabling must not trigger LOG_DIR creation either.
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            _, analytics = _reload(
                {
                    "LOG_DIR": str(log_dir),
                    _ANALYTICS_LOG_PATH: "off",
                }
            )
            analytics.append_record(analytics.build_record(repo=_REPO_SHORT, issue=1, event=_EVENT_VALUE))
            self.assertFalse(log_dir.exists())
