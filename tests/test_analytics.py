# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


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


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _claude_stdout_with_skills(
    *,
    skills: tuple[str, ...],
    offered: tuple[str, ...] = (),
    args_marker: str = "skill-args-must-never-be-stored",
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> str:
    """A claude stream-json stdout that both reports usage AND triggers
    `Skill` tool_use blocks.

    Each name in `skills` becomes one `tool_use` block named `"Skill"`
    whose `input` carries the name plus an `args` string we assert never
    reaches the analytics record (Privacy: only the skill name is read).
    The single `assistant` frame also carries a `usage` block so the
    baseline usage/cost record is produced regardless of the skill switch.

    When `offered` is non-empty a `system`/`init` frame carrying that
    `skills` array is prepended -- the dedicated offered-skills source the
    real claude stream exposes, so the extractor populates `available`.
    """
    content = [
        {
            "type": "tool_use",
            "name": "Skill",
            "id": f"toolu_{i}",
            "input": {"skill": name, "args": args_marker},
        }
        for i, name in enumerate(skills)
    ]
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-skill",
            "model": "claude-sonnet-4-6",
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    }
    result_frame = {"type": "result", "num_turns": 1}
    frames = [assistant, result_frame]
    if offered:
        frames.insert(
            0, {"type": "system", "subtype": "init", "skills": list(offered)}
        )
    return "\n".join(json.dumps(f) for f in frames)


class AnalyticsConfigTest(unittest.TestCase):
    """`ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` parse at import
    inside the analytics package: default-enabled under `config.LOG_DIR`,
    sentinel values disable, retention defaults to 90 days and 0 means
    keep raw data indefinitely.
    """

    def test_default_path_under_log_dir(self) -> None:
        config, analytics = _reload()
        self.assertEqual(
            analytics.ANALYTICS_LOG_PATH, config.LOG_DIR / "analytics.jsonl"
        )

    def test_default_retention_is_ninety_days(self) -> None:
        _, analytics = _reload()
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, 90)

    def test_explicit_path_overrides_default(self) -> None:
        _, analytics = _reload({"ANALYTICS_LOG_PATH": "/var/log/orch/a.jsonl"})
        self.assertEqual(
            analytics.ANALYTICS_LOG_PATH, Path("/var/log/orch/a.jsonl")
        )

    def test_empty_value_disables(self) -> None:
        # Explicit empty assignment in .env is the documented disable knob.
        _, analytics = _reload({"ANALYTICS_LOG_PATH": ""})
        self.assertIsNone(analytics.ANALYTICS_LOG_PATH)

    def test_sentinel_values_disable(self) -> None:
        for value in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(value=value):
                _, analytics = _reload({"ANALYTICS_LOG_PATH": value})
                self.assertIsNone(analytics.ANALYTICS_LOG_PATH)

    def test_zero_retention_means_keep_forever(self) -> None:
        _, analytics = _reload({"ANALYTICS_RETENTION_DAYS": "0"})
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, 0)

    def test_retention_env_override(self) -> None:
        _, analytics = _reload({"ANALYTICS_RETENTION_DAYS": "30"})
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, 30)


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
            analytics, "prune_old_records", return_value=0,
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

    def test_concurrent_append_during_prune_is_not_lost(self) -> None:
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
        import threading
        with tempfile.TemporaryDirectory(prefix="analytics-race-") as td:
            path = Path(td) / "analytics.jsonl"
            now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
            old_ts = (now - timedelta(days=200)).isoformat(timespec="seconds")
            new_ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
            # One old record (will be pruned) plus one recent record
            # (the prune rewrite must keep it). After the rewrite, an
            # appender adds a fresh record concurrently; the prune
            # must NOT drop it.
            path.write_text(
                json.dumps({
                    "ts": old_ts, "repo": "o/r", "issue": 1,
                    "event": "stage_enter",
                }) + "\n"
                + json.dumps({
                    "ts": new_ts, "repo": "o/r", "issue": 2,
                    "event": "stage_enter",
                }) + "\n",
                encoding="utf-8",
            )
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "90",
            })

            after_read = threading.Event()
            appender_done = threading.Event()
            real_replace = os.replace

            def gated_replace(src, dst):
                # The prune's `os.replace` runs after the kept-records
                # rewrite. By the time we get here, the prune has read
                # the original file and built the kept list. Signal
                # the appender to fire BEFORE the replace lands so
                # the appender's `open("a")` would race the rewrite
                # without the lock. The fix is that the appender's
                # `_FILE_LOCK.acquire()` blocks on the prune's still-
                # held lock, so this call returns before the appender
                # actually opens the file.
                after_read.set()
                # Wait for the appender to attempt its acquire. The
                # lock blocks the appender; this event just confirms
                # the appender has reached the try-acquire point.
                appender_done.wait(timeout=0.5)
                return real_replace(src, dst)

            def appender() -> None:
                # Wait for the prune to finish its read so the race
                # window is real (without the lock the appender's
                # write would land on the soon-unlinked inode).
                after_read.wait(timeout=5.0)
                analytics.append_record({
                    "ts": new_ts, "repo": "o/r", "issue": 99,
                    "event": "stage_enter",
                })
                appender_done.set()

            t = threading.Thread(target=appender)
            t.start()
            try:
                with patch.object(analytics.os, "replace", gated_replace):
                    removed = analytics.prune_old_records(now=now)
            finally:
                # Make sure the appender is unblocked even if the
                # prune raised; the wait above is bounded.
                after_read.set()
                t.join(timeout=5.0)

            self.assertEqual(removed, 1)
            remaining = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            issues = sorted(r["issue"] for r in remaining)
            # The old record (issue=1) is gone. Both the kept record
            # (issue=2) and the concurrent append (issue=99) survive.
            self.assertEqual(issues, [2, 99])

    def test_helper_rewrites_file_without_github_writes(self) -> None:
        # "Analytics is not authoritative workflow state" enforced at
        # the boundary: the prune helper takes no GitHub client and the
        # real `prune_old_records` implementation never imports `github`
        # at all. This pairs with the main-loop wiring tests in
        # `tests/test_main.py`: those verify the wrapper is called once
        # per tick; this verifies that calling it cannot mutate pinned
        # state through any client method.
        from orchestrator.github import GitHubClient

        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=200)).isoformat(timespec="seconds")
        new_ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory(prefix="analytics-retention-") as td:
            path = Path(td) / "analytics.jsonl"
            path.write_text(
                json.dumps({
                    "ts": old_ts, "repo": "o/r", "issue": 1,
                    "event": "stage_enter", "stage": "implementing",
                }) + "\n"
                + json.dumps({
                    "ts": new_ts, "repo": "o/r", "issue": 2,
                    "event": "stage_evaluation", "stage": "validating",
                    "duration_s": 0.001, "result": "ok",
                }) + "\n",
                encoding="utf-8",
            )
            _, analytics = _reload({
                "ANALYTICS_LOG_PATH": str(path),
                "ANALYTICS_RETENTION_DAYS": "90",
            })
            mutators = (
                "write_pinned_state", "comment", "set_workflow_label",
                "create_child_issue", "open_pr", "pr_comment",
                "merge_pr", "delete_remote_branch", "emit_event",
            )
            # Patch every GitHub-mutating method on the class so the
            # prune cannot side-effect through any client instance that
            # some future refactor accidentally routes it through.
            patchers = [
                patch.object(
                    GitHubClient,
                    name,
                    MagicMock(
                        side_effect=AssertionError(
                            f"prune must not call GitHubClient.{name}"
                        ),
                    ),
                )
                for name in mutators
            ]
            for p in patchers:
                p.start()
            try:
                removed = analytics.prune_old_records(now=now)
            finally:
                for p in patchers:
                    p.stop()
            self.assertEqual(removed, 1)
            remaining = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["issue"], 2)


class SkillTriggerConfigTest(unittest.TestCase):
    """`TRACK_SKILL_TRIGGERS` parses at import inside the analytics package,
    defaults off, is exported in `__all__`, and honors the same truthy
    spellings as the other boolean knobs in `orchestrator.config`."""

    def test_defaults_off_and_is_exported(self) -> None:
        _, analytics = _reload()
        self.assertFalse(analytics.TRACK_SKILL_TRIGGERS)
        self.assertIn("TRACK_SKILL_TRIGGERS", analytics.__all__)

    def test_truthy_spellings_enable(self) -> None:
        for value in ("1", "true", "on", "yes", "On", " YES "):
            with self.subTest(value=value):
                _, analytics = _reload({"TRACK_SKILL_TRIGGERS": value})
                self.assertTrue(analytics.TRACK_SKILL_TRIGGERS)

    def test_falsey_and_unknown_values_stay_off(self) -> None:
        for value in ("0", "false", "off", "no", "", "maybe"):
            with self.subTest(value=value):
                _, analytics = _reload({"TRACK_SKILL_TRIGGERS": value})
                self.assertFalse(analytics.TRACK_SKILL_TRIGGERS)


class RecordAgentExitSkillTest(unittest.TestCase):
    """`record_agent_exit` folds skill triggers into the `agent_exit`
    record only when `TRACK_SKILL_TRIGGERS` is on, never leaks the `Skill`
    args or raw stdout, and keeps emitting the baseline usage/cost record
    even when the skill parse raises (its own fail-open guard)."""

    def _emit(
        self, analytics, path, *, stdout, backend="claude", track=True,
    ) -> list[dict]:
        with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                patch.object(analytics, "TRACK_SKILL_TRIGGERS", track):
            analytics.record_agent_exit(
                repo="owner/repo",
                issue=7,
                stage="implementing",
                agent_role="developer",
                backend=backend,
                agent_spec="claude",
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                duration_s=0.0,
                review_round=0,
                retry_count=1,
            )
        return _read_records(path)

    def test_switch_off_drops_all_skill_fields(self) -> None:
        # Default-off: a skill-bearing stream still records usage but none
        # of the three skill keys appear -- shape-compatible with today.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(skills=("develop",)),
                track=False,
            )
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["event"], "agent_exit")
        self.assertEqual(rec["input_tokens"], 1000)
        for key in (
            "skills_triggered", "skills_triggered_count", "skills_available",
        ):
            self.assertNotIn(key, rec)

    def test_switch_on_records_triggered_fields(self) -> None:
        # develop fires twice and review once: the de-duplicated list keeps
        # first-seen order, the count sums every invocation, and the
        # uncaptured offered set leaves `skills_available` dropped.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(
                    skills=("develop", "develop", "review"),
                ),
                track=True,
            )
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["skills_triggered"], ["develop", "review"])
        self.assertEqual(rec["skills_triggered_count"], 3)
        self.assertNotIn("skills_available", rec)
        self.assertEqual(rec["input_tokens"], 1000)

    def test_switch_on_no_triggers_matches_off_shape(self) -> None:
        # Switch on but the stream triggered nothing: all three skill keys
        # stay dropped, so the record is shape-identical to the off case.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            off = self._emit(
                analytics, Path(td) / "off.jsonl",
                stdout=_claude_stdout_with_skills(skills=("develop",)),
                track=False,
            )
            on_none = self._emit(
                analytics, Path(td) / "on.jsonl",
                stdout=_claude_stdout_with_skills(skills=()),
                track=True,
            )
        for key in (
            "skills_triggered", "skills_triggered_count", "skills_available",
        ):
            self.assertNotIn(key, on_none[0])
        self.assertEqual(set(off[0]), set(on_none[0]))

    def test_skill_args_and_stdout_never_reach_the_record(self) -> None:
        # Privacy: the `Skill` tool's `args` can echo issue/user content; the
        # record carries the skill NAME but never the args payload nor the
        # raw stdout. Mirrors the usage-sink redaction contract.
        _, analytics = _reload()
        marker = "ghp_LEAKED_SKILL_ARG_PAYLOAD_DO_NOT_STORE"
        stdout = _claude_stdout_with_skills(
            skills=("develop",), args_marker=marker,
        )
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl", stdout=stdout, track=True,
            )
        rec = records[0]
        self.assertEqual(rec["skills_triggered"], ["develop"])
        blob = json.dumps(rec)
        self.assertNotIn(marker, blob)
        self.assertNotIn(stdout, blob)
        for forbidden in ("args", "stdout", "prompt"):
            self.assertNotIn(forbidden, rec)

    def test_available_field_recorded_from_real_init_skills(self) -> None:
        # The offered-set wiring exercised end-to-end through the real claude
        # extractor (no stub): a `system`/`init` frame carrying a `skills`
        # array lands as `skills_available`, independent of what triggered.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(
                    skills=("develop",),
                    offered=("develop", "review"),
                ),
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec["skills_triggered"], ["develop"])
        self.assertEqual(rec["skills_triggered_count"], 1)
        self.assertEqual(rec["skills_available"], ["develop", "review"])

    def test_available_recorded_independently_of_triggered(self) -> None:
        # Offered but nothing triggered: `skills_available` is written while
        # `skills_triggered` / `_count` stay dropped -- the asymmetry that
        # tells "offered but unused" from "never available."
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(
                    skills=(), offered=("develop", "review"),
                ),
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec["skills_available"], ["develop", "review"])
        self.assertNotIn("skills_triggered", rec)
        self.assertNotIn("skills_triggered_count", rec)

    def test_skill_parse_failure_still_emits_baseline_record(self) -> None:
        # A skill-parser bug must NOT drop the usage/cost record: the inner
        # fail-open guard logs and falls through with the skill fields unset.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(
                analytics.usage, "parse_agent_skills",
                side_effect=RuntimeError("boom"),
            ), self.assertLogs(analytics.log, level="ERROR"):
                records = self._emit(
                    analytics, Path(td) / "a.jsonl",
                    stdout=_claude_stdout_with_skills(skills=("develop",)),
                    track=True,
                )
        self.assertEqual(len(records), 1)
        rec = records[0]
        # Baseline usage fields survived the skill-parse failure...
        self.assertEqual(rec["event"], "agent_exit")
        self.assertEqual(rec["input_tokens"], 1000)
        self.assertEqual(rec["output_tokens"], 500)
        # ...and the skill fields were left off.
        for key in (
            "skills_triggered", "skills_triggered_count", "skills_available",
        ):
            self.assertNotIn(key, rec)

    def _record(
        self, analytics, *, stdout, track=True, parse=None,
    ):
        """Call `record_agent_exit` with the sink disabled and return its
        value -- the de-duplicated triggered list the caller emits events
        from. `parse` optionally stubs the skill extractor.
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(analytics, "ANALYTICS_LOG_PATH", None)
            )
            stack.enter_context(
                patch.object(analytics, "TRACK_SKILL_TRIGGERS", track)
            )
            if parse is not None:
                stack.enter_context(
                    patch.object(analytics.usage, "parse_agent_skills", parse)
                )
            return analytics.record_agent_exit(
                repo="owner/repo",
                issue=7,
                stage="implementing",
                agent_role="developer",
                backend="claude",
                agent_spec="claude",
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                duration_s=0.0,
                review_round=0,
                retry_count=1,
            )

    def test_returns_triggered_list_when_switch_on(self) -> None:
        # The return value is the de-duplicated first-seen list the audit
        # emitter consumes -- here develop fires twice, review once.
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(
                skills=("develop", "develop", "review"),
            ),
            track=True,
        )
        self.assertEqual(triggered, ["develop", "review"])

    def test_returns_none_when_switch_off(self) -> None:
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(skills=("develop",)),
            track=False,
        )
        self.assertIsNone(triggered)

    def test_returns_none_when_nothing_triggered(self) -> None:
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(skills=()),
            track=True,
        )
        self.assertIsNone(triggered)

    def test_returns_none_on_skill_parse_failure(self) -> None:
        # A skill-parse bug returns None (no events) but still emits baseline.
        _, analytics = _reload()
        with self.assertLogs(analytics.log, level="ERROR"):
            triggered = self._record(
                analytics,
                stdout=_claude_stdout_with_skills(skills=("develop",)),
                track=True,
                parse=MagicMock(side_effect=RuntimeError("boom")),
            )
        self.assertIsNone(triggered)


if __name__ == "__main__":
    unittest.main()
