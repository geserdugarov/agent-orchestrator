# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Small observable projections for trajectory parser tests."""


def claude_summary(trajectory) -> tuple:
    return (
        trajectory.backend,
        trajectory.system_prompt,
        trajectory.tools,
        trajectory.final_output,
        (trajectory.skills.available, trajectory.skills.triggered),
    )
