# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Descriptor for exposing module functions unchanged on compatibility classes."""
from __future__ import annotations

from typing import Any, Callable


class StaticMethodAlias:
    """Return one module function unchanged from class or instance access."""

    def __init__(self, function: Callable[..., Any]) -> None:
        self._function = function

    def __get__(
        self,
        instance: object,
        owner: type | None = None,
    ) -> Callable[..., Any]:
        return self._function
