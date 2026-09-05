# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pinned-state keys, park reasons, and detour labels for one rebase attempt.

These values are a public contract in two directions: the string keys and park
reasons are already written into pinned-state comments on live issues, and the
logger name is what operator log filters select on. Both are spelled out
literally here rather than derived from the module path so that moving this
owner cannot rename either one.
"""
from __future__ import annotations

import logging

from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger('orchestrator.base_sync')

_PR_REFRESH_DETOUR_LABELS = frozenset(
    (
        WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING,
        WorkflowLabel.IN_REVIEW, WorkflowLabel.FIXING,
    ),
)

_PARK_REASON = "park_reason"

_AWAITING_HUMAN = "awaiting_human"

_REVIEW_ROUND = "review_round"

_CONFLICT_ROUND = "conflict_round"

_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"

# What the attempt produced and what it produced it against, recorded once git
# has made the replay and before the gate is entered. The anchor beside them
# says which head the push is leased against and brings an interrupted attempt
# back; these say which local commit that attempt made and which publication
# it made it for, and neither is a thing the anchor can prove.
#
# The head is what stops a checkout nothing here made being force-pushed over
# the candidate: a rebase REPLAYS the branch, so the divergence a replay
# leaves is the same shape a rebuilt worktree, an operator's reset, and a
# branch pointed at other work leave, and all of them satisfy the same lease.
#
# The publication is what keeps the permit's own checks honest on the tick
# after a crash. Re-derived evidence that took the pull request and the stage
# from whatever the issue says NOW would compare today against today, and a
# relabel or a repoint made while the process was down would be adopted as
# though the dead tick had made its rewrite under it.
_PENDING_REWRITE_SHA = "pending_auto_base_rebase_rewrite_sha"

_PENDING_REWRITE_PR = "pending_auto_base_rebase_rewrite_pr"

_PENDING_REWRITE_STAGE = "pending_auto_base_rebase_rewrite_stage"

# The head a finish has already said it published. Written after the notice
# and the audit event and before the relabel, which is the one window a finish
# cannot otherwise be recovered across: everything it announces goes out
# before the pinned write that clears the record, so a process lost between
# them comes back to an attempt that looks unfinished and announces itself a
# second time -- a second `base_rebased` on the stream and a second notice on
# the pull request, for one publication that happened once.
_PENDING_ANNOUNCED_SHA = "pending_auto_base_rebase_announced_sha"

_REASON_AUTO_BASE_REBASE_FAILED = "auto_base_rebase_failed"

_REASON_AUTO_BASE_REBASE_PUSH_FAILED = "auto_base_rebase_push_failed"

_ERROR_SNIPPET_LEN = 120

_AUTO_REBASE_PARK_REASONS = frozenset(
    (
        _REASON_AUTO_BASE_REBASE_FAILED,
        "auto_base_rebase_dirty",
        _REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    ),
)
