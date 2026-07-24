# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.verify`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "contextlib",
        (("suppress", "suppress"),),
    ),
    *export_group(
        "dataclasses",
        (("dataclass", "dataclass"),),
    ),
    *export_group(
        "logging",
        (("logging", None),),
    ),
    *export_group(
        "orchestrator._verify_models",
        (
            ("VerifyResult", "VerifyResult"),
            ("_head_sha", "_head_sha"),
            ("_worktree_dirty_files", "_worktree_dirty_files"),
        ),
    ),
    *export_group(
        "orchestrator._verify_process",
        (
            ("_combine_output", "_combine_output"),
            ("_completed_verify_result", "_completed_verify_result"),
            ("_drain_verify_output", "_drain_verify_output"),
            ("_kill_verify_group", "_kill_verify_group"),
            ("_spawn_verify_command", "_spawn_verify_command"),
            ("_timeout_verify_result", "_timeout_verify_result"),
        ),
    ),
    *export_group(
        "orchestrator._verify_runner",
        (
            ("_run_verify_command", "_run_verify_command"),
            ("_run_verify_commands", "_run_verify_commands"),
            ("_truncate_verify_output", "_truncate_verify_output"),
        ),
    ),
    *export_group(
        "orchestrator._verify_state",
        (
            ("_VERIFY_OUTPUT_BUDGET", "_VERIFY_OUTPUT_BUDGET"),
            ("log", "log"),
        ),
    ),
    *export_group(
        "orchestrator.agents.environment",
        (("_filter_agent_env", "filter_agent_env"),),
    ),
    *export_group(
        "orchestrator.git_plumbing",
        (
            ("_git", "_git"),
            ("_git_hardened", "_git_hardened"),
        ),
    ),
    *export_group(
        "orchestrator.workflow_messages",
        (("_redact_secrets", "_redact_secrets"),),
    ),
    *export_group(
        "os",
        (("os", None),),
    ),
    *export_group(
        "pathlib",
        (("Path", "Path"),),
    ),
    *export_group(
        "signal",
        (("signal", None),),
    ),
    *export_group(
        "subprocess",
        (("subprocess", None),),
    ),
    *export_group(
        "typing",
        (("Optional", "Optional"),),
    ),
)
EXPORTED_NAMES = None
