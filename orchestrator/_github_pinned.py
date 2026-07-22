# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Authenticated pinned-state comment model and parser."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from github.IssueComment import IssueComment

log = logging.getLogger("orchestrator.github")

PINNED_STATE_MARKER = "<!--orchestrator-state"
PINNED_STATE_RE = re.compile(
    r"<!--orchestrator-state\s+(\{.*?\})\s*-->",
    re.DOTALL,
)
PINNED_STATE_BODY_RE = re.compile(
    r"\A\s*<!--orchestrator-state\s+(\{.*?\})\s*-->\s*\Z",
    re.DOTALL,
)
PINNED_STATE_TEMPLATE = "<!--orchestrator-state {payload}-->"
_MISSING_STATE = object()


@dataclass(init=False)
class PinnedState:
    """Pinned comment identity and mutable workflow state payload.

    ``state_data`` is the descriptive constructor keyword. The custom adapter
    retains the historical ``data=`` keyword and the ``.data`` instance
    attribute used throughout the workflow.
    """

    comment_id: Optional[int] = None
    state_data: dict = field(default_factory=dict)

    def __init__(
        self,
        comment_id: Optional[int] = None,
        state_data: Any = _MISSING_STATE,
        **legacy_fields: Any,
    ) -> None:
        legacy_state = legacy_fields.pop("data", _MISSING_STATE)
        if legacy_fields:
            unexpected_name = next(iter(legacy_fields))
            raise TypeError(
                "PinnedState() got an unexpected keyword argument "
                f"{unexpected_name!r}",
            )
        if state_data is not _MISSING_STATE and legacy_state is not _MISSING_STATE:
            raise TypeError("PinnedState() got multiple values for state data")
        selected_state = legacy_state if state_data is _MISSING_STATE else state_data
        if selected_state is _MISSING_STATE:
            selected_state = {}
        self.comment_id = comment_id
        self.state_data = selected_state

    def __getattr__(self, attribute_name: str) -> Any:
        if attribute_name == "data":
            return self.state_data
        raise AttributeError(attribute_name)

    def __setattr__(self, attribute_name: str, attribute_value: Any) -> None:
        target_name = "state_data" if attribute_name == "data" else attribute_name
        object.__setattr__(self, target_name, attribute_value)

    def get(self, key: str, default: Any = None) -> Any:
        """Return a workflow-state field or its default."""
        return self.state_data.get(key, default)

    def set(self, key: str, state_value: Any) -> None:
        """Set one workflow-state field."""
        self.state_data[key] = state_value


def pinned_state_from_comment(
    issue_comment: IssueComment,
    *,
    trusted_login: Optional[str],
    issue_number: int,
) -> Optional[PinnedState]:
    """Parse one authenticated, state-only pinned comment candidate."""
    body = issue_comment.body or ""
    if PINNED_STATE_MARKER not in body:
        return None
    author_login = getattr(
        getattr(issue_comment, "user", None),
        "login",
        None,
    )
    if trusted_login is not None and author_login != trusted_login:
        return None
    state_match = PINNED_STATE_BODY_RE.match(body)
    if state_match is None:
        return None
    try:
        parsed_state = json.loads(state_match.group(1))
    except json.JSONDecodeError:
        log.warning("issue=#%s pinned state JSON unparseable", issue_number)
        parsed_state = {}
    return PinnedState(
        comment_id=issue_comment.id,
        state_data=parsed_state,
    )
