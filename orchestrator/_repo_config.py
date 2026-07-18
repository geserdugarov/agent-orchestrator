# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Repository-entry model and REPOS parsing / default-spec construction.

This is the private home of the repository-configuration surface split out of
``orchestrator.config``: the ``RepoSpec`` per-repo identity dataclass, the
``REPOS`` environment parser (entry tokenizing, owner/name and option
validation, duplicate-slug detection, per-repo parallel-limit parsing), and the
default-spec construction that falls back to the legacy single-repo
``REPO`` / ``TARGET_REPO_ROOT`` / ``BASE_BRANCH`` / ``REMOTE_NAME`` trio when
``REPOS`` is unset.

``orchestrator.config`` re-exports ``RepoSpec`` and wraps ``parse_repos_env`` /
``build_repo_specs`` behind ``config._parse_repos_env`` / ``config._REPO_SPECS``
/ ``config.default_repo_specs`` so every existing caller and test patch target
keeps importing from the same site. The abort-on-invalid and warn-to-stderr
diagnostics live in ``config`` (its single configuration-failure funnel) and are
injected here as callables, so ``config`` keeps importing nothing from
``orchestrator`` and this parser stays a stdlib-only leaf testable in isolation.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

# Diagnostics injected from ``orchestrator.config`` so this module stays a
# stdlib-only leaf: ``config_error`` aborts import (SystemExit with the message)
# and ``config_warning`` writes a non-fatal diagnostic to stderr.
ConfigError = Callable[[str], NoReturn]
ConfigWarning = Callable[[str], None]


@dataclass(frozen=True)
class RepoSpec:
    """Per-repo identity threaded through the workflow.

    Replaces the global `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` reads
    inside workflow.py so a future multi-repo loop can drive several repos
    from one orchestrator process without touching module-level state.

    `remote_name` is the name of the git remote in `target_root` that points
    at this repo on GitHub. Defaults to `origin`; override when the local
    clone uses several remotes (e.g. a public `origin` and a private fork
    under a different remote name) and the orchestrator should drive the
    non-default one.

    `parallel_limit` caps how many issues this repo may advance in parallel
    on a single tick. Defaults to 1 (legacy one-at-a-time behavior); each
    `REPOS` entry can override it via the optional fifth pipe-separated
    field. The global `MAX_PARALLEL_ISSUES_GLOBAL` ceiling applies across
    all repos to cap-counted handlers regardless of any one repo's
    `parallel_limit`; no-agent family buckets (`blocked` / `umbrella`) are
    cap-exempt by design (a parent dep-graph walk must always get its turn)
    and are excluded from both `parallel_limit` and
    `MAX_PARALLEL_ISSUES_GLOBAL`.
    """

    slug: str
    target_root: Path
    base_branch: str
    remote_name: str = "origin"
    parallel_limit: int = 1


@dataclass(frozen=True)
class _RepoEnvEntry:
    """Required fields and raw options from one REPOS entry."""

    entry_no: int
    slug: str
    target_root: str
    base_branch: str
    remote_name: str
    parallel_limit_raw: str | None


def _iter_repos_entries(raw: str) -> Iterator[tuple[int, str]]:
    """Yield numbered, non-comment entries from a REPOS value."""
    # ';' accepted in addition to '\n' so the value can be one line in .env.
    for entry_no, raw_line in enumerate(
        raw.replace(";", "\n").splitlines(), start=1
    ):
        line = raw_line.strip()
        if line and not line.startswith("#"):
            yield entry_no, line


def _parse_repo_remote_name(
    entry_no: int, parts: tuple[str, ...], config_error: ConfigError
) -> str:
    """Return the remote option, rejecting an explicitly empty value."""
    if len(parts) == 3:
        return "origin"
    remote_name = parts[3]
    if not remote_name:
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has empty "
            "remote_name (omit the trailing '|' to default to 'origin')"
        )
    return remote_name


def _validate_repo_required_fields(
    entry_no: int,
    slug: str,
    target_root: str,
    base_branch: str,
    config_error: ConfigError,
) -> None:
    """Validate the required fields of one REPOS entry."""
    # Require exactly two non-empty components separated by a single '/'.
    # A substring check also accepts empty or extra path components.
    slug_components = slug.split("/")
    if len(slug_components) != 2 or not all(slug_components):
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has invalid "
            f"owner/name {slug!r}; expected exactly 'owner/name' "
            "with non-empty owner and name"
        )
    if not target_root:
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has empty target_root"
        )
    if not base_branch:
        config_error(
            f"orchestrator: REPOS entry #{entry_no} has empty base_branch"
        )


def _parse_repo_entry(
    entry_no: int, line: str, config_error: ConfigError
) -> _RepoEnvEntry:
    """Parse and validate the fields of one REPOS entry."""
    parts = tuple(part.strip() for part in line.split("|"))
    if len(parts) not in (3, 4, 5):
        config_error(
            f"orchestrator: REPOS entry #{entry_no} is malformed "
            f"(expected 'owner/name|target_root|base_branch' "
            f"with optional '|remote_name' and '|parallel_limit'): "
            f"{line!r}"
        )
    slug, target_root, base_branch = parts[:3]
    remote_name = _parse_repo_remote_name(entry_no, parts, config_error)
    _validate_repo_required_fields(
        entry_no,
        slug,
        target_root,
        base_branch,
        config_error,
    )
    return _RepoEnvEntry(
        entry_no=entry_no,
        slug=slug,
        target_root=target_root,
        base_branch=base_branch,
        remote_name=remote_name,
        parallel_limit_raw=parts[4] if len(parts) == 5 else None,
    )


def _record_repo_slug(
    entry: _RepoEnvEntry, seen: set[str], config_error: ConfigError
) -> None:
    """Reject duplicate repository slugs and record a unique one."""
    if entry.slug in seen:
        config_error(
            f"orchestrator: REPOS lists duplicate slug {entry.slug!r}; "
            "each repo can appear only once"
        )
    seen.add(entry.slug)


def _parse_repo_parallel_limit(
    entry: _RepoEnvEntry, default_parallel_limit: int, config_error: ConfigError
) -> int:
    """Validate one entry's optional parallel limit."""
    if entry.parallel_limit_raw is None:
        return default_parallel_limit
    if not entry.parallel_limit_raw:
        config_error(
            f"orchestrator: REPOS entry #{entry.entry_no} has empty "
            "parallel_limit (omit the trailing '|' to default to "
            f"MAX_PARALLEL_ISSUES_PER_REPO={default_parallel_limit})"
        )
    try:
        parallel_limit = int(entry.parallel_limit_raw)
    except ValueError:
        config_error(
            f"orchestrator: REPOS entry #{entry.entry_no} parallel_limit "
            f"{entry.parallel_limit_raw!r} is not a valid integer; expected "
            "a positive integer (>= 1)"
        )
    if parallel_limit < 1:
        config_error(
            f"orchestrator: REPOS entry #{entry.entry_no} parallel_limit "
            f"{entry.parallel_limit_raw!r} must be >= 1 (zero or negative "
            "would block all work for this repo)"
        )
    return parallel_limit


def _repo_spec_from_env_entry(
    entry: _RepoEnvEntry,
    default_parallel_limit: int,
    config_error: ConfigError,
    config_warning: ConfigWarning,
) -> RepoSpec:
    """Validate entry options and build a RepoSpec."""
    parallel_limit = _parse_repo_parallel_limit(
        entry, default_parallel_limit, config_error
    )
    target_path = Path(entry.target_root)
    if not target_path.exists():
        config_warning(
            f"orchestrator: REPOS entry {entry.slug!r} target_root "
            f"{target_path} does not exist; worktree creation will fail"
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
    seen: set[str] = set()
    for entry_no, line in _iter_repos_entries(raw):
        entry = _parse_repo_entry(entry_no, line, config_error)
        _record_repo_slug(entry, seen, config_error)
        specs.append(
            _repo_spec_from_env_entry(
                entry, default_parallel_limit, config_error, config_warning
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
