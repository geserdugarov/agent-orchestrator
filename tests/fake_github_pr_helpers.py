# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stateless pull-request helpers exposed by the fake GitHub client."""
from __future__ import annotations

from typing import Optional

from tests.fake_models import FakePR


_STATE_CLOSED = "closed"
_STATE_OPEN = "open"


def _resolve_pr(owner_or_pr, pr: Optional[FakePR]) -> FakePR:
    return pr or owner_or_pr


def _pr_has_label(owner_or_pr, pr_or_label, label_name=None) -> bool:
    if label_name is None:
        pull_request = owner_or_pr
        resolved_label = pr_or_label
    else:
        pull_request = pr_or_label
        resolved_label = label_name
    wanted = (resolved_label or "").lower()
    return any(
        ((getattr(label, "name", "") or "").lower() == wanted)
        for label in (pull_request.labels or [])
    )


def _pr_state(owner_or_pr, pr: Optional[FakePR] = None) -> str:
    pull_request = _resolve_pr(owner_or_pr, pr)
    if pull_request.merged:
        return "merged"
    if pull_request.state == _STATE_CLOSED:
        return _STATE_CLOSED
    return _STATE_OPEN


def _pr_is_mergeable(
    owner_or_pr,
    pr: Optional[FakePR] = None,
) -> Optional[bool]:
    return _resolve_pr(owner_or_pr, pr).mergeable


def _pr_is_approved(
    owner_or_pr,
    pr: Optional[FakePR] = None,
    *,
    head_sha: str,
) -> bool:
    pull_request = _resolve_pr(owner_or_pr, pr)
    if not pull_request.approved:
        return False
    approved_sha = pull_request.approval_head_sha or pull_request.head.sha
    return approved_sha == head_sha


def _pr_has_changes_requested(
    owner_or_pr,
    pr: Optional[FakePR] = None,
    *,
    head_sha: str,
) -> bool:
    pull_request = _resolve_pr(owner_or_pr, pr)
    if not pull_request.changes_requested:
        return False
    requested_sha = (
        pull_request.changes_requested_head_sha
        or pull_request.head.sha
    )
    return requested_sha == head_sha


def _pr_combined_check_state(
    owner_or_pr,
    pr: Optional[FakePR] = None,
) -> str:
    return _resolve_pr(owner_or_pr, pr).check_state
