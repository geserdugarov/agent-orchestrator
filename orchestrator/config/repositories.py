# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""REPOS parsing, validation, and default-spec construction.

Turns the ``REPOS`` environment value (entry tokenizing, owner/name and
option validation, duplicate-slug detection, per-repo parallel-limit
parsing) into the ``RepoSpec`` list threaded through the workflow, falling
back to the legacy single-repo ``REPO`` / ``TARGET_REPO_ROOT`` /
``BASE_BRANCH`` / ``REMOTE_NAME`` trio when ``REPOS`` is unset.

The abort-on-invalid and warn-to-stderr diagnostics live in
``orchestrator.config`` (its single configuration-failure funnel) and are
injected here as callables, so this module parses without importing config
back. The data types it produces (``RepoSpec``, ``RepoEnvEntry``) live in
``models``.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Callable, NoReturn

from orchestrator.config.models import RepoEnvEntry, RepoSpec

# Diagnostics injected from ``orchestrator.config`` keep configuration failure
# policy out of the parsing leaf: ``config_error`` aborts import and
# ``config_warning`` writes a non-fatal diagnostic to stderr.
ConfigError = Callable[[str], NoReturn]
ConfigWarning = Callable[[str], None]


def iter_repos_entries(raw_repos: str) -> Iterator[tuple[int, str]]:
    """Yield numbered, non-comment entries from a ``REPOS`` value."""
    for entry_no, raw_line in enumerate(
        raw_repos.replace(";", "\n").splitlines(), start=1,
    ):
        line = raw_line.strip()
        if line and not line.startswith("#"):
            yield entry_no, line


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
    # Remote validation precedes required-field validation to keep the
    # original abort ordering.
    if len(entry_parts) == 3:
        remote_name = "origin"
    else:
        remote_name = entry_parts[3]
        if not remote_name:
            config_error(
                f"orchestrator: REPOS entry #{entry_no} has empty "
                "remote_name (omit the trailing '|' to default to 'origin')",
            )
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
) -> RepoSpec:
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
    return RepoSpec(
        slug=entry.slug,
        target_root=target_path,
        base_branch=entry.base_branch,
        remote_name=entry.remote_name,
        parallel_limit=parallel_limit,
    )


def parse_repos_env(
    raw: str,
    *,
    default_parallel_limit: int,
    config_error: ConfigError,
    config_warning: ConfigWarning,
) -> list[RepoSpec]:
    """Parse the REPOS env value into a list of RepoSpecs.

    Format: one entry per line,
    ``owner/name|target_root|base_branch[|remote_name[|parallel_limit]]``.
    The fourth (``remote_name``, defaults to ``origin``) and fifth
    (``parallel_limit``, defaults to ``MAX_PARALLEL_ISSUES_PER_REPO`` via
    ``default_parallel_limit``) fields are optional. The fifth field is
    positional, so overriding ``parallel_limit`` requires also writing the
    ``remote_name`` (use ``origin`` explicitly to keep the default).
    Blank lines and lines starting with ``#`` are skipped. ``;`` is also
    accepted as an entry separator so the value fits on a single line in a
    ``.env`` file (the simple parser in `_load_dotenv` cannot represent
    multi-line values). Aborts (SystemExit) on malformed entries or
    duplicate slugs; a missing ``target_root`` is warned to stderr but not
    fatal so a freshly-cloned host can still start the orchestrator and
    notice the problem on the first tick rather than at import.
    """
    specs: list[RepoSpec] = []
    seen_slugs: set[str] = set()
    for entry_no, line in iter_repos_entries(raw):
        entry = parse_repo_entry(entry_no, line, config_error)
        # Duplicate-slug rejection precedes option parsing so a repeated repo
        # aborts before any per-entry option error on the duplicate row.
        if entry.slug in seen_slugs:
            config_error(
                f"orchestrator: REPOS lists duplicate slug {entry.slug!r}; "
                "each repo can appear only once",
            )
        seen_slugs.add(entry.slug)
        specs.append(
            build_repo_spec(
                entry,
                default_parallel_limit,
                config_error,
                config_warning,
            )
        )
    if not specs:
        config_error(
            "orchestrator: REPOS is set but contains no valid entries; "
            "either unset it or provide at least one "
            "'owner/name|target_root|base_branch' entry"
        )
    return specs


def build_repo_specs(
    repos_raw: str,
    *,
    default_spec: RepoSpec,
    config_error: ConfigError,
    config_warning: ConfigWarning,
) -> list[RepoSpec]:
    """Build the configured RepoSpec list from the REPOS env value.

    A single-element list holding ``default_spec`` (built from `REPO` /
    `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME` /
    `MAX_PARALLEL_ISSUES_PER_REPO`) when `REPOS` is unset, so existing
    single-repo deployments keep working unchanged; otherwise one element per
    `REPOS` entry. The per-entry ``parallel_limit`` default is
    ``default_spec.parallel_limit`` (i.e. `MAX_PARALLEL_ISSUES_PER_REPO`).
    """
    if not repos_raw.strip():
        return [default_spec]
    return parse_repos_env(
        repos_raw,
        default_parallel_limit=default_spec.parallel_limit,
        config_error=config_error,
        config_warning=config_warning,
    )
