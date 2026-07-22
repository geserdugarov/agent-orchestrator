# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared issue-list filtering and PyGithub query options."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from github.Issue import Issue
from github.Label import Label


def iter_new_non_pr_issues(
    issues: Iterable[Issue],
    seen_numbers: set[int],
) -> Iterable[Issue]:
    """Yield unseen non-PR issues while updating the shared number set."""
    for issue in issues:
        if issue.pull_request is None and issue.number not in seen_numbers:
            seen_numbers.add(issue.number)
            yield issue


def issue_query_options(
    *,
    issue_state: str,
    since: Optional[datetime],
    label: Optional[Label] = None,
) -> dict[str, Any]:
    """Build common open/closed issue query options."""
    query_options: dict[str, Any] = {
        "state": issue_state,
        "sort": "updated",
        "direction": "desc",
    }
    if label is not None:
        query_options["labels"] = [label]
    if since is not None:
        query_options["since"] = since
    return query_options
