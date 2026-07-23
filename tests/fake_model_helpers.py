# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stateless helpers shared by the in-memory GitHub models."""
from __future__ import annotations

from typing import Any


_CLOSED_SWEEP_LABELS = frozenset((
    "implementing",
    "documenting",
    "validating",
    "in_review",
    "fixing",
    "resolving_conflict",
    "question",
))


def _copy_issue_comments(issue: Any) -> list[Any]:
    return list(issue.comments)


def _has_closed_sweep_label(issue: Any) -> bool:
    return any(label.name in _CLOSED_SWEEP_LABELS for label in issue.labels)


def _review_has_feedback(review: Any) -> bool:
    return (
        (review.state or "").upper() in {"CHANGES_REQUESTED", "COMMENTED"}
        and bool((review.body or "").strip())
    )
