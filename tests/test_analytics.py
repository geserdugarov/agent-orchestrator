# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        env.update(extra)
    return env


def _reload(env: dict[str, str] | None = None):
    """Reload `orchestrator.config` and `orchestrator.analytics` against
    the given hermetic env. Returns both modules so tests can poke at
    config knobs and call analytics helpers from the same load.
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics", None)
        import orchestrator.config as config
        import orchestrator.analytics as analytics
        return config, analytics


class AnalyticsConfigTest(unittest.TestCase):
    """`ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` parse at import:
    default-enabled under `LOG_DIR`, sentinel values disable, retention
    defaults to 90 days and 0 means keep raw data indefinitely.
    """

    def test_default_path_under_log_dir(self) -> None:
        config, _ = _reload()
        self.assertEqual(
            config.ANALYTICS_LOG_PATH, config.LOG_DIR / "analytics.jsonl"
        )

    def test_default_retention_is_ninety_days(self) -> None:
        config, _ = _reload()
        self.assertEqual(config.ANALYTICS_RETENTION_DAYS, 90)

    def test_explicit_path_overrides_default(self) -> None:
        config, _ = _reload({"ANALYTICS_LOG_PATH": "/var/log/orch/a.jsonl"})
        self.assertEqual(
            config.ANALYTICS_LOG_PATH, Path("/var/log/orch/a.jsonl")
        )

    def test_empty_value_disables(self) -> None:
        # Explicit empty assignment in .env is the documented disable knob.
        config, _ = _reload({"ANALYTICS_LOG_PATH": ""})
        self.assertIsNone(config.ANALYTICS_LOG_PATH)

    def test_sentinel_values_disable(self) -> None:
        for value in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(value=value):
                config, _ = _reload({"ANALYTICS_LOG_PATH": value})
                self.assertIsNone(config.ANALYTICS_LOG_PATH)

    def test_zero_retention_means_keep_forever(self) -> None:
        config, _ = _reload({"ANALYTICS_RETENTION_DAYS": "0"})
        self.assertEqual(config.ANALYTICS_RETENTION_DAYS, 0)

    def test_retention_env_override(self) -> None:
        config, _ = _reload({"ANALYTICS_RETENTION_DAYS": "30"})
        self.assertEqual(config.ANALYTICS_RETENTION_DAYS, 30)


class AnalyticsDisabledModeTest(unittest.TestCase):
    """With the sink disabled, both `append_record` and
    `prune_old_records` are silent no-ops -- no file is ever opened,
    pinned GitHub state is untouched, and the helpers do not raise.
    """

    def test_append_creates_no_file_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            _, analytics = _reload({"ANALYTICS_LOG_PATH": ""})
            analytics.append_record(
                analytics.build_record(repo="o/r", issue=1, event="x")
            )
            self.assertFalse(sentinel.exists())
            # Directory should also stay empty.
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_prune_returns_zero_when_disabled(self) -> None:
        _, analytics = _reload({"ANALYTICS_LOG_PATH": "off"})
        self.assertEqual(analytics.prune_old_records(), 0)

    def test_disabled_sink_does_not_create_log_dir(self) -> None:
        # Important: disabling must not trigger LOG_DIR creation either.
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            _, analytics = _reload({
                "LOG_DIR": str(log_dir),
                "ANALYTICS_LOG_PATH": "off",
            })
            analytics.append_record(
                analytics.build_record(repo="o/r", issue=1, event="x")
            )
            self.assertFalse(log_dir.exists())


class AnalyticsAppendTest(unittest.TestCase):
    """`build_record` produces the documented base fields and
    `append_record` writes one well-formed JSONL line per call.
    """

    def test_record_has_required_base_fields(self) -> None:
        _, analytics = _reload()
        rec = analytics.build_record(
            repo="o/r", issue=42, event="stage_enter", stage="implementing"
        )
        self.assertIn("ts", rec)
        self.assertEqual(rec["repo"], "o/r")
        self.assertEqual(rec["issue"], 42)
        self.assertEqual(rec["event"], "stage_enter")
        self.assertEqual(rec["stage"], "implementing")
        parsed = datetime.fromisoformat(rec["ts"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_stage_omitted_when_none(self) -> None:
        _, analytics = _reload()
        rec = analytics.build_record(repo="o/r", issue=1, event="pr_opened")
        self.assertNotIn("stage", rec)

    def test_none_valued_extras_are_dropped(self) -> None:
        _, analytics = _reload()
        rec = analytics.build_record(
            repo="o/r", issue=1, event="agent_spawn",
            session_id=None, retry_count=2,
        )
        self.assertNotIn("session_id", rec)
        self.assertEqual(rec["retry_count"], 2)

    def test_append_writes_one_line_per_record(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({"ANALYTICS_LOG_PATH": str(path)})
            analytics.append_record(
                analytics.build_record(
                    repo="o/r", issue=1, event="stage_enter",
                    stage="implementing",
                )
            )
            analytics.append_record(
                analytics.build_record(
                    repo="o/r", issue=2, event="pr_opened", pr_number=5,
                )
            )
            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            rec0 = json.loads(lines[0])
            self.assertEqual(rec0["issue"], 1)
            self.assertEqual(rec0["event"], "stage_enter")
            self.assertEqual(rec0["stage"], "implementing")
            rec1 = json.loads(lines[1])
            self.assertEqual(rec1["pr_number"], 5)
            self.assertNotIn("stage", rec1)

    def test_append_creates_missing_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a" / "b" / "c" / "analytics.jsonl"
            _, analytics = _reload({"ANALYTICS_LOG_PATH": str(path)})
            analytics.append_record(
                analytics.build_record(repo="o/r", issue=1, event="x")
            )
            self.assertTrue(path.exists())

    def test_append_is_append_only(self) -> None:
        # Repeated appends must accumulate, never overwrite prior records.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({"ANALYTICS_LOG_PATH": str(path)})
            for n in range(5):
                analytics.append_record(
                    analytics.build_record(repo="o/r", issue=n, event="x")
                )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 5)
            issues = [json.loads(line)["issue"] for line in lines]
            self.assertEqual(issues, list(range(5)))


class AnalyticsPruneTest(unittest.TestCase):
    """`prune_old_records` removes records whose `ts` precedes
    `ANALYTICS_RETENTION_DAYS`, keeps newer records, no-ops when
    retention is 0 (keep forever) or the file is absent, and preserves
    malformed lines so cleanup is operator-driven.
    """

    @staticmethod
    def _write_lines(path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def test_removes_old_records_keeps_recent(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat(timespec="seconds")
        new_ts = (now - timedelta(days=10)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "90",
            })
            self._write_lines(path, [
                {"ts": old_ts, "repo": "o/r", "issue": 1, "event": "x"},
                {"ts": new_ts, "repo": "o/r", "issue": 2, "event": "y"},
                {"ts": old_ts, "repo": "o/r", "issue": 3, "event": "z"},
            ])
            removed = analytics.prune_old_records(now=now)
            self.assertEqual(removed, 2)
            remaining = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["issue"], 2)

    def test_zero_retention_is_no_op(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        ancient = (now - timedelta(days=1000)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "0",
            })
            self._write_lines(path, [
                {"ts": ancient, "repo": "o/r", "issue": 1, "event": "x"},
            ])
            removed = analytics.prune_old_records(now=now)
            self.assertEqual(removed, 0)
            # File contents unchanged.
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)

    def test_negative_retention_is_no_op(self) -> None:
        # Treated identically to the documented `0 = keep forever` knob.
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=100)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "-5",
            })
            self._write_lines(path, [
                {"ts": old_ts, "repo": "o/r", "issue": 1, "event": "x"},
            ])
            self.assertEqual(analytics.prune_old_records(now=now), 0)

    def test_missing_file_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent.jsonl"
            _, analytics = _reload({"ANALYTICS_LOG_PATH": str(path)})
            self.assertEqual(analytics.prune_old_records(), 0)
            self.assertFalse(path.exists())

    def test_no_records_old_enough_does_not_rewrite(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        new_ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "90",
            })
            self._write_lines(path, [
                {"ts": new_ts, "repo": "o/r", "issue": 1, "event": "x"},
            ])
            mtime_before = path.stat().st_mtime_ns
            removed = analytics.prune_old_records(now=now)
            self.assertEqual(removed, 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_malformed_lines_preserved(self) -> None:
        # Non-JSON lines, JSON without `ts`, and unparseable `ts` strings
        # survive the prune so operators can clean up rather than having
        # the helper silently drop data it cannot interpret.
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=200)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "90",
            })
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write("this is not json\n")
                fh.write(json.dumps(
                    {"ts": old_ts, "repo": "o/r", "issue": 1, "event": "x"}
                ) + "\n")
                fh.write('{"ts": "not-a-date", "event": "y"}\n')
                fh.write('{"event": "no-ts-field"}\n')
            removed = analytics.prune_old_records(now=now)
            # Only the parseable old record is removed; the three other
            # malformed-or-missing-ts lines survive.
            self.assertEqual(removed, 1)
            kept = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(kept), 3)
            self.assertIn("this is not json", kept[0])

    def test_naive_timestamp_treated_as_utc(self) -> None:
        # Pre-existing records written without tz info (or by an older
        # writer) must still be comparable; treat them as UTC rather than
        # raising and aborting the prune.
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        old_naive = (now - timedelta(days=100)).replace(
            tzinfo=None
        ).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analytics.jsonl"
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "90",
            })
            self._write_lines(path, [
                {"ts": old_naive, "repo": "o/r", "issue": 1, "event": "x"},
            ])
            self.assertEqual(analytics.prune_old_records(now=now), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
