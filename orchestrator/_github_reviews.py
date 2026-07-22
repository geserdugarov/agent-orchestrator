# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Current-head review aggregation and actionable feedback filtering."""
from __future__ import annotations

from typing import Any, Optional

from github.PullRequest import PullRequest

from orchestrator._static_alias import StaticMethodAlias

_REVIEW_CHANGES_REQUESTED = "CHANGES_REQUESTED"
_ReviewStateForHead = tuple[str, tuple[int, str]]


def _review_state_for_head(
    review: Any,
    head_sha: str,
) -> Optional[_ReviewStateForHead]:
    if (getattr(review, "commit_id", "") or "") != head_sha:
        return None
    review_state = (review.state or "").upper()
    if review_state not in (
        "APPROVED",
        _REVIEW_CHANGES_REQUESTED,
        "DISMISSED",
    ):
        return None
    reviewer_login = review.user.login if review.user else ""
    if not reviewer_login:
        return None
    review_id = getattr(review, "id", 0) or 0
    return reviewer_login, (review_id, review_state)


def _record_latest_review(
    latest_per_user: dict[str, tuple[int, str]],
    candidate: tuple[str, tuple[int, str]],
) -> None:
    reviewer_login, review_record = candidate
    previous_review = latest_per_user.get(reviewer_login)
    if previous_review is None or review_record[0] > previous_review[0]:
        latest_per_user[reviewer_login] = review_record


def latest_review_states_for_head(
    pr: PullRequest,
    *,
    head_sha: str,
) -> list[str]:
    """Return each reviewer's latest state on the current PR head."""
    if not head_sha:
        return []
    latest_per_user: dict[str, tuple[int, str]] = {}
    for review in pr.get_reviews():
        candidate = _review_state_for_head(review, head_sha)
        if candidate is not None:
            _record_latest_review(latest_per_user, candidate)
    return [
        review_state
        for _, review_state in latest_per_user.values()
    ]


def is_actionable_review_summary(
    review: Any,
    after_id: Optional[int],
) -> bool:
    """Return whether a review summary carries unread developer feedback."""
    review_state = (review.state or "").upper()
    if review_state not in (_REVIEW_CHANGES_REQUESTED, "COMMENTED"):
        return False
    if not (review.body or "").strip():
        return False
    return after_id is None or review.id > after_id


LATEST_REVIEW_STATES_METHOD = StaticMethodAlias(latest_review_states_for_head)
