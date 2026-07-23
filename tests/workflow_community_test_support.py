# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared values and builders for community-contribution sweeps."""
from __future__ import annotations

from tests.fakes import FakeLabel, FakePR, FakeUser
from tests.workflow_repo_values import _TEST_SPEC


OUTSIDER_LOGIN = "outsider"
ALLOWED_LOGIN = "geserdugarov"
ALLOWLIST_CONFIG = "ALLOWED_ISSUE_AUTHORS"
COMMENT_RETRY_PR_NUMBER = 11
TEST_SPEC = _TEST_SPEC


def make_pr(
    number: int,
    *,
    author: str,
    labels=(),
    user_type: str = "User",
) -> FakePR:
    return FakePR(
        number=number,
        user=FakeUser(author, type=user_type),
        labels=[FakeLabel(name) for name in labels],
    )


def fail_first_label_write(calls, original, pr, label) -> None:
    calls.append(pr.number)
    if pr.number == 1:
        raise RuntimeError("boom")
    original(pr, label)
