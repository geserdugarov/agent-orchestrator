# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting stage handler (stub).

Registers the `documenting` workflow label as a routable stage that sits
between `implementing` and `validating` so the dispatcher recognises it
instead of falling through to `_handle_pickup`. Real documentation
behaviour is added under parent #149; until then the implementing stage
does not auto-apply this label, so any issue carrying it arrived via a
manual operator action.

The handler intentionally PARKS rather than logging-and-returning: a
silent stub would let an operator who applied the label by hand sit
forever waiting for the orchestrator to advance the issue, and a future
documenting stage that silently skips would be indistinguishable from a
bug. Parking surfaces the situation to the HITL handles immediately so
the operator either removes the label or waits for the real handler.

Open `documenting` issues touch only their own pinned state and worktree,
so the label is deliberately NOT listed in `workflow._FAMILY_AWARE_LABELS`
and `tick()` routes it through the fan-out bucket.
"""
from __future__ import annotations

from github.Issue import Issue

from .. import config
from ..config import RepoSpec
from ..github import GitHubClient


def _handle_documenting(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    if state.get("awaiting_human"):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `documenting` applied manually but the "
        "documenting stage handler is not implemented yet (parent #149). "
        "Remove the label to resume the normal workflow, or wait for the "
        "real handler to land.",
        reason="documenting_stub_manual",
    )
    gh.write_pinned_state(issue, state)
