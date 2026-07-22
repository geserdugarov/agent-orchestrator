# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Option validation and model construction for parsed ``REPOS`` entries."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, NoReturn, TypeVar

from orchestrator._repo_config_entry import RepoEnvEntry

ConfigError = Callable[[str], NoReturn]
ConfigWarning = Callable[[str], None]
RepoSpecType = TypeVar("RepoSpecType")


def record_repo_slug(
    entry: RepoEnvEntry,
    seen_slugs: set[str],
    config_error: ConfigError,
) -> None:
    """Reject duplicate repository slugs and record a unique one."""
    if entry.slug in seen_slugs:
        config_error(
            f"orchestrator: REPOS lists duplicate slug {entry.slug!r}; "
            "each repo can appear only once",
        )
    seen_slugs.add(entry.slug)


def _parse_parallel_limit(
    entry: RepoEnvEntry,
    default_parallel_limit: int,
    config_error: ConfigError,
) -> int:
    """Validate one entry's optional parallel limit."""
    if entry.parallel_limit_raw is None:
        return default_parallel_limit
    if not entry.parallel_limit_raw:
        config_error(
            f"orchestrator: REPOS entry #{entry.entry_no} has empty "
            "parallel_limit (omit the trailing '|' to default to "
            f"MAX_PARALLEL_ISSUES_PER_REPO={default_parallel_limit})",
        )
    try:
        parallel_limit = int(entry.parallel_limit_raw)
    except ValueError:
        config_error(
            f"orchestrator: REPOS entry #{entry.entry_no} parallel_limit "
            f"{entry.parallel_limit_raw!r} is not a valid integer; expected "
            "a positive integer (>= 1)",
        )
    if parallel_limit < 1:
        config_error(
            f"orchestrator: REPOS entry #{entry.entry_no} parallel_limit "
            f"{entry.parallel_limit_raw!r} must be >= 1 (zero or negative "
            "would block all work for this repo)",
        )
    return parallel_limit


def build_repo_spec(
    entry: RepoEnvEntry,
    default_parallel_limit: int,
    config_error: ConfigError,
    config_warning: ConfigWarning,
    spec_factory: Callable[..., RepoSpecType],
) -> RepoSpecType:
    """Validate entry options and construct a repository specification."""
    parallel_limit = _parse_parallel_limit(
        entry,
        default_parallel_limit,
        config_error,
    )
    target_path = Path(entry.target_root)
    if not target_path.exists():
        config_warning(
            f"orchestrator: REPOS entry {entry.slug!r} target_root "
            f"{target_path} does not exist; worktree creation will fail",
        )
    return spec_factory(
        slug=entry.slug,
        target_root=target_path,
        base_branch=entry.base_branch,
        remote_name=entry.remote_name,
        parallel_limit=parallel_limit,
    )
