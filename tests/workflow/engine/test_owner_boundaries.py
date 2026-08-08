# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The two collaborators the engine reaches outside itself, and where they land.

Both are the seams a test replaces to drive the engine without touching the
host: the spawn every tracked run dispatches through belongs to
`agents/runner.py`, and the pre-tick base refresh belongs to
`git/base_sync/refresh.py`. Each is imported from that owner rather than read
off the `orchestrator.workflow` facade, so a mock left on the facade would let
a real CLI or a real `git fetch` run. Both cases patch BOTH -- the owner mock
has to answer and the facade guard has to stay untouched -- which is what fails
if a call site drifts back to `_wf`.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.agents import runner as _agent_runner
from orchestrator.workflow.engine import dispatch as _dispatch
from orchestrator.workflow.engine import tick as _tick
from orchestrator.workflow.engine import usage as _usage

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_git_owners import seam_patch
from tests.workflow_helpers import _FAKE_WT, _TEST_SPEC, _agent
from tests.workflow_owner_boundaries import OwnerBoundaryMixin

BOUNDARY_ISSUE = 940
BOUNDARY_SESSION = "sess-boundary"
BOUNDARY_PROMPT = "implement the thing"
LABEL_IMPLEMENTING = "implementing"
BACKEND_CLAUDE = "claude"
ROLE_DEVELOPER = "developer"

RUN_AGENT = "run_agent"
REFRESH_BASE = "_refresh_base_and_worktrees"
PROCESS_ISSUE = "_process_issue"


class AgentSpawnBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The tracked spawn dispatches on the agent runner owner."""

    def test_tracked_run_spawns_on_the_runner_owner(self) -> None:
        gh = FakeGitHubClient()
        spawned = _agent(session_id=BOUNDARY_SESSION)
        with (
            self.facade_out_of_the_path(RUN_AGENT, returns=_agent()),
            patch.object(
                _agent_runner, RUN_AGENT, return_value=spawned,
            ) as spawn,
        ):
            agent_result = _usage._run_agent_tracked(
                gh,
                BOUNDARY_ISSUE,
                agent_role=ROLE_DEVELOPER,
                stage=LABEL_IMPLEMENTING,
                backend=BACKEND_CLAUDE,
                prompt=BOUNDARY_PROMPT,
                cwd=_FAKE_WT,
            )
            spawn.assert_called_once()
        # The result the owner handed back is what the audit pair bookends.
        self.assertIs(agent_result, spawned)


class BaseRefreshBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The pre-tick base refresh lands on the base-sync refresh owner."""

    def test_tick_refreshes_on_the_base_sync_owner(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(BOUNDARY_ISSUE, label=LABEL_IMPLEMENTING))
        refresh = MagicMock()
        with (
            self.facade_out_of_the_path(REFRESH_BASE),
            seam_patch(REFRESH_BASE, refresh),
            patch.object(_dispatch, PROCESS_ISSUE, MagicMock()),
        ):
            _tick.tick(gh, _TEST_SPEC)
        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
