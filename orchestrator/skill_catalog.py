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
`usage.py`. Skills are deduped by name across both roots; every source
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
import os
from pathlib import Path
from typing import Iterable, Optional

from . import analytics
from .config import RepoSpec
from .git_plumbing import _git

log = logging.getLogger(__name__)

# Skill roots scanned on the target repo's base ref. Both are passed as
# pathspecs to a single `git ls-tree`; a root absent from the tree simply
# contributes no lines (git does not error on it).
_SKILL_ROOTS = (".agents/skills", ".claude/skills")

# The single file that marks a skill definition. Only a file with exactly
# this name, sitting directly under `<root>/<name>/`, defines a skill.
_SKILL_FILE = "SKILL.md"


def _skill_name_from_path(path: str) -> Optional[str]:
    """Return the skill name for a direct `<root>/<name>/SKILL.md` path.

    None for any path that is not exactly a known root followed by a
    single `<name>` segment and the `SKILL.md` file -- so a deeper nesting
    (`<root>/.system/<name>/SKILL.md`), a non-`SKILL.md` file, or a path
    outside the known roots is rejected.
    """
    for root in _SKILL_ROOTS:
        prefix = root + "/"
        if not path.startswith(prefix):
            continue
        parts = path[len(prefix):].split("/")
        if len(parts) == 2 and parts[0] and parts[1] == _SKILL_FILE:
            return parts[0]
    return None


def _extract_skill_catalog(
    paths: Iterable[str],
) -> tuple[list[str], dict[str, list[str]]]:
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
    paths_by_name: dict[str, set[str]] = {}
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        name = _skill_name_from_path(path)
        if name is None:
            continue
        paths_by_name.setdefault(name, set()).add(path)
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
    r = _git(
        "ls-tree", "-r", "--name-only", base_ref, *_SKILL_ROOTS,
        cwd=spec.target_root,
    )
    if r.returncode != 0:
        log.debug(
            "repo=%s skill catalog: ls-tree of %s failed: %s; skipping",
            spec.slug, base_ref, (r.stderr or "").strip(),
        )
        return None
    return [line for line in (r.stdout or "").splitlines() if line.strip()]


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
    except Exception:
        log.exception(
            "repo=%s skill catalog collection failed; continuing", spec.slug,
        )


# --- per-run local skill discovery (out-of-band codex workaround) -----------


# codex ships its built-in skills under a `.system` container inside the global
# skills root (`$CODEX_HOME/skills/.system/<name>/SKILL.md`) and auto-loads them
# every session. It is descended only for that global root: repo skill roots do
# not carry `.system`, and the per-tick catalog deliberately ignores it there.
_SYSTEM_SKILL_DIR = ".system"


def _direct_skill_names(root: Path) -> list[str]:
    """Return the direct `<root>/<name>/SKILL.md` skill names under `root`.

    A skill is a direct child directory of `root` that contains a `SKILL.md`
    file, matching `_skill_name_from_path`'s "direct child only" rule. Names
    are sorted for deterministic output (the filesystem scan order is not).
    Dot-prefixed entries (e.g. codex's `.system` container) are not skills
    themselves -- a caller descends into them explicitly when relevant. Any
    `OSError` (missing root, permission) yields the names gathered so far and
    never raises, so a caller can fold several roots without guarding each one.
    """
    names: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return names
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir() and (Path(entry.path) / _SKILL_FILE).is_file():
                names.append(entry.name)
        except OSError:
            continue
    return sorted(names)


def discover_local_skills(cwd: Path) -> tuple[str, ...]:
    """Enumerate the skill names available to a codex run rooted at `cwd`.

    Out-of-band workaround for codex's `--json` stream carrying no
    offered-skills catalog (see the module docstring): scan the repo skill
    roots (`_SKILL_ROOTS`: `.agents/skills` / `.claude/skills`) under the run's
    worktree `cwd`, then the global `$CODEX_HOME/skills` (falling back to
    `~/.codex/skills`) codex loads. Repo roots contribute their direct
    `<root>/<name>/SKILL.md` definitions; the global root additionally
    contributes the built-in skills codex auto-loads from its `.system`
    container (`$CODEX_HOME/skills/.system/<name>/SKILL.md` -- imagegen,
    openai-docs, skill-installer, ...), so the offered set mirrors what codex
    actually loads rather than only user-placed skills. Names are deduped in
    first-seen order (repo-local before global, so a name defined in both keeps
    its repo-local position). Fail-open: a missing root or filesystem error
    contributes nothing rather than raising, so the caller degrades to an empty
    available set. Reads only skill names, never `SKILL.md` contents.
    """
    seen: dict[str, None] = {}
    for root in _SKILL_ROOTS:
        for name in _direct_skill_names(cwd / root):
            seen.setdefault(name, None)
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    global_root = Path(codex_home) / "skills"
    global_names = sorted(set(
        _direct_skill_names(global_root)
        + _direct_skill_names(global_root / _SYSTEM_SKILL_DIR)
    ))
    for name in global_names:
        seen.setdefault(name, None)
    return tuple(seen)


# codex exec's default offered-tools surface, captured from a real codex-cli
# 0.142.5 request payload (the `codex exec --json` stream itself carries no
# offered-tools frame the way claude's `system`/`init` frame does -- the
# upstream codex feature request tracks adding one). Kept as codex's own tool
# identifiers so it reads as what codex actually offers. Best-effort by nature:
# codex assembles the real per-run set from its version, feature flags, model,
# and MCP config, none of which is observable out-of-band, so this baseline can
# drift -- the trade-off the workaround accepts. MCP-server tools are not
# enumerated (that needs live negotiation).
_CODEX_OFFERED_TOOLS: tuple[str, ...] = (
    "exec_command",
    "write_stdin",
    "update_plan",
    "request_user_input",
    "view_image",
    "multi_agent_v1",
    "get_goal",
    "create_goal",
    "update_goal",
    "web_search",
)


def discover_codex_tools() -> tuple[str, ...]:
    """Return codex's best-effort offered-tools set for a trajectory record.

    Out-of-band workaround mirroring `discover_local_skills`: codex's `codex
    exec --json` stream carries no offered-tools frame, so a codex trajectory's
    "Tools offered" chip would otherwise stay empty. Returns the
    `_CODEX_OFFERED_TOOLS` baseline (see its comment for the drift caveat and
    provenance). A function rather than a bare constant so the analytics
    backfill has a single seam that can later grow config awareness.
    """
    return _CODEX_OFFERED_TOOLS
