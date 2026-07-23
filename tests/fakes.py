# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable import surface for the in-memory GitHub test doubles."""
from __future__ import annotations

from tests import fake_github_client as _client
from tests import fake_models as _models
from tests.fake_github_state import _IssueSeed


FakeGitHubClient = _client.FakeGitHubClient
FakeComment = _models.FakeComment
FakeIssue = _models.FakeIssue
FakeLabel = _models.FakeLabel
FakePR = _models.FakePR
FakePRRef = _models.FakePRRef
FakePRReview = _models.FakePRReview
FakeUser = _models.FakeUser


def make_issue(number: int, **issue_fields) -> FakeIssue:
    """Build an issue while preserving the historical keyword surface."""
    seed = _IssueSeed(**issue_fields)
    labels = [FakeLabel(seed.label)] if seed.label else []
    return FakeIssue(
        number=number,
        title=seed.title,
        body=seed.body,
        labels=labels,
        comments=list(seed.comments),
        user=FakeUser(seed.author),
    )
