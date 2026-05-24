# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question stage handler (stub).

Registers the `question` workflow label as a routable stage so an operator
applying it does not fall through to `_handle_pickup` or
`_handle_implementing`. The body is intentionally a no-op for now: the
follow-up issue under parent #141 fills in real behavior (HITL prompt
surfacing, resume-on-answer). Until then the handler logs presence and
returns without touching pinned state, spawning agents, or relabeling.

Open `question` issues are own-state / fan-out work -- they do not read or
write any other issue's pinned state -- so the label is deliberately NOT
listed in `workflow._FAMILY_AWARE_LABELS` and `tick()` routes it through
the fan-out bucket.
"""
from __future__ import annotations

from github.Issue import Issue

from ..config import RepoSpec
from ..github import GitHubClient


def _handle_question(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    _wf.log.info(
        "repo=%s issue=#%s question stage stub; leaving alone",
        spec.slug, issue.number,
    )
