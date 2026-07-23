# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tests.fakes import FakeComment, FakeGitHubClient, FakeIssue, make_issue

LABEL_BLOCKED = "blocked"


@dataclass(frozen=True)
class DecomposerResumeCase:
    issue_number: int
    comments: tuple[FakeComment, ...]
    label: str
    last_action_comment_id: int
    backend: str
    session_id: str


def _comments_for_issue(
    github: FakeGitHubClient,
    issue_number: int,
) -> list[str]:
    return [body for posted_issue_number, body in github.posted_comments if posted_issue_number == issue_number]


def _labels_for_issue(
    github: FakeGitHubClient,
    issue_number: int,
) -> list[str]:
    return [label for labeled_issue_number, label in github.label_history if labeled_issue_number == issue_number]


def _comment_with_marker(
    github: FakeGitHubClient,
    issue_number: int,
    marker: str,
) -> str:
    return next(body for body in _comments_for_issue(github, issue_number) if marker in body)


def _run_with_logs(
    case,
    logger_name: str,
    level: str,
    action: Callable[[], None],
) -> list[str]:
    with case.assertLogs(logger_name, level=level) as capture:
        action()
        return capture.output


def _seed_blocked_children(
    github: FakeGitHubClient,
    parent_number: int,
    child_numbers: Iterable[int],
) -> None:
    for child_number in child_numbers:
        child = make_issue(child_number, label=LABEL_BLOCKED)
        github.add_issue(child)
        github.seed_state(
            child_number,
            parent_number=parent_number,
            created_at="2026-05-03T00:00:00+00:00",
        )


def _seed_decomposer_resume(
    resume_case: DecomposerResumeCase,
) -> tuple[FakeGitHubClient, FakeIssue]:
    github = FakeGitHubClient()
    issue = make_issue(resume_case.issue_number, label=resume_case.label)
    issue.comments.extend(resume_case.comments)
    github.add_issue(issue)
    github.seed_state(
        resume_case.issue_number,
        awaiting_human=True,
        last_action_comment_id=resume_case.last_action_comment_id,
        decomposer_agent=resume_case.backend,
        decomposer_session_id=resume_case.session_id,
    )
    return github, issue
