# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Callable agent fixture for usage retry scenarios."""
from __future__ import annotations

from typing import Optional

from tests.workflow_helpers import _agent


class _PoisonedThenFreshRun:
    def __init__(self) -> None:
        self.calls: list[Optional[str]] = []

    def __call__(self, *_args, resume_session_id=None, **_kwargs):
        self.calls.append(resume_session_id)
        if resume_session_id == "poisoned-sess":
            return _agent(
                session_id="",
                last_message="",
                stderr=(
                    "Error: No conversation found with session ID: x"
                ),
            )
        return _agent(session_id="fresh-sess", last_message="ok")
