# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Repository-entry model and REPOS parsing / default-spec construction.

This is the private home of the repository-configuration surface split out of
``orchestrator.config``: the ``RepoSpec`` per-repo identity dataclass, the
``REPOS`` environment parser (entry tokenizing, owner/name and option
validation, duplicate-slug detection, per-repo parallel-limit parsing), and the
default-spec construction that falls back to the legacy single-repo
``REPO`` / ``TARGET_REPO_ROOT`` / ``BASE_BRANCH`` / ``REMOTE_NAME`` trio when
``REPOS`` is unset. Entry tokenization and model construction live in
``_repo_config_entry`` and ``_repo_config_build`` respectively.

``orchestrator.config`` re-exports ``RepoSpec`` and wraps ``parse_repos_env`` /
``build_repo_specs`` behind ``config._parse_repos_env`` / ``config._REPO_SPECS``
/ ``config.default_repo_specs`` so every existing caller and test patch target
keeps importing from the same site. The abort-on-invalid and warn-to-stderr
diagnostics live in ``config`` (its single configuration-failure funnel) and
are injected here as callables.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

from orchestrator import _repo_config_build, _repo_config_entry

# Diagnostics injected from ``orchestrator.config`` keep configuration failure
# policy out of the parsing leaves: ``config_error`` aborts import and
# ``config_warning`` writes a non-fatal diagnostic to stderr.
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
    for entry_no, line in _repo_config_entry.iter_repos_entries(raw):
        entry = _repo_config_entry.parse_repo_entry(entry_no, line, config_error)
        _repo_config_build.record_repo_slug(entry, seen_slugs, config_error)
        specs.append(
            _repo_config_build.build_repo_spec(
                entry,
                default_parallel_limit,
                config_error,
                config_warning,
                RepoSpec,
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
