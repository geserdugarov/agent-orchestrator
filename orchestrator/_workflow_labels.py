# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow and control label vocabulary."""
from __future__ import annotations

from enum import StrEnum


class WorkflowLabel(StrEnum):
    """Workflow states whose values are the GitHub label strings."""

    DECOMPOSING = "decomposing"
    READY = "ready"
    BLOCKED = "blocked"
    UMBRELLA = "umbrella"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    DOCUMENTING = "documenting"
    IN_REVIEW = "in_review"
    FIXING = "fixing"
    RESOLVING_CONFLICT = "resolving_conflict"
    QUESTION = "question"
    DONE = "done"
    REJECTED = "rejected"


class ControlLabel(StrEnum):
    """Operator modifiers that coexist with a workflow state.

    These values gate or redirect processing while leaving the underlying
    ``WorkflowLabel`` intact. They never enter the workflow transition table.
    """

    BACKLOG = "backlog"
    PAUSED = "paused"
    COMMUNITY_CONTRIBUTION = "community_contribution"


def coerce_label_name(label_name: str | WorkflowLabel) -> WorkflowLabel:
    """Return the workflow member for a wire label or raise ``ValueError``."""
    try:
        return WorkflowLabel(label_name)
    except ValueError:
        valid_labels = ", ".join(
            repr(str(member)) for member in WorkflowLabel
        )
        raise ValueError(
            f"{label_name!r} is not a valid workflow label; "
            f"expected one of: {valid_labels}",
        ) from None
