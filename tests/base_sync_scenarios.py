# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from unittest.mock import MagicMock

from orchestrator import workflow

from tests.base_sync_test_support import (
    AFTER_SHA,
    BEFORE_SHA,
    ISSUE,
    THREE_BEHIND_STDOUT,
    TWO_BEHIND_STDOUT,
    _git_result,
    _patch_base_sync,
)

REBASE_PATCH = "rebase"
PUSH_PATCH = "push"


@dataclass(frozen=True)
class _BaseSyncScenario:
    patches: Mapping[str, object]

    def __getitem__(self, alias: str):
        return self.patches[alias]

    def run(self, fixture) -> None:
        with _patch_base_sync(**self.patches):
            workflow._sync_worktree_with_base(
                fixture.gh,
                fixture.spec,
                fixture.wt,
                ISSUE,
            )


def _scenario(**patches: object) -> _BaseSyncScenario:
    return _BaseSyncScenario(MappingProxyType(dict(patches)))


def _clean_rebase_scenario(
    behind_stdout: str = TWO_BEHIND_STDOUT,
    *,
    push_result: bool = True,
) -> _BaseSyncScenario:
    return _scenario(
        dirty=MagicMock(return_value=[]),
        **{
            REBASE_PATCH: MagicMock(return_value=(True, [])),
            PUSH_PATCH: MagicMock(return_value=push_result),
        },
        head_sha=MagicMock(side_effect=[BEFORE_SHA, AFTER_SHA]),
        git=MagicMock(return_value=_git_result(stdout=behind_stdout)),
        hardened=MagicMock(return_value=_git_result()),
    )


def _conflict_rebase_scenario() -> _BaseSyncScenario:
    return _scenario(
        dirty=MagicMock(return_value=[]),
        **{
            REBASE_PATCH: MagicMock(
                return_value=(False, ["src/feature.py", "tests/foo.py"]),
            ),
            PUSH_PATCH: MagicMock(),
        },
        head_sha=MagicMock(return_value=BEFORE_SHA),
        git=MagicMock(
            return_value=_git_result(stdout=THREE_BEHIND_STDOUT),
        ),
        hardened=MagicMock(return_value=_git_result()),
    )
