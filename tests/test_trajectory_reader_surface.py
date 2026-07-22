# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory reader facade and reload-isolation tests."""

import os


import sys


import tempfile


import unittest


from dataclasses import dataclass


from pathlib import Path


from unittest.mock import patch


from orchestrator import trajectory_reader as tr


from orchestrator import _trajectory_records as records


_LOG_PATH_ATTR = "TRAJECTORY_LOG_PATH"


_READER_MODULE = "orchestrator.trajectory_reader"


_ANALYTICS_MODULE = "orchestrator.analytics"


_CONFIG_MODULE = "orchestrator.config"


_ORCHESTRATOR_PKG = "orchestrator"


def _reload_reader_world(log_path, hermetic):
    """Reload analytics + reader against `log_path` and return the fresh pair.

    Pops only the PUBLIC modules a caller would reload -- not the private
    `_trajectory_records` leaf -- so the reload test exercises the facade's own
    eviction rather than masking it.
    """
    import importlib

    reload_env = {**hermetic, _LOG_PATH_ATTR: str(log_path)}
    with patch.dict(os.environ, reload_env, clear=True):
        for name in (_READER_MODULE, _ANALYTICS_MODULE, _CONFIG_MODULE):
            sys.modules.pop(name, None)
        # Re-import through `importlib` so a popped submodule is rebuilt
        # rather than resolved from the parent package's stale attribute.
        fresh_analytics = importlib.import_module(_ANALYTICS_MODULE)
        fresh_reader = importlib.import_module(_READER_MODULE)
        return fresh_analytics, fresh_reader


def _snapshot_and_arm_orchestrator_reset(test):
    """Snapshot every `orchestrator*` module + the package namespace, restore after `test`.

    Importing a submodule binds it as an attribute of its parent package, so an
    A/B reload rebinds `orchestrator.analytics` (and `.config` /
    `.trajectory_reader` / `._trajectory_records`) on the persistent
    `orchestrator` package object. Restoring `sys.modules` alone would leave
    `from orchestrator import analytics` (how the reader leaf resolves
    `TRAJECTORY_LOG_PATH`) pointing at a discarded reload, so the package's own
    namespace is snapshotted and reverted too.
    """
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith(_ORCHESTRATOR_PKG)
    }
    orchestrator_pkg = sys.modules[_ORCHESTRATOR_PKG]
    test.addCleanup(
        _restore_orchestrator_modules,
        saved,
        orchestrator_pkg,
        dict(orchestrator_pkg.__dict__),
    )


def _restore_orchestrator_modules(saved, orchestrator_pkg, saved_pkg_attrs):
    """Evict the current `orchestrator*` modules and reinstate the snapshot."""
    stale = [name for name in sys.modules if name.startswith(_ORCHESTRATOR_PKG)]
    for name in stale:
        sys.modules.pop(name, None)
    sys.modules.update(saved)
    orchestrator_pkg.__dict__.clear()
    orchestrator_pkg.__dict__.update(saved_pkg_attrs)


@dataclass(frozen=True)
class _ReaderWorld:
    path: Path
    analytics: object
    reader: object

    @classmethod
    def load(cls, path: Path, hermetic: dict[str, str]) -> "_ReaderWorld":
        analytics, reader = _reload_reader_world(path, hermetic)
        return cls(path, analytics, reader)


class ModuleLayoutTest(unittest.TestCase):
    """Pin the facade / read-leaf split so callers keep one import site.

    The record and view dataclasses, the log-path resolution, and the JSONL
    parsing / reading pipeline live in the private
    `orchestrator._trajectory_records` leaf; `orchestrator.trajectory_reader`
    re-exports them under the same names and owns the filtering and
    summary aggregation. The dashboard and the tests reach everything through
    `trajectory_reader`, so the re-exported names must stay the same objects
    the leaf defines and the filter surface must stay defined on the facade.
    """

    def test_read_surface_reexported_from_leaf(self) -> None:
        for name in (
            "TrajectoryStepView",
            "TimelineEntry",
            "TurnUsageView",
            "RunUsageView",
            "TrajectoryRun",
            "resolve_log_path",
            "log_unconfigured_message",
            "read_trajectories",
            "parse_record",
            "TRAJECTORY_EVENT",
            "TIMELINE_PROMPT",
            "TIMELINE_OUTPUT",
            "UNCONFIGURED_LOG_MESSAGE",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(tr, name), getattr(records, name))

    def test_read_symbols_have_leaf_module_of_record(self) -> None:
        for symbol in (
            tr.TrajectoryRun,
            tr.TrajectoryStepView,
            tr.parse_record,
            tr.read_trajectories,
            tr.resolve_log_path,
        ):
            with self.subTest(symbol=symbol.__name__):
                self.assertEqual(symbol.__module__, "orchestrator._trajectory_records")

    def test_filter_surface_defined_on_facade(self) -> None:
        for symbol in (
            tr.FilterOptions,
            tr.RunFilterOptions,
            tr.TrajectorySummary,
            tr.filter_options,
            tr.filter_runs,
            tr.summarize,
        ):
            with self.subTest(symbol=symbol.__name__):
                self.assertEqual(symbol.__module__, _READER_MODULE)

    def test_reload_binds_reader_to_its_world(self) -> None:
        """A reloaded reader resolves its own world's `TRAJECTORY_LOG_PATH`.

        Reloading `orchestrator.analytics` and `orchestrator.trajectory_reader`
        together must give the fresh reader a leaf bound to the fresh analytics
        instance, and the earlier world's reader must keep resolving the earlier
        world's path -- the A/B env isolation the single-module reader had.
        Without the facade evicting its cached `_trajectory_records`, the fresh
        reader would re-export the stale leaf and resolve the previous path.
        """
        hermetic = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        _snapshot_and_arm_orchestrator_reset(self)
        with tempfile.TemporaryDirectory() as work_dir:
            world_a = _ReaderWorld.load(Path(work_dir) / "a.jsonl", hermetic)
            world_b = _ReaderWorld.load(Path(work_dir) / "b.jsonl", hermetic)
            # Each reader's leaf is bound to its own analytics instance, so
            # world A still resolves world A after world B has been loaded.
            self.assertIsNot(world_a.reader, world_b.reader)
            self.assertIsNot(world_a.analytics, world_b.analytics)
            self.assertEqual(
                (
                    world_a.reader.resolve_log_path(),
                    world_b.reader.resolve_log_path(),
                ),
                (world_a.path, world_b.path),
            )
