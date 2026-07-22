# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tokenization and required-field validation for ``REPOS`` entries."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable, NoReturn

ConfigError = Callable[[str], NoReturn]


@dataclass(frozen=True)
class RepoEnvEntry:
    """Required fields and raw options from one ``REPOS`` entry."""

    entry_no: int
    slug: str
    target_root: str
    base_branch: str
    remote_name: str
    parallel_limit_raw: str | None


def iter_repos_entries(raw_repos: str) -> Iterator[tuple[int, str]]:
    """Yield numbered, non-comment entries from a ``REPOS`` value."""
    for entry_no, raw_line in enumerate(
        raw_repos.replace(";", "\n").splitlines(), start=1,
    ):
        line = raw_line.strip()
        if line and not line.startswith("#"):
            yield entry_no, line


def _parse_remote_name(
    entry_no: int,
    entry_parts: tuple[str, ...],
    config_error: ConfigError,
) -> str:
    """Return the remote option, rejecting an explicitly empty value."""
    if len(entry_parts) == 3:
        return "origin"
    remote_name = entry_parts[3]
    if not remote_name:
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has empty "
            "remote_name (omit the trailing '|' to default to 'origin')",
        )
    return remote_name


def _validate_required_fields(
    entry_no: int,
    slug: str,
    target_root: str,
    base_branch: str,
    config_error: ConfigError,
) -> None:
    """Validate the required fields of one ``REPOS`` entry."""
    slug_components = slug.split("/")
    if len(slug_components) != 2 or not all(slug_components):
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has invalid "
            f"owner/name {slug!r}; expected exactly 'owner/name' "
            "with non-empty owner and name",
        )
    if not target_root:
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has empty target_root",
        )
    if not base_branch:
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has empty base_branch",
        )


def parse_repo_entry(
    entry_no: int,
    line: str,
    config_error: ConfigError,
) -> RepoEnvEntry:
    """Parse and validate the fields of one ``REPOS`` entry."""
    entry_parts = tuple(part.strip() for part in line.split("|"))
    if len(entry_parts) not in (3, 4, 5):
        config_error(
            f"orchestrator: REPOS entry #{entry_no} is malformed "
            "(expected 'owner/name|target_root|base_branch' "
            "with optional '|remote_name' and '|parallel_limit'): "
            f"{line!r}",
        )
    slug, target_root, base_branch = entry_parts[:3]
    remote_name = _parse_remote_name(entry_no, entry_parts, config_error)
    _validate_required_fields(
        entry_no,
        slug,
        target_root,
        base_branch,
        config_error,
    )
    return RepoEnvEntry(
        entry_no=entry_no,
        slug=slug,
        target_root=target_root,
        base_branch=base_branch,
        remote_name=remote_name,
        parallel_limit_raw=entry_parts[4] if len(entry_parts) == 5 else None,
    )
