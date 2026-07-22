# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for :mod:`orchestrator.git_plumbing`."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "contextlib",
        (("contextmanager", "contextmanager"),),
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
        "orchestrator._workflow_dependencies",
        (("config", "config"),),
    ),
    *export_group(
        "orchestrator._git_auth",
        (
            ("_GitAuthSession", "_GitAuthSession"),
            ("_failed_fetch", "_failed_fetch"),
            ("_git_auth_env", "_git_auth_env"),
            ("_git_auth_session", "_git_auth_session"),
            ("_resolved_git_token", "_resolved_git_token"),
            ("_target_root_lock", "_target_root_lock"),
        ),
    ),
    *export_group(
        "orchestrator._git_commands",
        (
            ("_git", "_git"),
            ("_git_hardened", "_git_hardened"),
            ("_unsafe_local_transport_config", "_unsafe_local_transport_config"),
        ),
    ),
    *export_group(
        "orchestrator._git_fetch",
        (
            ("_authed_fetch", "_authed_fetch"),
            ("_authed_target_fetch", "_authed_target_fetch"),
        ),
    ),
    *export_group(
        "orchestrator._git_plumbing_state",
        (
            ("_ASKPASS_MODE", "_ASKPASS_MODE"),
            ("_AUTHED_GIT_PREFIX", "_AUTHED_GIT_PREFIX"),
            ("_FETCH", "_FETCH"),
            ("_GIT", "_GIT"),
            ("_GIT_NO_PROMPT_ENV", "_GIT_NO_PROMPT_ENV"),
            ("_TARGET_ROOT_LOCKS", "_TARGET_ROOT_LOCKS"),
            ("_TARGET_ROOT_LOCKS_LOCK", "_TARGET_ROOT_LOCKS_LOCK"),
            ("_UNSAFE_TRANSPORT_CONFIG_RE", "_UNSAFE_TRANSPORT_CONFIG_RE"),
            ("log", "log"),
        ),
    ),
    *export_group(
        "orchestrator._git_push",
        (
            ("_push_branch", "_push_branch"),
            ("_push_with_auth", "_push_with_auth"),
            ("_remote_branch_sha", "_remote_branch_sha"),
        ),
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
        "subprocess",
        (("subprocess", None),),
    ),
    *export_group(
        "tempfile",
        (("tempfile", None),),
    ),
    *export_group(
        "threading",
        (("threading", None),),
    ),
    *export_group(
        "types",
        (("MappingProxyType", "MappingProxyType"),),
    ),
    *export_group(
        "typing",
        (
            ("Iterator", "Iterator"),
            ("Mapping", "Mapping"),
            ("Optional", "Optional"),
        ),
    ),
)
EXPORTED_NAMES = None
