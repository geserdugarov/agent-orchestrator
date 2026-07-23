# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tests.fakes import FakeGitHubClient

RUN_AGENT = "run_agent"


def _agent_prompt(mocks) -> str:
    agent_call = mocks[RUN_AGENT].call_args
    return agent_call.kwargs.get("prompt") or agent_call.args[1]


def _issue_comment_text(
    github: FakeGitHubClient,
    issue_number: int | None = None,
) -> str:
    return "\n".join(
        body
        for posted_issue_number, body in github.posted_comments
        if issue_number is None or posted_issue_number == issue_number
    )


def _pr_comment_text(github: FakeGitHubClient) -> str:
    return "\n".join(
        body
        for _pull_request_number, body in github.posted_pr_comments
    )


def _lifecycle_events(
    github: FakeGitHubClient,
    stage: str,
) -> list[dict]:
    return [
        event
        for event in github.recorded_events
        if event["event"] in ("agent_spawn", "agent_exit")
        and event.get("stage") == stage
    ]
