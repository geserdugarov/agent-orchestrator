# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations


def _contains_log(messages: list[str], *fragments: str) -> bool:
    for message in messages:
        if all(fragment in message for fragment in fragments):
            return True
    return False
