# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The ways a split transaction can be interrupted, and the refusals it meets.

A death and a failure are different subjects and are kept apart here. A
`BaseException` no `except Exception` catches is what the durable ordering
exists for -- a tick that does not live long enough to see anything fail --
while a refusal is a step that reported back, which the transaction handles by
parking with the recorded verdict standing.

The interceptors are classes rather than closures because each has to run the
real call before or instead of its own behavior, and a closure over the
original would be one more thing to read at every call site.
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

from tests.support.fakes import FakeGitHubClient
from tests.workflow.stages.decomposition.late_test_support import (
    LATE_ISSUE_NUMBER,
)

KEY_CHILDREN = "children"


class _RunAndDie:
    """One effect that lands, followed by a process that does not return."""

    def __init__(self, ran, name: str) -> None:
        self._ran = ran
        self._name = name

    def __call__(self, *call_args, **call_options):
        self._ran(*call_args, **call_options)
        raise KeyboardInterrupt(self._name)


class _ChildWrites:
    """Intercept the pinned writes a split makes, parent and children apart.

    Aimed at the child rather than at the client, because the parent's own
    writes are what put a child in the register: refusing those too would
    describe a tick that never created anything.
    """

    def __init__(self, client, *, widths=None, refuse: bool = False) -> None:
        self._wrote = client.write_pinned_state
        self._widths = widths
        self._refuse = refuse

    def __call__(self, issue, state):
        if issue.number != LATE_ISSUE_NUMBER:
            if self._refuse:
                raise RuntimeError("child state refused")
        elif self._widths is not None:
            self._widths.append(list(state.get(KEY_CHILDREN) or []))
        return self._wrote(issue, state)


@contextlib.contextmanager
def killed_after(owner, name: str):
    """The process dying immediately after one effect has landed."""
    with patch.object(owner, name, _RunAndDie(getattr(owner, name), name)):
        yield


@contextlib.contextmanager
def killed_before(owner, name: str):
    """The process dying before one effect is attempted at all."""
    with patch.object(owner, name, side_effect=KeyboardInterrupt(name)):
        yield


@contextlib.contextmanager
def refusing(client: FakeGitHubClient, method: str):
    """GitHub refusing one call the transaction makes."""
    with patch.object(client, method, side_effect=RuntimeError):
        yield


@contextlib.contextmanager
def refusing_child_writes(client: FakeGitHubClient):
    """GitHub refusing the seed write, and only it."""
    with patch.object(
        client, "write_pinned_state", _ChildWrites(client, refuse=True),
    ):
        yield


@contextlib.contextmanager
def recording_children(client: FakeGitHubClient, widths: list):
    """Record the parent's child list at every write it makes."""
    with patch.object(
        client, "write_pinned_state", _ChildWrites(client, widths=widths),
    ):
        yield
