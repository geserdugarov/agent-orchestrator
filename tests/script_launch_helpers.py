# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Import-world isolation for direct-script launch tests."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import ModuleType


ModulePredicate = Callable[[str], bool]


def clear_modules(predicate: ModulePredicate) -> None:
    """Remove every loaded module selected by ``predicate``."""
    for name in tuple(sys.modules):
        if predicate(name):
            sys.modules.pop(name, None)


def restore_sys_path(path_entries: list[str]) -> None:
    """Replace ``sys.path`` with a captured sequence."""
    sys.path.clear()
    sys.path.extend(path_entries)


def drop_repo_root(repo_root: Path) -> None:
    """Remove one repository root while preserving other search paths."""
    resolved_root = repo_root.resolve()
    kept_entries = [
        entry
        for entry in sys.path
        if not entry or Path(entry).resolve() != resolved_root
    ]
    restore_sys_path(kept_entries)


def snapshot_modules(predicate: ModulePredicate) -> dict[str, ModuleType]:
    """Capture matching modules for exact post-test restoration."""
    return {name: module for name, module in sys.modules.items() if predicate(name)}


def restore_launch_state(
    original_path: list[str],
    saved_modules: dict[str, ModuleType],
    predicate: ModulePredicate,
) -> None:
    """Restore both search paths and the selected module world."""
    restore_sys_path(original_path)
    clear_modules(predicate)
    sys.modules.update(saved_modules)


def arm_launch_cleanup(
    test_case: unittest.TestCase,
    predicate: ModulePredicate,
) -> None:
    """Register import-world restoration on one unittest case."""
    test_case.addCleanup(
        restore_launch_state,
        list(sys.path),
        snapshot_modules(predicate),
        predicate,
    )


@contextmanager
def script_launch_sandbox(predicate: ModulePredicate) -> Iterator[ExitStack]:
    """Yield a cleanup stack inside an isolated direct-launch world."""
    original_path = list(sys.path)
    saved_modules = snapshot_modules(predicate)
    with ExitStack() as cleanup:
        cleanup.callback(sys.modules.update, saved_modules)
        cleanup.callback(clear_modules, predicate)
        cleanup.callback(restore_sys_path, original_path)
        yield cleanup
