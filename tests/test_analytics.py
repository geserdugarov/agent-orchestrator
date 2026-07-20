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


DEFAULT_RETENTION_DAYS = 90
_YEAR = 2026
PRUNE_NOW = datetime(_YEAR, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
FRESH_RECORD_AGE_DAYS = 1
RECENT_RECORD_AGE_DAYS = 10
OLD_RECORD_AGE_DAYS = 100
VERY_OLD_RECORD_AGE_DAYS = 200
ANCIENT_RECORD_AGE_DAYS = 1000

AGENT_EXIT_ISSUE_NUMBER = 7
SKILL_STREAM_INPUT_TOKENS = 1_000
SKILL_STREAM_OUTPUT_TOKENS = 500
CLAUDE_TRAJECTORY_INPUT_TOKENS = 100
CLAUDE_TRAJECTORY_OUTPUT_TOKENS = 50
CODEX_TRAJECTORY_INPUT_TOKENS = 200
CODEX_TRAJECTORY_OUTPUT_TOKENS = 80
TRAJECTORY_REVIEW_ROUND = 2
TRAJECTORY_RETRY_COUNT = 1

# Content caps the truncation tests shrink so a bounded fixture still
# crosses them.
_TRUNCATION_EDGE_CHARS = 5
_LONG_TEXT_CHARS = 100
_BUDGET_TOOL_PAIR_COUNT = 5
_MANY_TURNS_COUNT = 5_000
_METADATA_ONLY_STEP_COUNT = 10_000

# Repeated domain values asserted across the suite: repo slugs, agent
# backends, skill names, the stage / event kinds the records carry, and
# the model the fixture streams bill against.
_REPO = "owner/repo"
_REPO_SHORT = "o/r"
_CLAUDE = "claude"
_CODEX = "codex"
_DEVELOP = "develop"
_REVIEW = "review"
_STAGE_IMPLEMENTING = "implementing"
_DEVELOPER = "developer"
_AGENT_EXIT = "agent_exit"
_STAGE_ENTER = "stage_enter"
_AGENT_TRAJECTORY = "agent_trajectory"
_CLAUDE_MODEL = "claude-sonnet-4-6"
_ENCODING = "utf-8"

# Config knob names, used as both the `_reload` env keys and the module
# attributes the tests patch / read back.
_ANALYTICS_LOG_PATH = "ANALYTICS_LOG_PATH"
_ANALYTICS_RETENTION_DAYS = "ANALYTICS_RETENTION_DAYS"
_TRACK_SKILL_TRIGGERS = "TRACK_SKILL_TRIGGERS"
_TRAJECTORY_LOG_PATH = "TRAJECTORY_LOG_PATH"
_TRAJECTORY_RETENTION_DAYS = "TRAJECTORY_RETENTION_DAYS"
_CODEX_HOME = "CODEX_HOME"
_DEFAULT_RETENTION_STR = str(DEFAULT_RETENTION_DAYS)

# Analytics-record field keys asserted repeatedly across the suite.
_INPUT_TOKENS = "input_tokens"
_OUTPUT_TOKENS = "output_tokens"
_STEPS = "steps"
_BACKEND = "backend"
_OUTPUT = "output"
_RUN_USAGE = "run_usage"
_USER_INPUT = "user_input"
_TRUNCATED = "truncated"
_SKILLS_TRIGGERED = "skills_triggered"
_SKILLS_TRIGGERED_COUNT = "skills_triggered_count"
_SKILLS_AVAILABLE = "skills_available"
_SKILLS_EVIDENCE = "skills_evidence"
_SKILLS_INCIDENTAL = "skills_incidental"
_SKILLS_INCIDENTAL_COUNT = "skills_incidental_count"
# The skill fields `record_agent_exit` folds in behind the switch; several
# tests assert they all appear or are dropped together (the backward-compatible
# "absent opt-in -> today's record shape" guarantee).
_SKILL_FIELD_KEYS = (
    _SKILLS_TRIGGERED,
    _SKILLS_TRIGGERED_COUNT,
    _SKILLS_AVAILABLE,
    _SKILLS_EVIDENCE,
    _SKILLS_INCIDENTAL,
    _SKILLS_INCIDENTAL_COUNT,
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding=_ENCODING)


def _read_lines(path: Path) -> list[str]:
    return _read_text(path).splitlines()


def _write_json_lines(path: Path, records: list[dict]) -> None:
    """Write one sorted-key JSON object per line, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=_ENCODING) as stream:
        for record in records:
            stream.write(f"{json.dumps(record, sort_keys=True)}\n")


def _ts_days_ago(days: int, *, now: datetime = PRUNE_NOW) -> str:
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


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


@contextlib.contextmanager
def _analytics_sink(retention: str | None = None):
    """Reload the analytics package against a temporary `analytics.jsonl`
    sink, yielding `(path, analytics)`.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "analytics.jsonl"
        env = {_ANALYTICS_LOG_PATH: str(path)}
        if retention is not None:
            env[_ANALYTICS_RETENTION_DAYS] = retention
        _, analytics = _reload(env)
        yield path, analytics


@contextlib.contextmanager
def _trajectory_sink(retention: str | None = None):
    """Reload the analytics package against a temporary `trajectory.jsonl`
    sink, yielding `(path, analytics)`.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trajectory.jsonl"
        env = {_TRAJECTORY_LOG_PATH: str(path)}
        if retention is not None:
            env[_TRAJECTORY_RETENTION_DAYS] = retention
        _, analytics = _reload(env)
        yield path, analytics


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in _read_lines(path)
        if line.strip()
    ]


def _claude_stdout_with_skills(
    *,
    skills: tuple[str, ...],
    offered: tuple[str, ...] = (),
    args_marker: str = "skill-args-must-never-be-stored",
    input_tokens: int = SKILL_STREAM_INPUT_TOKENS,
    output_tokens: int = SKILL_STREAM_OUTPUT_TOKENS,
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
    tool_blocks = [
        {
            "type": "tool_use",
            "name": "Skill",
            "id": f"toolu_{index}",
            "input": {"skill": name, "args": args_marker},
        }
        for index, name in enumerate(skills)
    ]
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-skill",
            "model": _CLAUDE_MODEL,
            "content": tool_blocks,
            "usage": {
                _INPUT_TOKENS: input_tokens,
                _OUTPUT_TOKENS: output_tokens,
            },
        },
    }
    result_frame = {"type": "result", "num_turns": 1}
    frames = [assistant, result_frame]
    if offered:
        frames.insert(
            0, {"type": "system", "subtype": "init", "skills": list(offered)}
        )
    return "\n".join(json.dumps(frame) for frame in frames)


def _codex_command(item_id: str, command: str) -> dict:
    return {"type": "item.completed", "item": {
        "id": item_id, "type": "command_execution", "command": command,
    }}


def _codex_stdout_with_skills(
    *,
    read: str | None = None,
    incidental: str | None = None,
    input_tokens: int = SKILL_STREAM_INPUT_TOKENS,
    output_tokens: int = SKILL_STREAM_OUTPUT_TOKENS,
) -> str:
    """A codex exec --json stdout that reports usage and, optionally, a direct
    SKILL.md read (an inferred load) and/or a `git diff` inspection of one (an
    incidental reference).

    `read` / `incidental` are skill names: each becomes one `command_execution`
    item -- a `cat .../SKILL.md` for the load, a `git diff -- .../SKILL.md` for
    the reference -- so the recorder exercises the real `parse_codex_skills`
    classifier end-to-end (no stub).
    """
    frames: list[dict] = []
    if read is not None:
        frames.append(_codex_command(
            "read1", f"/bin/bash -lc 'cat skills/{read}/SKILL.md'"))
    if incidental is not None:
        frames.append(_codex_command(
            "diff1",
            f"/bin/bash -lc 'git diff -- .agents/skills/{incidental}/SKILL.md'"))
    frames.append({"type": "turn.completed", "usage": {
        _INPUT_TOKENS: input_tokens, _OUTPUT_TOKENS: output_tokens,
    }})
    return "\n".join(json.dumps(frame) for frame in frames)


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
        self.assertEqual(
            analytics.ANALYTICS_RETENTION_DAYS, DEFAULT_RETENTION_DAYS,
        )

    def test_explicit_path_overrides_default(self) -> None:
        _, analytics = _reload({_ANALYTICS_LOG_PATH: "/var/log/orch/a.jsonl"})
        self.assertEqual(
            analytics.ANALYTICS_LOG_PATH, Path("/var/log/orch/a.jsonl")
        )

    def test_empty_value_disables(self) -> None:
        # Explicit empty assignment in .env is the documented disable knob.
        _, analytics = _reload({_ANALYTICS_LOG_PATH: ""})
        self.assertIsNone(analytics.ANALYTICS_LOG_PATH)

    def test_sentinel_values_disable(self) -> None:
        for spelling in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_ANALYTICS_LOG_PATH: spelling})
                self.assertIsNone(analytics.ANALYTICS_LOG_PATH)

    def test_zero_retention_means_keep_forever(self) -> None:
        _, analytics = _reload({_ANALYTICS_RETENTION_DAYS: "0"})
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, 0)

    def test_retention_env_override(self) -> None:
        _, analytics = _reload({_ANALYTICS_RETENTION_DAYS: "30"})
        self.assertEqual(analytics.ANALYTICS_RETENTION_DAYS, 30)


class AnalyticsDisabledModeTest(unittest.TestCase):
    """With the sink disabled, both `append_record` and
    `prune_old_records` are silent no-ops -- no file is ever opened,
    pinned GitHub state is untouched, and the helpers do not raise.
    """

    def test_append_creates_no_file_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            _, analytics = _reload({_ANALYTICS_LOG_PATH: ""})
            analytics.append_record(
                analytics.build_record(repo=_REPO_SHORT, issue=1, event="x")
            )
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
            _, analytics = _reload({
                "LOG_DIR": str(log_dir),
                _ANALYTICS_LOG_PATH: "off",
            })
            analytics.append_record(
                analytics.build_record(repo=_REPO_SHORT, issue=1, event="x")
            )
            self.assertFalse(log_dir.exists())


class AnalyticsAppendTest(unittest.TestCase):
    """`build_record` produces the documented base fields and
    `append_record` writes one well-formed JSONL line per call.
    """

    def test_record_has_required_base_fields(self) -> None:
        _, analytics = _reload()
        rec = analytics.build_record(
            repo=_REPO_SHORT, issue=42, event=_STAGE_ENTER,
            stage=_STAGE_IMPLEMENTING,
        )
        self.assertIn("ts", rec)
        self.assertEqual(rec["repo"], _REPO_SHORT)
        self.assertEqual(rec["issue"], 42)
        self.assertEqual(rec["event"], _STAGE_ENTER)
        self.assertEqual(rec["stage"], _STAGE_IMPLEMENTING)
        parsed = datetime.fromisoformat(rec["ts"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_stage_omitted_when_none(self) -> None:
        _, analytics = _reload()
        rec = analytics.build_record(
            repo=_REPO_SHORT, issue=1, event="pr_opened",
        )
        self.assertNotIn("stage", rec)

    def test_none_valued_extras_are_dropped(self) -> None:
        _, analytics = _reload()
        rec = analytics.build_record(
            repo=_REPO_SHORT, issue=1, event="agent_spawn",
            session_id=None, retry_count=2,
        )
        self.assertNotIn("session_id", rec)
        self.assertEqual(rec["retry_count"], 2)

    def test_append_writes_one_line_per_record(self) -> None:
        with _analytics_sink() as (path, analytics):
            analytics.append_record(
                analytics.build_record(
                    repo=_REPO_SHORT, issue=1, event=_STAGE_ENTER,
                    stage=_STAGE_IMPLEMENTING,
                )
            )
            analytics.append_record(
                analytics.build_record(
                    repo=_REPO_SHORT, issue=2, event="pr_opened", pr_number=5,
                )
            )
            self.assertTrue(path.exists())
            lines = _read_lines(path)
            self.assertEqual(len(lines), 2)
            rec0 = json.loads(lines[0])
            self.assertEqual(rec0["issue"], 1)
            self.assertEqual(rec0["event"], _STAGE_ENTER)
            self.assertEqual(rec0["stage"], _STAGE_IMPLEMENTING)
            rec1 = json.loads(lines[1])
            self.assertEqual(rec1["pr_number"], 5)
            self.assertNotIn("stage", rec1)

    def test_creates_missing_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a" / "b" / "c" / "analytics.jsonl"
            _, analytics = _reload({_ANALYTICS_LOG_PATH: str(path)})
            analytics.append_record(
                analytics.build_record(repo=_REPO_SHORT, issue=1, event="x")
            )
            self.assertTrue(path.exists())

    def test_append_is_append_only(self) -> None:
        # Repeated appends must accumulate, never overwrite prior records.
        with _analytics_sink() as (path, analytics):
            for issue_num in range(5):
                analytics.append_record(
                    analytics.build_record(
                        repo=_REPO_SHORT, issue=issue_num, event="x",
                    )
                )
            lines = _read_lines(path)
            self.assertEqual(len(lines), 5)
            issues = [json.loads(line)["issue"] for line in lines]
            self.assertEqual(issues, list(range(5)))


class AnalyticsPruneTest(unittest.TestCase):
    """`prune_old_records` removes records whose `ts` precedes
    `ANALYTICS_RETENTION_DAYS`, keeps newer records, no-ops when
    retention is 0 (keep forever) or the file is absent, and preserves
    malformed lines so cleanup is operator-driven.
    """

    def test_removes_old_records_keeps_recent(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        new_ts = _ts_days_ago(RECENT_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [
                {"ts": old_ts, "repo": _REPO_SHORT, "issue": 1, "event": "x"},
                {"ts": new_ts, "repo": _REPO_SHORT, "issue": 2, "event": "y"},
                {"ts": old_ts, "repo": _REPO_SHORT, "issue": 3, "event": "z"},
            ])
            removed = analytics.prune_old_records(now=now)
            self.assertEqual(removed, 2)
            remaining = [
                json.loads(line)
                for line in _read_lines(path)
            ]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["issue"], 2)

    def test_zero_retention_is_no_op(self) -> None:
        now = PRUNE_NOW
        ancient = _ts_days_ago(ANCIENT_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention="0") as (path, analytics):
            _write_json_lines(path, [
                {"ts": ancient, "repo": _REPO_SHORT, "issue": 1, "event": "x"},
            ])
            self.assertEqual(analytics.prune_old_records(now=now), 0)
            # File contents unchanged.
            lines = _read_lines(path)
            self.assertEqual(len(lines), 1)

    def test_negative_retention_is_no_op(self) -> None:
        # Treated identically to the documented `0 = keep forever` knob.
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention="-5") as (path, analytics):
            _write_json_lines(path, [
                {"ts": old_ts, "repo": _REPO_SHORT, "issue": 1, "event": "x"},
            ])
            self.assertEqual(analytics.prune_old_records(now=now), 0)

    def test_missing_file_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent.jsonl"
            _, analytics = _reload({_ANALYTICS_LOG_PATH: str(path)})
            self.assertEqual(analytics.prune_old_records(), 0)
            self.assertFalse(path.exists())

    def test_no_records_old_enough_does_not_rewrite(self) -> None:
        now = PRUNE_NOW
        new_ts = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [
                {"ts": new_ts, "repo": _REPO_SHORT, "issue": 1, "event": "x"},
            ])
            mtime_before = path.stat().st_mtime_ns
            self.assertEqual(analytics.prune_old_records(now=now), 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_malformed_lines_preserved(self) -> None:
        # Non-JSON lines, JSON without `ts`, and unparseable `ts` strings
        # survive the prune so operators can clean up rather than having
        # the helper silently drop data it cannot interpret.
        now = PRUNE_NOW
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding=_ENCODING) as fh:
                fh.write("this is not json\n")
                fh.write(json.dumps({
                    "ts": old_ts, "repo": _REPO_SHORT,
                    "issue": 1, "event": "x",
                }) + "\n")
                fh.write('{"ts": "not-a-date", "event": "y"}\n')
                fh.write('{"event": "no-ts-field"}\n')
            removed = analytics.prune_old_records(now=now)
            # Only the parseable old record is removed; the three other
            # malformed-or-missing-ts lines survive.
            self.assertEqual(removed, 1)
            kept = _read_lines(path)
            self.assertEqual(len(kept), 3)
            self.assertIn("this is not json", kept[0])

    def test_rewrite_failure_leaves_original_intact(self) -> None:
        # An OSError from the atomic rewrite (e.g. a full disk hitting
        # `os.replace`) is downgraded to a logged no-op: the prune returns
        # 0 and the original file is left untouched rather than truncated,
        # so analytics stays observability-only. The partial temp file is
        # cleaned up so no `.prune.*.tmp` orphan is left behind.
        now = PRUNE_NOW
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [
                {"ts": old_ts, "repo": _REPO_SHORT, "issue": 1, "event": "x"},
            ])
            before = _read_text(path)
            with patch.object(
                analytics.os, "replace",
                side_effect=OSError("no space left on device"),
            ):
                removed = analytics.prune_old_records(now=now)
            self.assertEqual(removed, 0)
            self.assertEqual(_read_text(path), before)
            leftovers = [
                entry.name
                for entry in path.parent.iterdir()
                if ".prune." in entry.name
            ]
            self.assertEqual(leftovers, [])

    def test_naive_timestamp_treated_as_utc(self) -> None:
        # Pre-existing records written without tz info (or by an older
        # writer) must still be comparable; treat them as UTC rather than
        # raising and aborting the prune.
        now = PRUNE_NOW
        old_naive = (now - timedelta(days=OLD_RECORD_AGE_DAYS)).replace(
            tzinfo=None
        ).isoformat(timespec="seconds")
        with _analytics_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [{
                "ts": old_naive, "repo": _REPO_SHORT,
                "issue": 1, "event": "x",
            }])
            self.assertEqual(analytics.prune_old_records(now=now), 1)
            self.assertEqual(_read_text(path), "")


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
        import threading
        with tempfile.TemporaryDirectory(prefix="analytics-race-") as td:
            path = Path(td) / "analytics.jsonl"
            now = PRUNE_NOW
            old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
            new_ts = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=now)
            # One old record (will be pruned) plus one recent record
            # (the prune rewrite must keep it). After the rewrite, an
            # appender adds a fresh record concurrently; the prune
            # must NOT drop it.
            path.write_text(
                json.dumps({
                    "ts": old_ts, "repo": _REPO_SHORT, "issue": 1,
                    "event": _STAGE_ENTER,
                }) + "\n"
                + json.dumps({
                    "ts": new_ts, "repo": _REPO_SHORT, "issue": 2,
                    "event": _STAGE_ENTER,
                }) + "\n",
                encoding=_ENCODING,
            )
            _, analytics = _reload({
                _ANALYTICS_LOG_PATH: str(path),
                _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
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
                    "ts": new_ts, "repo": _REPO_SHORT, "issue": 99,
                    "event": _STAGE_ENTER,
                })
                appender_done.set()

            appender_thread = threading.Thread(target=appender)
            appender_thread.start()
            try:
                with patch.object(analytics.os, "replace", gated_replace):
                    removed = analytics.prune_old_records(now=now)
            finally:
                # Make sure the appender is unblocked even if the
                # prune raised; the wait above is bounded.
                after_read.set()
                appender_thread.join(timeout=5.0)

            self.assertEqual(removed, 1)
            remaining = [
                json.loads(line)
                for line in _read_lines(path)
            ]
            issues = sorted(record["issue"] for record in remaining)
            # The old record (issue=1) is gone. Both the kept record
            # (issue=2) and the concurrent append (issue=99) survive.
            self.assertEqual(issues, [2, 99])

    def test_prune_rewrites_without_github_writes(self) -> None:
        # "Analytics is not authoritative workflow state" enforced at
        # the boundary: the prune helper takes no GitHub client and the
        # real `prune_old_records` implementation never imports `github`
        # at all. This pairs with the main-loop wiring tests in
        # `tests/test_main.py`: those verify the wrapper is called once
        # per tick; this verifies that calling it cannot mutate pinned
        # state through any client method.
        from orchestrator.github import GitHubClient

        now = PRUNE_NOW
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
        new_ts = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=now)
        with tempfile.TemporaryDirectory(prefix="analytics-retention-") as td:
            path = Path(td) / "analytics.jsonl"
            path.write_text(
                json.dumps({
                    "ts": old_ts, "repo": _REPO_SHORT, "issue": 1,
                    "event": _STAGE_ENTER, "stage": _STAGE_IMPLEMENTING,
                }) + "\n"
                + json.dumps({
                    "ts": new_ts, "repo": _REPO_SHORT, "issue": 2,
                    "event": "stage_evaluation", "stage": "validating",
                    "duration_s": 0.001, "result": "ok",
                }) + "\n",
                encoding=_ENCODING,
            )
            _, analytics = _reload({
                _ANALYTICS_LOG_PATH: str(path),
                _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
            })
            mutators = (
                "write_pinned_state", "comment", "set_workflow_label",
                "create_child_issue", "open_pr", "pr_comment",
                "merge_pr", "delete_remote_branch", "emit_event",
            )
            # Patch every GitHub-mutating method on the class so the
            # prune cannot side-effect through any client instance that
            # some future refactor accidentally routes it through.
            with contextlib.ExitStack() as guards:
                for name in mutators:
                    guards.enter_context(patch.object(
                        GitHubClient,
                        name,
                        MagicMock(
                            side_effect=AssertionError(
                                f"prune must not call GitHubClient.{name}"
                            ),
                        ),
                    ))
                removed = analytics.prune_old_records(now=now)
            self.assertEqual(removed, 1)
            remaining = [
                json.loads(line)
                for line in _read_lines(path)
            ]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["issue"], 2)


class SkillTriggerConfigTest(unittest.TestCase):
    """`TRACK_SKILL_TRIGGERS` parses at import inside the analytics package,
    defaults off, is exported in `__all__`, and honors the same truthy
    spellings as the other boolean knobs in `orchestrator.config`."""

    def test_defaults_off_and_is_exported(self) -> None:
        # Default-off is a deliberate, revisited decision (#515): even after
        # codex skill-trigger coverage landed (#513), the new file-open path's
        # production noise stays unmeasured, so the default holds off until it
        # proves low-noise live. Flipping this assertion is the flip.
        _, analytics = _reload()
        self.assertFalse(analytics.TRACK_SKILL_TRIGGERS)
        self.assertIn(_TRACK_SKILL_TRIGGERS, analytics.__all__)

    def test_truthy_spellings_enable(self) -> None:
        for spelling in ("1", "true", "on", "yes", "On", " YES "):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_TRACK_SKILL_TRIGGERS: spelling})
                self.assertTrue(analytics.TRACK_SKILL_TRIGGERS)

    def test_falsey_and_unknown_values_stay_off(self) -> None:
        for spelling in ("0", "false", "off", "no", "", "maybe"):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_TRACK_SKILL_TRIGGERS: spelling})
                self.assertFalse(analytics.TRACK_SKILL_TRIGGERS)


class RecordAgentExitSkillTest(unittest.TestCase):
    """`record_agent_exit` folds skill triggers into the `agent_exit`
    record only when `TRACK_SKILL_TRIGGERS` is on, never leaks the `Skill`
    args or raw stdout, and keeps emitting the baseline usage/cost record
    even when the skill parse raises (its own fail-open guard)."""

    def test_switch_off_drops_all_skill_fields(self) -> None:
        # Default-off: a skill-bearing stream still records usage but none
        # of the three skill keys appear -- shape-compatible with today.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                track=False,
            )
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["event"], _AGENT_EXIT)
        self.assertEqual(rec[_INPUT_TOKENS], SKILL_STREAM_INPUT_TOKENS)
        for key in _SKILL_FIELD_KEYS:
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
                    skills=(_DEVELOP, _DEVELOP, _REVIEW),
                ),
                track=True,
            )
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_DEVELOP, _REVIEW])
        self.assertEqual(rec[_SKILLS_TRIGGERED_COUNT], 3)
        # A claude `Skill` call is a confirmed load, so every triggered name
        # carries `confirmed` evidence and there are no incidental references.
        self.assertEqual(
            rec[_SKILLS_EVIDENCE], {_DEVELOP: "confirmed", _REVIEW: "confirmed"},
        )
        self.assertNotIn(_SKILLS_INCIDENTAL, rec)
        self.assertNotIn(_SKILLS_INCIDENTAL_COUNT, rec)
        self.assertNotIn(_SKILLS_AVAILABLE, rec)
        self.assertEqual(rec[_INPUT_TOKENS], SKILL_STREAM_INPUT_TOKENS)

    def test_switch_on_no_triggers_matches_off_shape(self) -> None:
        # Switch on but the stream triggered nothing: all three skill keys
        # stay dropped, so the record is shape-identical to the off case.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            off = self._emit(
                analytics, Path(td) / "off.jsonl",
                stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                track=False,
            )
            on_none = self._emit(
                analytics, Path(td) / "on.jsonl",
                stdout=_claude_stdout_with_skills(skills=()),
                track=True,
            )
        for key in _SKILL_FIELD_KEYS:
            self.assertNotIn(key, on_none[0])
        self.assertEqual(set(off[0]), set(on_none[0]))

    def test_args_and_stdout_absent_from_record(self) -> None:
        # Privacy: the `Skill` tool's `args` can echo issue/user content; the
        # record carries the skill NAME but never the args payload nor the
        # raw stdout. Mirrors the usage-sink redaction contract.
        _, analytics = _reload()
        marker = "ghp_LEAKED_SKILL_ARG_PAYLOAD_DO_NOT_STORE"
        stdout = _claude_stdout_with_skills(
            skills=(_DEVELOP,), args_marker=marker,
        )
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl", stdout=stdout, track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_DEVELOP])
        blob = json.dumps(rec)
        self.assertNotIn(marker, blob)
        self.assertNotIn(stdout, blob)
        for forbidden in ("args", "stdout", "prompt"):
            self.assertNotIn(forbidden, rec)

    def test_real_init_records_available_field(self) -> None:
        # The offered-set wiring exercised end-to-end through the real claude
        # extractor (no stub): a `system`/`init` frame carrying a `skills`
        # array lands as `skills_available`, independent of what triggered.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(
                    skills=(_DEVELOP,),
                    offered=(_DEVELOP, _REVIEW),
                ),
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_DEVELOP])
        self.assertEqual(rec[_SKILLS_TRIGGERED_COUNT], 1)
        self.assertEqual(rec[_SKILLS_AVAILABLE], [_DEVELOP, _REVIEW])

    def test_available_independent_of_triggered(self) -> None:
        # Offered but nothing triggered: `skills_available` is written while
        # `skills_triggered` / `_count` stay dropped -- the asymmetry that
        # tells "offered but unused" from "never available."
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_claude_stdout_with_skills(
                    skills=(), offered=(_DEVELOP, _REVIEW),
                ),
                track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_AVAILABLE], [_DEVELOP, _REVIEW])
        self.assertNotIn(_SKILLS_TRIGGERED, rec)
        self.assertNotIn(_SKILLS_TRIGGERED_COUNT, rec)

    def test_parse_failure_keeps_baseline_record(self) -> None:
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
                    stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                    track=True,
                )
        self.assertEqual(len(records), 1)
        rec = records[0]
        # Baseline usage fields survived the skill-parse failure...
        self.assertEqual(rec["event"], _AGENT_EXIT)
        self.assertEqual(rec[_INPUT_TOKENS], SKILL_STREAM_INPUT_TOKENS)
        self.assertEqual(rec[_OUTPUT_TOKENS], SKILL_STREAM_OUTPUT_TOKENS)
        # ...and the skill fields were left off.
        for key in _SKILL_FIELD_KEYS:
            self.assertNotIn(key, rec)

    def test_returns_triggered_list_when_switch_on(self) -> None:
        # The return value is the de-duplicated first-seen list the audit
        # emitter consumes -- here develop fires twice, review once.
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(
                skills=(_DEVELOP, _DEVELOP, _REVIEW),
            ),
            track=True,
        )
        self.assertEqual(triggered, [_DEVELOP, _REVIEW])

    def test_returns_none_when_switch_off(self) -> None:
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
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
                stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                track=True,
                parse=MagicMock(side_effect=RuntimeError("boom")),
            )
        self.assertIsNone(triggered)

    def test_codex_records_inferred_evidence_and_incidental(self) -> None:
        # A codex run that directly reads review/SKILL.md (an inferred load)
        # and runs `git diff` over a changed develop/SKILL.md (an incidental
        # reference) records the load in `skills_triggered` with `inferred`
        # evidence and the reference in the separate incidental bucket -- never
        # in the triggered set or its count.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_codex_stdout_with_skills(
                    read=_REVIEW, incidental=_DEVELOP,
                ),
                backend=_CODEX, track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_REVIEW])
        self.assertEqual(rec[_SKILLS_TRIGGERED_COUNT], 1)
        self.assertEqual(rec[_SKILLS_EVIDENCE], {_REVIEW: "inferred"})
        self.assertEqual(rec[_SKILLS_INCIDENTAL], [_DEVELOP])
        self.assertEqual(rec[_SKILLS_INCIDENTAL_COUNT], 1)

    def test_codex_skill_loaded_and_inspected_records_both(self) -> None:
        # A skill a codex run both reads and inspects persists in BOTH the
        # triggered / evidence fields and the incidental fields: the buckets
        # are independent, so a loaded skill keeps its incidental count while
        # the trigger set still excludes the inspection.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_codex_stdout_with_skills(
                    read=_REVIEW, incidental=_REVIEW,
                ),
                backend=_CODEX, track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_TRIGGERED], [_REVIEW])
        self.assertEqual(rec[_SKILLS_TRIGGERED_COUNT], 1)
        self.assertEqual(rec[_SKILLS_EVIDENCE], {_REVIEW: "inferred"})
        self.assertEqual(rec[_SKILLS_INCIDENTAL], [_REVIEW])
        self.assertEqual(rec[_SKILLS_INCIDENTAL_COUNT], 1)

    def test_incidental_only_run_keeps_triggered_keys_absent(self) -> None:
        # A run whose only SKILL.md reference is a `git diff` inspection records
        # the incidental bucket but leaves every triggered / evidence key
        # dropped, so the record cannot masquerade as a load.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            records = self._emit(
                analytics, Path(td) / "a.jsonl",
                stdout=_codex_stdout_with_skills(incidental=_DEVELOP),
                backend=_CODEX, track=True,
            )
        rec = records[0]
        self.assertEqual(rec[_SKILLS_INCIDENTAL], [_DEVELOP])
        self.assertEqual(rec[_SKILLS_INCIDENTAL_COUNT], 1)
        for key in (_SKILLS_TRIGGERED, _SKILLS_TRIGGERED_COUNT, _SKILLS_EVIDENCE):
            self.assertNotIn(key, rec)

    def test_returns_only_loaded_skills_not_incidental(self) -> None:
        # The value `record_agent_exit` returns -- the list the `skill_triggered`
        # audit emitter iterates -- carries only loaded skills, so an incidental
        # `git diff` reference never produces an audit event.
        _, analytics = _reload()
        triggered = self._record(
            analytics,
            stdout=_codex_stdout_with_skills(read=_REVIEW, incidental=_DEVELOP),
            track=True,
            backend=_CODEX,
        )
        self.assertEqual(triggered, [_REVIEW])

    def _emit(
        self, analytics, path, *, stdout, backend=_CLAUDE, track=True,
    ) -> list[dict]:
        with patch.object(analytics, _ANALYTICS_LOG_PATH, path), \
                patch.object(analytics, _TRACK_SKILL_TRIGGERS, track):
            analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage=_STAGE_IMPLEMENTING,
                agent_role=_DEVELOPER,
                backend=backend,
                agent_spec=_CLAUDE,
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

    def _record(
        self, analytics, *, stdout, track=True, parse=None, backend=_CLAUDE,
    ):
        """Call `record_agent_exit` with the sink disabled and return its
        value -- the de-duplicated triggered list the caller emits events
        from. `parse` optionally stubs the skill extractor.
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(analytics, _ANALYTICS_LOG_PATH, None)
            )
            stack.enter_context(
                patch.object(analytics, _TRACK_SKILL_TRIGGERS, track)
            )
            if parse is not None:
                stack.enter_context(
                    patch.object(analytics.usage, "parse_agent_skills", parse)
                )
            return analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage=_STAGE_IMPLEMENTING,
                agent_role=_DEVELOPER,
                backend=backend,
                agent_spec=backend,
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


class TrajectoryConfigTest(unittest.TestCase):
    """`TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS` parse at import
    inside the analytics package. Unlike `ANALYTICS_LOG_PATH`, the
    trajectory sink is opt-in: an *unset* path disables it. Retention
    mirrors the analytics knob (default 90, non-positive keeps forever).
    """

    def test_unset_disables(self) -> None:
        # The opt-in distinction from analytics: no env var => off.
        _, analytics = _reload()
        self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_empty_value_disables(self) -> None:
        _, analytics = _reload({_TRAJECTORY_LOG_PATH: ""})
        self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_sentinel_values_disable(self) -> None:
        for spelling in ("off", "OFF", " off ", "disabled", "none", "None"):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_TRAJECTORY_LOG_PATH: spelling})
                self.assertIsNone(analytics.TRAJECTORY_LOG_PATH)

    def test_explicit_path_enables(self) -> None:
        _, analytics = _reload({_TRAJECTORY_LOG_PATH: "/var/log/orch/t.jsonl"})
        self.assertEqual(
            analytics.TRAJECTORY_LOG_PATH, Path("/var/log/orch/t.jsonl")
        )

    def test_default_retention_is_ninety_days(self) -> None:
        _, analytics = _reload()
        self.assertEqual(
            analytics.TRAJECTORY_RETENTION_DAYS, DEFAULT_RETENTION_DAYS,
        )

    def test_zero_retention_means_keep_forever(self) -> None:
        _, analytics = _reload({_TRAJECTORY_RETENTION_DAYS: "0"})
        self.assertEqual(analytics.TRAJECTORY_RETENTION_DAYS, 0)

    def test_retention_env_override(self) -> None:
        _, analytics = _reload({_TRAJECTORY_RETENTION_DAYS: "7"})
        self.assertEqual(analytics.TRAJECTORY_RETENTION_DAYS, 7)

    def test_knobs_exported(self) -> None:
        _, analytics = _reload()
        self.assertIn(_TRAJECTORY_LOG_PATH, analytics.__all__)
        self.assertIn(_TRAJECTORY_RETENTION_DAYS, analytics.__all__)
        self.assertIn("append_trajectory_record", analytics.__all__)
        self.assertIn("prune_trajectory_records", analytics.__all__)


class TrajectoryDisabledModeTest(unittest.TestCase):
    """With the trajectory sink disabled (the opt-in default), both
    `append_trajectory_record` and `prune_trajectory_records` are silent
    no-ops -- no file is ever opened and the helpers do not raise.
    """

    def test_append_creates_no_file_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, analytics = _reload()  # TRAJECTORY_LOG_PATH unset => off
            analytics.append_trajectory_record({"ts": "x", "event": "y"})
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_append_creates_no_file_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            _, analytics = _reload({_TRAJECTORY_LOG_PATH: "off"})
            analytics.append_trajectory_record({"ts": "x", "event": "y"})
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_prune_returns_zero_when_disabled(self) -> None:
        _, analytics = _reload({_TRAJECTORY_LOG_PATH: "disabled"})
        self.assertEqual(analytics.prune_trajectory_records(), 0)

    def test_prune_returns_zero_when_unset(self) -> None:
        _, analytics = _reload()
        self.assertEqual(analytics.prune_trajectory_records(), 0)


class TrajectoryAppendTest(unittest.TestCase):
    """`append_trajectory_record` reopens append per record, creates
    parent directories, never overwrites, and downgrades OSError to a
    warning rather than propagating it.
    """

    def test_append_writes_one_line_per_record(self) -> None:
        with _trajectory_sink() as (path, analytics):
            analytics.append_trajectory_record({"session_id": "a", "n": 1})
            analytics.append_trajectory_record({"session_id": "b", "n": 2})
            lines = _read_lines(path)
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["session_id"], "a")
            self.assertEqual(json.loads(lines[1])["n"], 2)

    def test_creates_missing_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a" / "b" / "c" / "trajectory.jsonl"
            _, analytics = _reload({_TRAJECTORY_LOG_PATH: str(path)})
            analytics.append_trajectory_record({"event": "x"})
            self.assertTrue(path.exists())

    def test_append_is_append_only(self) -> None:
        with _trajectory_sink() as (path, analytics):
            for index in range(5):
                analytics.append_trajectory_record({"n": index})
            counters = [json.loads(line)["n"] for line in _read_lines(path)]
            self.assertEqual(counters, list(range(5)))

    def test_oserror_is_downgraded_to_warning(self) -> None:
        # A path whose parent is a regular file makes `mkdir(parents=True)`
        # raise NotADirectoryError (an OSError). The append must log a
        # warning and swallow it -- analytics/trajectory is observability,
        # never authoritative state, so a misconfigured path cannot raise.
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text(
                "i am a file, not a directory", encoding=_ENCODING,
            )
            path = blocker / "sub" / "trajectory.jsonl"
            _, analytics = _reload({_TRAJECTORY_LOG_PATH: str(path)})
            with self.assertLogs(analytics.log, level="WARNING") as cm:
                analytics.append_trajectory_record({"event": "x"})
            self.assertFalse(path.exists())
            self.assertTrue(
                any("could not write" in message for message in cm.output)
            )


class TrajectoryPruneTest(unittest.TestCase):
    """`prune_trajectory_records` mirrors `prune_old_records`: removes
    records past `TRAJECTORY_RETENTION_DAYS`, no-ops at retention <= 0 or
    on an absent file, and preserves malformed / unparseable lines.
    """

    def test_removes_old_records_keeps_recent(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        new_ts = _ts_days_ago(RECENT_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [
                {"ts": old_ts, "session_id": "1"},
                {"ts": new_ts, "session_id": "2"},
                {"ts": old_ts, "session_id": "3"},
            ])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 2)
            self.assertEqual(
                [
                    json.loads(line)["session_id"]
                    for line in _read_lines(path)
                ],
                ["2"],
            )

    def test_zero_retention_is_no_op(self) -> None:
        now = PRUNE_NOW
        ancient = _ts_days_ago(ANCIENT_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention="0") as (path, analytics):
            _write_json_lines(path, [{"ts": ancient, "session_id": "1"}])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 0)
            self.assertEqual(
                len(_read_lines(path)), 1
            )

    def test_negative_retention_is_no_op(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(OLD_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention="-5") as (path, analytics):
            _write_json_lines(path, [{"ts": old_ts, "session_id": "1"}])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 0)

    def test_missing_file_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "absent.jsonl"
            _, analytics = _reload({_TRAJECTORY_LOG_PATH: str(path)})
            self.assertEqual(analytics.prune_trajectory_records(), 0)
            self.assertFalse(path.exists())

    def test_no_records_old_enough_does_not_rewrite(self) -> None:
        now = PRUNE_NOW
        new_ts = _ts_days_ago(FRESH_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [{"ts": new_ts, "session_id": "1"}])
            mtime_before = path.stat().st_mtime_ns
            self.assertEqual(analytics.prune_trajectory_records(now=now), 0)
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_malformed_lines_preserved(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding=_ENCODING) as fh:
                fh.write("this is not json\n")
                fh.write(f"{json.dumps({'ts': old_ts, 'session_id': '1'})}\n")
                fh.write('{"ts": "not-a-date", "session_id": "2"}\n')
                fh.write('{"session_id": "no-ts-field"}\n')
            self.assertEqual(analytics.prune_trajectory_records(now=now), 1)
            kept = _read_lines(path)
            self.assertEqual(len(kept), 3)
            self.assertIn("this is not json", kept[0])

    def test_naive_timestamp_treated_as_utc(self) -> None:
        now = PRUNE_NOW
        old_naive = (now - timedelta(days=OLD_RECORD_AGE_DAYS)).replace(
            tzinfo=None
        ).isoformat(timespec="seconds")
        with _trajectory_sink(retention=_DEFAULT_RETENTION_STR) as (
            path, analytics,
        ):
            _write_json_lines(path, [{"ts": old_naive, "session_id": "1"}])
            self.assertEqual(analytics.prune_trajectory_records(now=now), 1)
            self.assertEqual(_read_text(path), "")

    def test_probe_oserror_becomes_warning(self) -> None:
        # `Path.exists()` re-raises OSErrors that don't mean "absent"
        # (e.g. ENAMETOOLONG on an over-long path). That probe runs
        # before the read/rewrite try-block, so without its own guard
        # the error would escape the per-tick caller. The prune must
        # warn and no-op (return 0) instead of raising.
        with tempfile.TemporaryDirectory() as td:
            # A single path component well past NAME_MAX (255) makes the
            # underlying stat() raise OSError [Errno 36] File name too long.
            path = Path(td) / ("x" * 5000)
            _, analytics = _reload({
                _TRAJECTORY_LOG_PATH: str(path),
                _TRAJECTORY_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
            })
            with self.assertLogs(analytics.log, level="WARNING") as cm:
                removed = analytics.prune_trajectory_records()
            self.assertEqual(removed, 0)
            self.assertTrue(
                any("prune" in message for message in cm.output)
            )


class TrajectorySinkIndependenceTest(unittest.TestCase):
    """The trajectory sink is a fully independent file: its append /
    prune never open, write, or rewrite `ANALYTICS_LOG_PATH`, and it
    holds a dedicated lock so the two sinks do not serialize against one
    another.
    """

    def test_dedicated_lock_is_distinct(self) -> None:
        _, analytics = _reload()
        self.assertIsNot(analytics._FILE_LOCK, analytics._TRAJECTORY_FILE_LOCK)

    def test_append_leaves_analytics_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            _, analytics = _reload({
                _ANALYTICS_LOG_PATH: str(a_path),
                _TRAJECTORY_LOG_PATH: str(t_path),
            })
            analytics.append_trajectory_record({"session_id": "s"})
            self.assertTrue(t_path.exists())
            # The analytics file was never opened by the trajectory append.
            self.assertFalse(a_path.exists())

    def test_prune_leaves_analytics_file_untouched(self) -> None:
        now = PRUNE_NOW
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            _, analytics = _reload({
                _ANALYTICS_LOG_PATH: str(a_path),
                _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                _TRAJECTORY_LOG_PATH: str(t_path),
                _TRAJECTORY_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
            })
            # An equally-old record in BOTH files; pruning trajectory must
            # drop only the trajectory record and never rewrite analytics.
            _write_json_lines(a_path, [{"ts": old_ts, "event": "x"}])
            _write_json_lines(t_path, [{"ts": old_ts, "session_id": "1"}])
            a_before = _read_text(a_path)
            self.assertEqual(analytics.prune_trajectory_records(now=now), 1)
            self.assertEqual(_read_text(t_path), "")
            # Analytics file is byte-for-byte unchanged.
            self.assertEqual(_read_text(a_path), a_before)

    def test_analytics_prune_ignores_trajectory(self) -> None:
        # Symmetric guard: the analytics prune must not rewrite the
        # trajectory file either.
        now = PRUNE_NOW
        old_ts = _ts_days_ago(VERY_OLD_RECORD_AGE_DAYS, now=now)
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            _, analytics = _reload({
                _ANALYTICS_LOG_PATH: str(a_path),
                _ANALYTICS_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
                _TRAJECTORY_LOG_PATH: str(t_path),
                _TRAJECTORY_RETENTION_DAYS: _DEFAULT_RETENTION_STR,
            })
            _write_json_lines(a_path, [{"ts": old_ts, "event": "x"}])
            _write_json_lines(t_path, [{"ts": old_ts, "session_id": "1"}])
            t_before = _read_text(t_path)
            self.assertEqual(analytics.prune_old_records(now=now), 1)
            self.assertEqual(_read_text(a_path), "")
            self.assertEqual(_read_text(t_path), t_before)


def _claude_trajectory_stdout(
    *,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    tool_result: object = "tool result text",
    final_output: str | None = "final answer",
    offered_tools: tuple[str, ...] = ("Read", "Bash"),
    input_tokens: int = CLAUDE_TRAJECTORY_INPUT_TOKENS,
    output_tokens: int = CLAUDE_TRAJECTORY_OUTPUT_TOKENS,
) -> str:
    """A claude stream-json stdout with offered tools, one tool_use /
    tool_result step, a usage block, and a terminal `result` answer -- the
    full surface `parse_claude_trajectory` reconstructs."""
    frames: list[dict] = [
        {"type": "system", "subtype": "init", "tools": list(offered_tools)},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": _CLAUDE_MODEL,
                "content": [{
                    "type": "tool_use",
                    "name": tool_name,
                    "id": "tu1",
                    "input": tool_input or {"command": "ls"},
                }],
                "usage": {
                    _INPUT_TOKENS: input_tokens,
                    _OUTPUT_TOKENS: output_tokens,
                },
            },
        },
        {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tu1",
                "content": tool_result,
            }]},
        },
    ]
    result_frame: dict = {"type": "result", "num_turns": 1}
    if final_output is not None:
        result_frame["result"] = final_output
    frames.append(result_frame)
    return "\n".join(json.dumps(frame) for frame in frames)


def _claude_multistep_stdout(*, n_steps: int, result_text: str) -> str:
    """A claude stream with `n_steps` tool_use / tool_result pairs (so
    `2 * n_steps` trajectory steps), each result carrying `result_text`. Used
    to drive the total-record-budget truncation."""
    frames: list[dict] = [
        {"type": "system", "subtype": "init", "tools": ["Bash"]},
    ]
    for index in range(n_steps):
        frames.append({
            "type": "assistant",
            "message": {
                "id": f"m{index}", "model": _CLAUDE_MODEL,
                "content": [{
                    "type": "tool_use", "name": "Bash", "id": f"tu{index}",
                    "input": {"command": "x"},
                }],
                "usage": {_INPUT_TOKENS: 1, _OUTPUT_TOKENS: 1},
            },
        })
        frames.append({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": f"tu{index}",
                "content": result_text,
            }]},
        })
    frames.append({"type": "result", "num_turns": n_steps})
    return "\n".join(json.dumps(frame) for frame in frames)


def _codex_trajectory_stdout(
    *,
    command: str = "ls -la",
    output: str = "command output",
    final: str | None = "codex done",
    input_tokens: int = CODEX_TRAJECTORY_INPUT_TOKENS,
    output_tokens: int = CODEX_TRAJECTORY_OUTPUT_TOKENS,
) -> str:
    """A codex --json stdout with one command_execution call + result and a
    final agent_message -- the surface `parse_codex_trajectory` reads."""
    frames: list[dict] = [
        {"type": "item.started", "item": {
            "id": "c1", "type": "command_execution", "command": command,
        }},
        {"type": "item.completed", "item": {
            "id": "c1", "type": "command_execution", "command": command,
            "aggregated_output": output,
        }},
    ]
    if final is not None:
        frames.append({"type": "item.completed", "item": {
            "id": "a1", "type": "agent_message", "text": final,
        }})
    frames.append({"type": "turn_complete", "usage": {
        _INPUT_TOKENS: input_tokens, _OUTPUT_TOKENS: output_tokens,
    }})
    return "\n".join(json.dumps(frame) for frame in frames)


class RecordAgentExitTrajectoryTest(unittest.TestCase):
    """`record_agent_exit` writes the opt-in trajectory record only when
    `TRAJECTORY_LOG_PATH` is enabled, redacts every free-text field, applies
    head/tail + total-size truncation caps, and never lets a trajectory
    failure drop the baseline `agent_exit` usage record."""

    def test_sink_off_writes_no_trajectory_or_input(self) -> None:
        # Default off: a prompt is passed but, with the trajectory sink
        # disabled, no trajectory file is created and the baseline
        # `agent_exit` record never carries `user_input`.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(),
                prompt="please implement the feature",
                traj_path=None,
                analytics_path=a_path,
            )
            # Only the analytics file exists -- no trajectory file anywhere.
            self.assertEqual(
                sorted(entry.name for entry in Path(td).iterdir()),
                ["analytics.jsonl"],
            )
            recs = _read_records(a_path)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["event"], _AGENT_EXIT)
            self.assertNotIn(_USER_INPUT, recs[0])

    def test_sink_on_writes_redacted_trajectory(self) -> None:
        # Sink on: a single `agent_trajectory` record carries the redacted
        # user_input, the offered tools, the ordered steps with their
        # tool_call input / tool_result content, and the final output --
        # alongside (not replacing) the baseline `agent_exit` record.
        analytics = _reload()[1]
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_input={"command": "echo hi"},
                    tool_result="hi",
                    final_output="implemented",
                ),
                prompt="implement X",
                traj_path=t_path,
                analytics_path=a_path,
            )
            self._assert_baseline_exit_record(a_path)
            record = self._read_single_trajectory(t_path)
            self._assert_claude_trajectory_identity(record)
            self._assert_claude_trajectory_steps(record)
            self._assert_claude_trajectory_usage(record)
            self.assertNotIn(_TRUNCATED, record)

    def test_codex_trajectory_record(self) -> None:
        # The codex backend dispatches through the same path: command +
        # aggregated_output become the tool_call / tool_result, the trailing
        # agent_message rides along as an assistant_message turn, and that
        # same last agent_message is the output.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_codex_trajectory_stdout(),
                prompt="codex prompt",
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
                backend=_CODEX,
            )
            rec = _read_records(t_path)[0]
            self.assertEqual(rec["event"], _AGENT_TRAJECTORY)
            self.assertEqual(rec[_BACKEND], _CODEX)
            self.assertEqual(rec[_USER_INPUT], "codex prompt")
            self.assertEqual(rec[_OUTPUT], "codex done")
            self.assertEqual(
                [step["kind"] for step in rec[_STEPS]],
                ["tool_call", "tool_result", "assistant_message"],
            )
            self.assertEqual(rec[_STEPS][0]["content"], "ls -la")
            self.assertEqual(rec[_STEPS][1]["content"], "command output")
            self.assertEqual(rec[_STEPS][2]["content"], "codex done")
            # The text turn carries no tool name / id.
            self.assertIsNone(rec[_STEPS][2]["name"])
            self.assertIsNone(rec[_STEPS][2]["tool_id"])
            # codex exposes no offered-tools frame, so the trajectory record
            # backfills the best-effort baseline out-of-band.
            from orchestrator import skill_catalog
            self.assertEqual(
                rec["tools"], list(skill_catalog.discover_codex_tools()),
            )
            # run_usage is codex's only usage surface: the denormalized
            # run-level totals, present even though per-turn detail is not.
            run_usage = rec[_RUN_USAGE]
            self.assertNotIn(_BACKEND, run_usage)
            self.assertEqual(
                run_usage[_INPUT_TOKENS], CODEX_TRAJECTORY_INPUT_TOKENS,
            )
            self.assertEqual(
                run_usage[_OUTPUT_TOKENS], CODEX_TRAJECTORY_OUTPUT_TOKENS,
            )
            # No priced model in the stream -> unknown-price, no cost.
            self.assertEqual(run_usage["cost_source"], "unknown-price")
            self.assertIsNone(run_usage["cost_usd"])
            # codex usage frames are cumulative, not per-turn: the per-turn
            # array is dropped and no step carries a `turn` index.
            self.assertNotIn("turns", rec)
            self.assertTrue(all("turn" not in step for step in rec[_STEPS]))

    def test_text_turns_redacted_capped_and_recorded(self) -> None:
        # New timeline items -- assistant / user text turns -- are stored as
        # their own steps and get the same treatment as tool payloads: stream
        # order preserved, secrets masked, over-long text head/tail truncated,
        # and `name` / `tool_id` null (text turns carry no tool metadata).
        _, analytics = _reload()
        secret = "sk-ant-TEXTLEAK-0123456789"
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}), \
                patch.object(
                    analytics,
                    "_TRAJECTORY_FIELD_HEAD",
                    _TRUNCATION_EDGE_CHARS,
                ), \
                patch.object(
                    analytics,
                    "_TRAJECTORY_FIELD_TAIL",
                    _TRUNCATION_EDGE_CHARS,
                ):
            t_path = Path(td) / "trajectory.jsonl"
            frames = [
                {"type": "system", "subtype": "init", "tools": ["Bash"]},
                {"type": "assistant", "message": {"id": "m1", "content": [
                    {"type": "text", "text": "B" * _LONG_TEXT_CHARS},
                    {"type": "tool_use", "name": "Bash", "id": "tu1",
                     "input": {"command": "ls"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": "ok"},
                    {"type": "text", "text": f"leak {secret}"}]}},
                {"type": "result", "result": "done"},
            ]
            stdout = "\n".join(json.dumps(frame) for frame in frames)
            self._emit(
                analytics,
                stdout=stdout,
                prompt="p",
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
            )
            rec = _read_records(t_path)[0]
            self.assertEqual(
                [step["kind"] for step in rec[_STEPS]],
                ["assistant_message", "tool_call", "tool_result",
                 "user_message"],
            )
            assistant_step = rec[_STEPS][0]
            # Long assistant text head/tail truncated; no tool metadata.
            self.assertLess(
                len(assistant_step["content"]), _LONG_TEXT_CHARS,
            )
            self.assertIn("chars elided", assistant_step["content"])
            self.assertIsNone(assistant_step["name"])
            self.assertIsNone(assistant_step["tool_id"])
            # Secret masked in the user text turn and nowhere survives.
            user_step = rec[_STEPS][3]
            self.assertEqual(user_step["kind"], "user_message")
            self.assertIn("***", user_step["content"])
            self.assertNotIn(secret, json.dumps(rec))

    def test_secrets_redacted_in_every_field(self) -> None:
        # The secret env value must not survive in user_input, the tool_call
        # input, the tool_result content, or the output. `_redact_secrets`
        # reads the live os.environ, so set a secret-shaped var around the
        # call and assert it is masked everywhere.
        _, analytics = _reload()
        secret = "sk-ant-DEADBEEF-secret-value-0123456789"
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_input={"command": f"echo {secret}"},
                    tool_result=f"leaked {secret} here",
                    final_output=f"the answer is {secret}",
                ),
                prompt=f"use token {secret}",
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
            )
            rec = _read_records(t_path)[0]
            blob = json.dumps(rec)
            self.assertNotIn(secret, blob)
            # The masking marker landed in each field that carried it.
            self.assertIn("***", rec[_USER_INPUT])
            self.assertIn("***", rec[_OUTPUT])
            self.assertIn("***", rec[_STEPS][0]["content"])
            self.assertIn("***", rec[_STEPS][1]["content"])

    def test_multiline_tool_secret_is_redacted(self) -> None:
        # Regression: dict / list tool payloads are redacted leaf-by-leaf
        # BEFORE JSON serialization. A multiline secret env value would
        # otherwise have its newlines escaped by `json.dumps` (`\n` -> the
        # two-char escape), leaving `_redact_secrets`' literal `str.replace`
        # unable to match the raw value -- so the secret would leak into
        # `steps[].content`. Redacting raw leaves first keeps it masked, for
        # both the dict tool_call input and the list tool_result content.
        _, analytics = _reload()
        secret = "topsecretvalue\nwith-newline-marker-0123456789"
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {"MULTILINE_SECRET_KEY": secret}):
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_input={"command": f"echo {secret}"},
                    tool_result=[{"type": "text", "text": f"saw {secret}"}],
                    final_output="done",
                ),
                prompt="p",
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
            )
            rec = _read_records(t_path)[0]
            blob = json.dumps(rec)
            # Neither the raw value nor its distinctive post-newline marker
            # survives anywhere in the record.
            self.assertNotIn("with-newline-marker-0123456789", blob)
            self.assertNotIn("topsecretvalue", blob)
            # Both the dict input and the list content carry the mask.
            self.assertIn("***", rec[_STEPS][0]["content"])
            self.assertIn("***", rec[_STEPS][1]["content"])

    def test_per_step_content_head_tail_truncated(self) -> None:
        # A long field is redacted then truncated to head + tail chars with
        # an elision marker, so a single huge tool output cannot bloat one
        # step. Shrink the caps so the test stays small.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(
                    analytics,
                    "_TRAJECTORY_FIELD_HEAD",
                    _TRUNCATION_EDGE_CHARS,
                ), \
                patch.object(
                    analytics,
                    "_TRAJECTORY_FIELD_TAIL",
                    _TRUNCATION_EDGE_CHARS,
                ):
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(
                    tool_result="A" * _LONG_TEXT_CHARS,
                    final_output="done",
                ),
                prompt="p",
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
            )
            rec = _read_records(t_path)[0]
            result_step = next(
                step for step in rec[_STEPS] if step["kind"] == "tool_result"
            )
            body = result_step["content"]
            self.assertLess(len(body), _LONG_TEXT_CHARS)
            edge = "A" * _TRUNCATION_EDGE_CHARS
            self.assertTrue(body.startswith(edge))
            self.assertTrue(body.endswith(edge))
            self.assertIn("chars elided", body)

    def test_total_record_budget_drops_excess_steps(self) -> None:
        # When the cumulative redacted content crosses the record budget the
        # remaining steps are dropped and `truncated` is set, so one runaway
        # run cannot write an unbounded JSONL line.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(analytics, "_TRAJECTORY_RECORD_BUDGET", 2000):
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_claude_multistep_stdout(
                    n_steps=_BUDGET_TOOL_PAIR_COUNT,
                    result_text="0123456789" * 20,
                ),
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
            )
            rec = _read_records(t_path)[0]
            self.assertTrue(rec[_TRUNCATED])
            # 5 pairs => 10 steps emitted; the budget dropped the tail but
            # kept a prefix.
            self.assertGreater(len(rec[_STEPS]), 0)
            self.assertLess(
                len(rec[_STEPS]), _BUDGET_TOOL_PAIR_COUNT * 2,
            )
            # The 5 small per-turn entries fit under the budget (they are drawn
            # down before the steps), so all are kept while the step tail is
            # dropped; a turns array that itself overflows is truncated too
            # (see test_turns_array_respects_total_budget).
            self.assertEqual(
                len(rec["turns"]), _BUDGET_TOOL_PAIR_COUNT,
            )
            self.assertIn(_RUN_USAGE, rec)

    def test_turns_array_respects_total_budget(self) -> None:
        # Regression: the per-turn `turns[]` array is charged AND truncated
        # under the record budget, not merely charged. A claude run with
        # thousands of turns but no steps would otherwise write the whole
        # array in full via `build_record` and overshoot the budget by its
        # size -- the reviewer reproduced ~914 KB with zero steps kept.
        _, analytics = _reload()
        many = analytics.usage.AgentTrajectory(
            backend=_CLAUDE,
            turns=tuple(
                analytics.usage.TurnUsage(
                    turn=index,
                    model=_CLAUDE_MODEL,
                    input_tokens=1,
                    output_tokens=1,
                )
                for index in range(_MANY_TURNS_COUNT)
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            t_path = Path(td) / "trajectory.jsonl"
            with patch.object(
                analytics.usage, "parse_agent_trajectory", return_value=many,
            ):
                self._emit(
                    analytics,
                    stdout="",  # ignored: the trajectory parser is stubbed
                    prompt="p",
                    traj_path=t_path,
                    analytics_path=Path(td) / "a.jsonl",
                )
            raw = _read_text(t_path)
            rec = json.loads(raw)
            self.assertTrue(rec[_TRUNCATED])
            self.assertLess(len(rec["turns"]), _MANY_TURNS_COUNT)
            # The on-disk line is bounded near the budget, not the ~914 KB an
            # uncapped turns array produced.
            self.assertLess(len(raw), analytics._TRAJECTORY_RECORD_BUDGET * 2)

    def test_metadata_only_steps_respect_total_budget(self) -> None:
        # Regression: the budget must count each step's serialized metadata,
        # not just `len(content)`. A run of 10,000 empty-content steps -- each
        # still ~80 bytes of `kind` / `name` / `tool_id` JSON -- would
        # otherwise produce a multi-hundred-KB record with NO `truncated`
        # flag, because the old content-length-only check never advanced.
        _, analytics = _reload()
        many = analytics.usage.AgentTrajectory(
            backend=_CLAUDE,
            steps=tuple(
                analytics.usage.TrajectoryStep(
                    kind="tool_call",
                    name="command_execution",
                    tool_id=f"id{index}",
                    content=None,
                )
                for index in range(_METADATA_ONLY_STEP_COUNT)
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            t_path = Path(td) / "trajectory.jsonl"
            with patch.object(
                analytics.usage, "parse_agent_trajectory", return_value=many,
            ):
                self._emit(
                    analytics,
                    stdout="",  # ignored: the trajectory parser is stubbed
                    prompt="p",
                    traj_path=t_path,
                    analytics_path=Path(td) / "a.jsonl",
                )
            raw = _read_text(t_path)
            rec = json.loads(raw)
            self.assertTrue(rec[_TRUNCATED])
            self.assertLess(
                len(rec[_STEPS]), _METADATA_ONLY_STEP_COUNT,
            )
            # The on-disk line is bounded near the budget, not the ~749 KB an
            # uncapped run produced -- one step of overshoot plus the envelope.
            self.assertLess(len(raw), analytics._TRAJECTORY_RECORD_BUDGET * 2)

    def test_parser_failure_keeps_baseline_and_skills(self) -> None:
        # The trajectory parse rides its own fail-open guard: a parser bug
        # logs and is swallowed, leaving the baseline `agent_exit` record AND
        # the skill-trigger return value (which drives the audit events)
        # intact.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            t_path = Path(td) / "trajectory.jsonl"
            with patch.object(
                analytics.usage, "parse_agent_trajectory",
                side_effect=RuntimeError("boom"),
            ), self.assertLogs(analytics.log, level="ERROR"):
                returned = self._emit(
                    analytics,
                    stdout=_claude_stdout_with_skills(skills=(_DEVELOP,)),
                    prompt="p",
                    traj_path=t_path,
                    analytics_path=a_path,
                    track=True,
                )
            # Skill return value (and thus audit emission) is unaffected.
            self.assertEqual(returned, [_DEVELOP])
            # Baseline record survived...
            base = _read_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertEqual(base[0]["event"], _AGENT_EXIT)
            self.assertEqual(
                base[0][_INPUT_TOKENS], SKILL_STREAM_INPUT_TOKENS,
            )
            # ...and the broken trajectory wrote nothing.
            self.assertFalse(t_path.exists())

    def test_sink_failure_keeps_baseline_record(self) -> None:
        # A non-OSError escaping the sink append (a programming error past
        # the inner OSError swallow) must not drop the baseline record: the
        # outer guard logs and falls through.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            a_path = Path(td) / "analytics.jsonl"
            with patch.object(
                analytics, "append_trajectory_record",
                side_effect=RuntimeError("sink boom"),
            ), self.assertLogs(analytics.log, level="ERROR"):
                self._emit(
                    analytics,
                    stdout=_claude_trajectory_stdout(),
                    prompt="p",
                    traj_path=Path(td) / "trajectory.jsonl",
                    analytics_path=a_path,
                )
            base = _read_records(a_path)
            self.assertEqual(len(base), 1)
            self.assertEqual(base[0]["event"], _AGENT_EXIT)

    def test_absent_prompt_drops_user_input(self) -> None:
        # No prompt passed -> `user_input` is dropped (not stored as null),
        # while the rest of the trajectory still records.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            t_path = Path(td) / "trajectory.jsonl"
            self._emit(
                analytics,
                stdout=_claude_trajectory_stdout(final_output="x"),
                prompt=None,
                traj_path=t_path,
                analytics_path=Path(td) / "a.jsonl",
            )
            rec = _read_records(t_path)[0]
            self.assertNotIn(_USER_INPUT, rec)
            self.assertEqual(rec[_OUTPUT], "x")

    def _emit(
        self,
        analytics,
        *,
        stdout,
        prompt=None,
        traj_path=None,
        analytics_path=None,
        backend=_CLAUDE,
        track=False,
    ):
        with patch.object(analytics, _ANALYTICS_LOG_PATH, analytics_path), \
                patch.object(analytics, _TRAJECTORY_LOG_PATH, traj_path), \
                patch.object(analytics, _TRACK_SKILL_TRIGGERS, track):
            return analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage=_STAGE_IMPLEMENTING,
                agent_role=_DEVELOPER,
                backend=backend,
                agent_spec=backend,
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess-traj",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                duration_s=0.0,
                review_round=TRAJECTORY_REVIEW_ROUND,
                retry_count=TRAJECTORY_RETRY_COUNT,
                prompt=prompt,
            )

    def _assert_baseline_exit_record(self, path: Path) -> None:
        records = _read_records(path)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["event"], _AGENT_EXIT)
        self.assertEqual(
            record[_INPUT_TOKENS],
            CLAUDE_TRAJECTORY_INPUT_TOKENS,
        )
        self.assertNotIn(_USER_INPUT, record)
        self.assertNotIn(_RUN_USAGE, record)

    def _read_single_trajectory(self, path: Path) -> dict:
        records = _read_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], _AGENT_TRAJECTORY)
        return records[0]

    def _assert_claude_trajectory_identity(self, record: dict) -> None:
        expected = {
            "event": _AGENT_TRAJECTORY,
            "repo": _REPO,
            "issue": AGENT_EXIT_ISSUE_NUMBER,
            "stage": _STAGE_IMPLEMENTING,
            "agent_role": _DEVELOPER,
            _BACKEND: _CLAUDE,
            "session_id": "sess-traj",
            "review_round": TRAJECTORY_REVIEW_ROUND,
            "retry_count": TRAJECTORY_RETRY_COUNT,
            _USER_INPUT: "implement X",
            "tools": ["Read", "Bash"],
            _OUTPUT: "implemented",
        }
        self.assertEqual(
            {key: record[key] for key in expected},
            expected,
        )

    def _assert_claude_trajectory_steps(self, record: dict) -> None:
        steps = record[_STEPS]
        tool_call = steps[0]
        self.assertEqual(
            {
                "kinds": [step["kind"] for step in steps],
                "tool_name": tool_call["name"],
                "tool_result": steps[1]["content"],
                "tool_turn": tool_call["turn"],
            },
            {
                "kinds": ["tool_call", "tool_result"],
                "tool_name": "Bash",
                "tool_result": "hi",
                "tool_turn": 0,
            },
        )
        self.assertIn("echo hi", tool_call["content"])
        # Tool results become the next turn's input; only the billed call
        # carries the current turn index.
        self.assertNotIn("turn", steps[1])

    def _assert_claude_trajectory_usage(self, record: dict) -> None:
        run_usage = record[_RUN_USAGE]
        expected_run = {
            _INPUT_TOKENS: CLAUDE_TRAJECTORY_INPUT_TOKENS,
            _OUTPUT_TOKENS: CLAUDE_TRAJECTORY_OUTPUT_TOKENS,
            "models": [_CLAUDE_MODEL],
            "turns": 1,
            "cost_source": "estimated",
        }
        self.assertNotIn(_BACKEND, run_usage)
        self.assertEqual(
            {key: run_usage[key] for key in expected_run},
            expected_run,
        )

        turns = record["turns"]
        expected_turn = {
            "turn": 0,
            "model": _CLAUDE_MODEL,
            _INPUT_TOKENS: CLAUDE_TRAJECTORY_INPUT_TOKENS,
            _OUTPUT_TOKENS: CLAUDE_TRAJECTORY_OUTPUT_TOKENS,
            "cost_source": "estimated",
        }
        self.assertEqual(len(turns), 1)
        self.assertEqual(
            {key: turns[0][key] for key in expected_turn},
            expected_turn,
        )


def _mk_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# skill\n", encoding=_ENCODING)


class RecordAgentExitCodexSkillDiscoveryTest(unittest.TestCase):
    """Codex has no offered-skills stream frame, so `record_agent_exit`
    backfills `skills_available` out-of-band from the worktree / `$CODEX_HOME`
    skill roots (via `skill_catalog.discover_local_skills`) -- into both the
    `agent_exit` record (behind `TRACK_SKILL_TRIGGERS`) and the trajectory
    record (behind `TRAJECTORY_LOG_PATH`). Claude is untouched (its offered
    set rides the stream), and a run with no worktree stays empty."""

    def test_agent_exit_records_discovered_skills(self) -> None:
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "wt"
            _mk_skill(cwd / ".agents/skills", _DEVELOP)
            _mk_skill(cwd / ".agents/skills", _REVIEW)
            with patch.dict(os.environ, {_CODEX_HOME: str(Path(td) / "none")}):
                base, _ = self._emit(
                    analytics, backend=_CODEX, cwd=cwd, td=td, track=True,
                )
        rec = base[0]
        self.assertEqual(rec["event"], _AGENT_EXIT)
        # No SKILL.md read in the stream -> nothing triggered, but the offered
        # set is filled from the filesystem scan.
        self.assertEqual(rec[_SKILLS_AVAILABLE], [_DEVELOP, _REVIEW])
        self.assertNotIn(_SKILLS_TRIGGERED, rec)

    def test_trajectory_records_discovered_skills(self) -> None:
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "wt"
            _mk_skill(cwd / ".claude/skills", _REVIEW)
            with patch.dict(os.environ, {_CODEX_HOME: str(Path(td) / "none")}):
                _, traj = self._emit(
                    analytics, backend=_CODEX, cwd=cwd, td=td, traj=True,
                )
        rec = traj[0]
        self.assertEqual(rec["event"], _AGENT_TRAJECTORY)
        self.assertEqual(rec[_BACKEND], _CODEX)
        self.assertEqual(rec[_SKILLS_AVAILABLE], [_REVIEW])
        # The offered-tools baseline is backfilled onto the same record.
        from orchestrator import skill_catalog
        self.assertEqual(rec["tools"], list(skill_catalog.discover_codex_tools()))

    def test_no_worktree_leaves_codex_available_empty(self) -> None:
        # No worktree -> no skill discovery; the offered-tools baseline needs
        # no worktree, so the trajectory record still carries `tools`.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {_CODEX_HOME: str(Path(td) / "none")}):
                base, traj = self._emit(
                    analytics, backend=_CODEX, cwd=None, td=td,
                    track=True, traj=True,
                )
        self.assertNotIn(_SKILLS_AVAILABLE, base[0])
        self.assertNotIn(_SKILLS_AVAILABLE, traj[0])
        from orchestrator import skill_catalog
        self.assertEqual(traj[0]["tools"], list(skill_catalog.discover_codex_tools()))

    def test_claude_offered_set_not_from_discovery(self) -> None:
        # Discovery is codex-only: a claude run in a worktree full of skill
        # dirs still takes its offered set from the stream (here: none), never
        # from the filesystem, so a stray scan can't invent a claude field.
        _, analytics = _reload()
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "wt"
            _mk_skill(cwd / ".agents/skills", _DEVELOP)
            a_path = Path(td) / "a.jsonl"
            with patch.dict(os.environ, {_CODEX_HOME: str(Path(td) / "none")}), \
                    patch.object(analytics, _ANALYTICS_LOG_PATH, a_path), \
                    patch.object(analytics, _TRAJECTORY_LOG_PATH, None), \
                    patch.object(analytics, _TRACK_SKILL_TRIGGERS, True):
                analytics.record_agent_exit(
                    repo=_REPO,
                    issue=AGENT_EXIT_ISSUE_NUMBER,
                    stage=_STAGE_IMPLEMENTING,
                    agent_role=_DEVELOPER, backend=_CLAUDE,
                    agent_spec=_CLAUDE, resume_session_id=None,
                    result=analytics.AgentResult(
                        session_id="s", last_message="", exit_code=0,
                        timed_out=False,
                        stdout=_claude_stdout_with_skills(skills=()),
                        stderr="",
                    ),
                    duration_s=0.0, review_round=0, retry_count=0, cwd=cwd,
                )
            self.assertNotIn(_SKILLS_AVAILABLE, _read_records(a_path)[0])

    def _emit(
        self, analytics, *, backend, cwd, td, track=False, traj=False,
    ) -> tuple[list[dict], list[dict]]:
        a_path = Path(td) / "a.jsonl"
        t_path = Path(td) / "t.jsonl" if traj else None
        with patch.object(analytics, _ANALYTICS_LOG_PATH, a_path), \
                patch.object(analytics, _TRAJECTORY_LOG_PATH, t_path), \
                patch.object(analytics, _TRACK_SKILL_TRIGGERS, track):
            analytics.record_agent_exit(
                repo=_REPO,
                issue=AGENT_EXIT_ISSUE_NUMBER,
                stage="validating",
                agent_role="reviewer",
                backend=backend,
                agent_spec=backend,
                resume_session_id=None,
                result=analytics.AgentResult(
                    session_id="sess",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_codex_trajectory_stdout(),
                    stderr="",
                ),
                duration_s=0.0,
                review_round=1,
                retry_count=0,
                prompt="review this",
                cwd=cwd,
            )
        traj_recs = _read_records(t_path) if t_path else []
        return _read_records(a_path), traj_recs


class RecordingFacadeTest(unittest.TestCase):
    """The event-recording implementation lives in
    `orchestrator.analytics._recording`, the opt-in trajectory sink in
    `orchestrator.analytics._trajectories`, and the by-age retention prune
    entry points in `orchestrator.analytics._retention`; the package
    re-exports all three as a facade, each package instance carries its own
    submodules, and the recorders read sink knobs / call sibling recorders
    back off the facade, so a reference held across a `_reload` keeps
    dispatching to the instance its own callers patched.
    """

    def test_recorders_defined_in_recording_module(self) -> None:
        _, analytics = _reload()
        for name in (
            "append_record",
            "build_record",
            "record_agent_exit",
            "record_repo_skill_catalog",
            "record_stage_enter",
            "record_stage_evaluation",
        ):
            with self.subTest(name=name):
                member = getattr(analytics, name)
                self.assertEqual(
                    member.__module__, "orchestrator.analytics._recording",
                )
                self.assertIs(member, getattr(analytics._recording, name))

    def test_trajectory_recorder_defined_in_submodule(self) -> None:
        _, analytics = _reload()
        for name in ("append_trajectory_record",):
            with self.subTest(name=name):
                member = getattr(analytics, name)
                self.assertEqual(
                    member.__module__, "orchestrator.analytics._trajectories",
                )
                self.assertIs(member, getattr(analytics._trajectories, name))

    def test_prune_entry_points_in_retention_module(self) -> None:
        _, analytics = _reload()
        for name in (
            "prune_old_records",
            "prune_trajectory_records",
            "prune_with_retention_logging",
        ):
            with self.subTest(name=name):
                member = getattr(analytics, name)
                self.assertEqual(
                    member.__module__, "orchestrator.analytics._retention",
                )
                self.assertIs(member, getattr(analytics._retention, name))

    def test_internal_append_routes_via_facade(self) -> None:
        # A recorder's internal `append_record` is late-bound through the
        # facade, so patching `analytics.append_record` intercepts it.
        _, analytics = _reload()
        captured: list[dict] = []
        with patch.object(analytics, "append_record", captured.append):
            analytics.record_stage_enter(
                repo=_REPO_SHORT, issue=1, stage=_STAGE_IMPLEMENTING,
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["event"], _STAGE_ENTER)

    def test_reload_keeps_stale_facade_reference(self) -> None:
        # A holder that imported the package before a `_reload` keeps its own
        # instance: its recorders read the knobs patched on THAT instance, not
        # the freshly reloaded one that now sits in `sys.modules`.
        _, stale = _reload()
        captured_stale: list[dict] = []
        stale_patch = patch.object(stale, "append_record", captured_stale.append)
        stale_patch.start()
        self.addCleanup(stale_patch.stop)
        _, fresh = _reload()
        self.assertIsNot(fresh, stale)
        captured_fresh: list[dict] = []
        with patch.object(fresh, "append_record", captured_fresh.append):
            fresh.record_stage_enter(repo=_REPO_SHORT, issue=2, stage="fixing")
        stale.record_stage_enter(
            repo=_REPO_SHORT, issue=1, stage=_STAGE_IMPLEMENTING,
        )
        self.assertEqual([rec["issue"] for rec in captured_fresh], [2])
        self.assertEqual([rec["issue"] for rec in captured_stale], [1])


if __name__ == "__main__":
    unittest.main()
