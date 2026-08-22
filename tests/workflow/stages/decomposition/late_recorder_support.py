# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What pinned state held INSIDE a side effect the late mode makes.

Classes rather than closures for the reason the stage tests' recorders are:
the orders these pin down -- the record written before the agent starts, and
the body preserved before the pull request is edited -- are only visible from
inside the call that makes them. A record written afterwards would still be
there by the time a test looked.
"""
from __future__ import annotations

from tests.workflow.stages.decomposition.late_test_support import (
    LATE_ISSUE_NUMBER,
)


class SpawnSnapshot:
    """What pinned state held at the moment the agent was started.

    The persist-before-spawn order is only visible from inside the spawn: a
    record written afterwards would still be there by the time a test looked.
    """

    def __init__(self, github, agent_result) -> None:
        self.snapshots: list[dict] = []
        self._github = github
        self._agent_result = agent_result

    def __call__(self, *_args, **_kwargs):
        self.snapshots.append(self._github.pinned_data(LATE_ISSUE_NUMBER))
        return self._agent_result


class HoldSnapshot:
    """What pinned state held each time the plan PR body was rewritten."""

    def __init__(self, github) -> None:
        self.snapshots: list[dict] = []
        self._github = github
        self._edit = github.edit_pr_body

    def __call__(self, pr, body):
        self.snapshots.append(self._github.pinned_data(LATE_ISSUE_NUMBER))
        return self._edit(pr, body)
