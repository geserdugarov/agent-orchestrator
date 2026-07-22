# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-tick repo skill-catalog collection.

Enumerates the skill definitions a configured target repo carries on its
base ref and appends one `repo_skill_catalog` analytics record per tick
per spec via `analytics.record_repo_skill_catalog`. Producer-side only:
the record lands in the analytics JSONL sink (and, once synced, the
`extras` JSONB of `analytics_events` -- no DDL), so the consumer /
dashboard side stays a separate change.

Two skill roots are scanned -- `.agents/skills` and `.claude/skills` --
and only *direct* `<root>/<name>/SKILL.md` definitions count: a `SKILL.md`
nested any deeper (e.g. `.claude/skills/.system/<name>/SKILL.md`) or not
under a known root is ignored, mirroring the names-only trigger anchor in
`_usage_skills.py`. Skills are deduped by name across both roots; every source
path that produced a name is preserved under `skill_paths`.

Dashboard-local skill files are never scanned: enumeration reads the
target repo's base ref via `git ls-tree`, not the orchestrator's own
working tree. The whole producer is fail-open -- a missing clone, an
unfetched ref, a git error, or a sink IO failure logs and is swallowed so
catalog collection never disturbs the polling tick. `workflow.tick`
re-exports `_emit_repo_skill_catalog` and calls it once per tick per spec
after `_refresh_base_and_worktrees` has refreshed
`<remote_name>/<base_branch>`.

Two per-run collectors (`discover_local_skills`, `discover_codex_tools`) serve
the analytics trajectory record rather than the per-tick catalog. Codex's
`codex exec --json` stream -- unlike claude's `system`/`init` frame -- carries
no offered-skills or offered-tools catalog, so a codex run's `skills_available`
/ `tools` would stay empty. As an out-of-band workaround `discover_local_skills`
scans, directly on the filesystem, the same repo skill roots this catalog uses
(`.agents/skills` / `.claude/skills`) under the run's worktree plus the global
`$CODEX_HOME/skills` codex loads -- including the built-in skills under that
global root's `.system` container. It is fail-open (a missing root contributes
nothing) and reads only skill *names*, never `SKILL.md` contents.
`discover_codex_tools` returns a best-effort static baseline of codex exec's
offered tools (codex's stream, unlike its skill files, exposes no filesystem
source for these).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from orchestrator import _local_skills, analytics
from orchestrator.config import RepoSpec
from orchestrator.git_plumbing import _git

log = logging.getLogger(__name__)

_SkillPaths = dict[str, list[str]]
_SkillCatalog = tuple[list[str], _SkillPaths]
discover_codex_tools = _local_skills.discover_codex_tools
discover_local_skills = _local_skills.discover_local_skills
_CODEX_OFFERED_TOOLS = _local_skills._CODEX_OFFERED_TOOLS

# Skill roots scanned on the target repo's base ref. Both are passed as
# pathspecs to a single `git ls-tree`; a root absent from the tree simply
# contributes no lines (git does not error on it).
_SKILL_ROOTS = (".agents/skills", ".claude/skills")

# The single file that marks a skill definition. Only a file with exactly
# this name, sitting directly under `<root>/<name>/`, defines a skill.
_SKILL_FILE = "SKILL.md"


def _direct_skill_name(parts: list[str]) -> Optional[str]:
    """Return the name from an exact ``<name>/SKILL.md`` suffix."""
    if len(parts) != 2:
        return None
    skill_name, file_name = parts
    if not skill_name or file_name != _SKILL_FILE:
        return None
    return skill_name


def _skill_name_from_path(path: str) -> Optional[str]:
    """Return the skill name for a direct `<root>/<name>/SKILL.md` path.

    None for any path that is not exactly a known root followed by a
    single `<name>` segment and the `SKILL.md` file -- so a deeper nesting
    (`<root>/.system/<name>/SKILL.md`), a non-`SKILL.md` file, or a path
    outside the known roots is rejected.
    """
    for root in _SKILL_ROOTS:
        prefix = f"{root}/"
        if not path.startswith(prefix):
            continue
        parts = path[len(prefix):].split("/")
        skill_name = _direct_skill_name(parts)
        if skill_name is not None:
            return skill_name
    return None


def _paths_by_skill(paths: Iterable[str]) -> dict[str, set[str]]:
    """Group valid catalog paths by their direct skill name."""
    paths_by_name: dict[str, set[str]] = {}
    for raw_path in paths:
        skill_path = raw_path.strip()
        if not skill_path:
            continue
        skill_name = _skill_name_from_path(skill_path)
        if skill_name is not None:
            paths_by_name.setdefault(skill_name, set()).add(skill_path)
    return paths_by_name


def _extract_skill_catalog(
    paths: Iterable[str],
) -> _SkillCatalog:
    """Extract the deduped skill catalog from `git ls-tree` path lines.

    Keeps only direct `<root>/<name>/SKILL.md` definitions for the two
    known roots and dedupes by skill name across roots, preserving every
    source path that produced the name.

    Returns `(skills_available, skill_paths)` where `skills_available` is
    the sorted list of unique skill names and `skill_paths` maps each name
    to the sorted list of source paths that defined it. A name defined in
    both roots appears once in `skills_available` while `skill_paths`
    carries both of its source paths.
    """
    paths_by_name = _paths_by_skill(paths)
    skills_available = sorted(paths_by_name)
    skill_paths = {
        name: sorted(paths_by_name[name]) for name in skills_available
    }
    return skills_available, skill_paths


def _list_skill_tree(spec: RepoSpec) -> Optional[list[str]]:
    """Run `git ls-tree` for the skill roots on the spec's base ref.

    Returns the non-empty path lines (possibly an empty list when the
    repo carries no skills) on success, or None when the target clone is
    missing or git fails -- the caller skips the record on None so a
    missing clone or unfetched ref is a fail-open no-op, never a record
    built from partial data. The `is_dir` probe keeps a missing
    `target_root` from raising inside `subprocess.run`.
    """
    if not spec.target_root.is_dir():
        log.debug(
            "repo=%s skill catalog: target_root %s missing; skipping",
            spec.slug, spec.target_root,
        )
        return None
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    ls_tree = _git(
        "ls-tree", "-r", "--name-only", base_ref, *_SKILL_ROOTS,
        cwd=spec.target_root,
    )
    if ls_tree.returncode != 0:
        log.debug(
            "repo=%s skill catalog: ls-tree of %s failed: %s; skipping",
            spec.slug, base_ref, (ls_tree.stderr or "").strip(),
        )
        return None
    return [line for line in (ls_tree.stdout or "").splitlines() if line.strip()]


def _collect_and_record_catalog(spec: RepoSpec) -> None:
    """Enumerate the target repo's skills and append one analytics record."""
    paths = _list_skill_tree(spec)
    if paths is None:
        return
    skills_available, skill_paths = _extract_skill_catalog(paths)
    analytics.record_repo_skill_catalog(
        repo=spec.slug,
        base_branch=spec.base_branch,
        remote_name=spec.remote_name,
        skills_available=skills_available,
        skill_paths=skill_paths or None,
    )
    log.debug(
        "repo=%s skill catalog: recorded %d skill(s)",
        spec.slug, len(skills_available),
    )


def _emit_repo_skill_catalog(spec: RepoSpec) -> None:
    """Collect the target repo's skill catalog and append one record.

    Called once per tick per spec from `workflow.tick` after the base
    fetch in `_refresh_base_and_worktrees` has refreshed
    `<remote_name>/<base_branch>`. Emits unconditionally on a successful
    enumeration (an empty catalog still records `skills_available: []`).
    Fail-open: any failure (missing clone, unfetched ref, git error, sink
    IO) logs and is swallowed so catalog collection never disturbs the
    polling tick -- analytics is observation-only, never authoritative
    workflow state.
    """
    try:
        _collect_and_record_catalog(spec)
    except Exception:
        log.exception(
            "repo=%s skill catalog collection failed; continuing", spec.slug,
        )
