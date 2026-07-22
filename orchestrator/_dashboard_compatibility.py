# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Small helpers shared by stable dashboard compatibility surfaces."""
from __future__ import annotations

from typing import Any, Iterable


def preserve_defining_module(
    module_name: str,
    exported_members: Iterable[Any],
) -> None:
    """Keep historical ``__module__`` metadata on moved callables."""
    for exported_member in exported_members:
        exported_member.__module__ = module_name
