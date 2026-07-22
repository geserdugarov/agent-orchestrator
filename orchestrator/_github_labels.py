# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""GitHub label vocabulary, bootstrap specifications, and predicates."""
from __future__ import annotations

from typing import Optional

from github.Issue import Issue

from orchestrator._static_alias import StaticMethodAlias
from orchestrator.state_machine import ControlLabel, WorkflowLabel

WORKFLOW_LABEL_SPECS: tuple[tuple[WorkflowLabel, str, str], ...] = (
    (WorkflowLabel.DECOMPOSING, "fbca04", "Orchestrator is breaking this issue into sub-issues"),
    (WorkflowLabel.READY, "0e8a16", "Decomposed and ready for implementation"),
    (WorkflowLabel.BLOCKED, "b60205", "Blocked on another issue"),
    (WorkflowLabel.UMBRELLA, "ededed", "Parent of child issues with no implementation of its own"),
    (WorkflowLabel.IMPLEMENTING, "1d76db", "A coding agent is working on this"),
    (WorkflowLabel.VALIDATING, "8a2be2", "Reviewer agent is checking the diff; verify gate runs on approval"),
    (
        WorkflowLabel.DOCUMENTING,
        "c2e0c6",
        "Documentation pass after reviewer approval (final-docs hop), before in_review",
    ),
    (WorkflowLabel.IN_REVIEW, "d93f0b", "PR is open, awaiting human review"),
    (
        WorkflowLabel.FIXING,
        "fef2c0",
        "Dev fix-loop addressing reviewer changes or in_review PR feedback before re-validation",
    ),
    (
        WorkflowLabel.RESOLVING_CONFLICT,
        "e99695",
        "Resolving an actual rebase conflict (clean rebases route straight to validating)",
    ),
    (WorkflowLabel.QUESTION, "d876e3", "Awaiting a clarifying answer from a human before the orchestrator can advance"),
    (WorkflowLabel.DONE, "cccccc", "Merged to main"),
    (WorkflowLabel.REJECTED, "5c0000", "Issue rejected / closed without merge"),
)
assert {spec[0] for spec in WORKFLOW_LABEL_SPECS} == set(WorkflowLabel)
WORKFLOW_LABELS = frozenset(WorkflowLabel)

BACKLOG_LABEL = ControlLabel.BACKLOG
PAUSED_LABEL = ControlLabel.PAUSED
COMMUNITY_CONTRIBUTION_LABEL = ControlLabel.COMMUNITY_CONTRIBUTION
CONTROL_LABEL_SPECS: tuple[tuple[ControlLabel, str, str], ...] = (
    (
        BACKLOG_LABEL,
        "c5def5",
        "Skip orchestrator processing entirely until the label is removed",
    ),
    (
        PAUSED_LABEL,
        "d4c5f9",
        "Pause an in-flight issue: skip orchestrator processing entirely until the label is removed",
    ),
    (
        COMMUNITY_CONTRIBUTION_LABEL,
        "7057ff",
        "PR opened by an author outside ALLOWED_ISSUE_AUTHORS; human review requested",
    ),
)
HARD_SKIP_CONTROL_LABELS: tuple[ControlLabel, ...] = (
    BACKLOG_LABEL,
    PAUSED_LABEL,
)


def issue_has_label(issue: Issue, label_name: str) -> bool:
    """Return whether an issue has a case-insensitive label name."""
    wanted_label = (label_name or "").lower()
    return any(
        ((getattr(label, "name", "") or "").lower() == wanted_label)
        for label in (issue.labels or [])
    )


def hard_skip_control_label(issue: Issue) -> Optional[str]:
    """Return the first control label that suppresses issue processing."""
    for control_label in HARD_SKIP_CONTROL_LABELS:
        if issue_has_label(issue, control_label):
            return control_label
    return None


def workflow_label(issue: Issue) -> Optional[WorkflowLabel]:
    """Return an issue's workflow label, excluding control labels."""
    for issue_label in issue.labels:
        if issue_label.name in WORKFLOW_LABELS:
            return WorkflowLabel(issue_label.name)
    return None


WORKFLOW_LABEL_METHOD = StaticMethodAlias(workflow_label)
