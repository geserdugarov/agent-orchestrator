# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed labels and the guard for the label-based workflow state machine.

The label vocabulary lives in ``_workflow_labels`` and the declared graph in
``_state_transitions``. This module remains the stable import surface used by
workflow code, tests, and external operator scripts.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from orchestrator import _state_transitions, _workflow_labels

log = logging.getLogger(__name__)
ALLOWED_TRANSITIONS = _state_transitions.ALLOWED_TRANSITIONS
_DETOUR_TO_RESOLVING = _state_transitions._DETOUR_TO_RESOLVING
ControlLabel = _workflow_labels.ControlLabel
WorkflowLabel = _workflow_labels.WorkflowLabel
_MISSING_LABEL = object()


class IllegalTransition(Exception):
    """A workflow-label write is absent from ``ALLOWED_TRANSITIONS``."""


def coerce_workflow_label(
    label_name: str | WorkflowLabel | object = _MISSING_LABEL,
    **legacy_fields: Any,
) -> WorkflowLabel:
    """Coerce a workflow label while accepting the historical ``value=``.

    ``label_name`` is the descriptive keyword for new callers. The adapter
    keeps existing keyword calls working and rejects duplicate or unknown
    arguments with ``TypeError`` before delegating to the typed label parser.
    """
    legacy_label = legacy_fields.pop("value", _MISSING_LABEL)
    if legacy_fields:
        unexpected_name = next(iter(legacy_fields))
        raise TypeError(
            "coerce_workflow_label() got an unexpected keyword argument "
            f"{unexpected_name!r}",
        )
    if label_name is not _MISSING_LABEL and legacy_label is not _MISSING_LABEL:
        raise TypeError(
            "coerce_workflow_label() got multiple values for the label",
        )
    selected_label = legacy_label if label_name is _MISSING_LABEL else label_name
    if selected_label is _MISSING_LABEL:
        raise TypeError(
            "coerce_workflow_label() missing required argument: 'label_name'",
        )
    return _workflow_labels.coerce_label_name(selected_label)


def is_allowed_transition(
    current: Optional[WorkflowLabel],
    new: WorkflowLabel,
) -> bool:
    """Return whether relabeling ``current`` to ``new`` is legal."""
    if current == new:
        return True
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def guard_transition(
    current: Optional[WorkflowLabel],
    new: WorkflowLabel,
    mode: str,
) -> None:
    """Warn or raise when a workflow-label write is illegal."""
    if mode == "off" or is_allowed_transition(current, new):
        return
    allowed = ", ".join(
        sorted(
            str(state)
            for state in ALLOWED_TRANSITIONS.get(current, frozenset())
        ),
    )
    current_label = None if current is None else str(current)
    allowed_text = allowed or "(none -- terminal state)"
    detail = (
        "illegal workflow transition "
        f"{current_label!r} -> {str(new)!r}; "
        f"allowed from there: {allowed_text}"
    )
    if mode == "enforce":
        raise IllegalTransition(detail)
    log.warning("%s", detail)
