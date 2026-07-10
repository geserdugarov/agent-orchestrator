# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Compatibility re-export hub for the worktree subsystem.

Every helper that used to live here has been extracted into a focused
module, and this file imports each one under its original name so
existing call sites (`workflow.py` re-exports and
`patch.object(worktrees, "_foo", ...)` test patches that resolve the
symbol against the worktrees module) keep working without touching the
new modules. No behavior lives here -- the file is a documented facade
whose only job is to preserve the historical `worktrees` import surface.

Module map for the extractions:

* The hardened git subprocess layer -- `_GIT_NO_PROMPT_ENV`,
  `_target_root_lock` / `_TARGET_ROOT_LOCKS` / `_TARGET_ROOT_LOCKS_LOCK`,
  `_git`, `_git_hardened`, `_authed_fetch`, `_authed_target_fetch`, and
  `_push_branch` -- lives in `git_plumbing.py`.
* The worktree naming / layout / creation / restoration / cleanup
  helpers -- `_branch_name`, `_sanitize_slug`, `_repo_worktrees_root`,
  `_worktree_path`, `_decompose_worktree_path`, `_ensure_worktree`,
  `_ensure_pr_worktree`, `_ensure_decompose_worktree`,
  `_cleanup_decompose_worktree`, `_branch_has_unpushed_commits`,
  `_cleanup_question_worktree`, `_cleanup_terminal_branch`, and
  `_has_new_commits` -- live in `worktree_lifecycle.py`.
* The local-verify runner and its worktree-state probes --
  `VerifyResult`, `_run_verify_commands`, `_truncate_verify_output`,
  `_head_sha`, `_worktree_dirty_files` -- live in `verify.py`.
* The PR branch publication helpers -- `_CONVENTIONAL_RE`,
  `_is_conventional_subject`, `_is_prefixed_subject`,
  `_first_commit_subject`, `_recent_base_subjects`,
  `_infer_subject_prefix`, `_pr_title_from_commit_or_issue`,
  `_branch_ahead_behind`, and `_squash_and_force_push` -- live in
  `branch_publication.py`.
* The per-tick base refresh, rebase routing, and crash-recovery
  helpers -- `_rebase_base_into_worktree`, `_merge_base_into_worktree`,
  `_rebase_in_progress`, `_refresh_base_and_worktrees`,
  `_PR_REFRESH_DETOUR_LABELS`, `_AUTO_REBASE_PARK_REASONS`,
  `_park_auto_rebase_failure`, `_recover_pending_auto_base_rebase`,
  `_sync_worktree_with_base`, `_sync_pr_worktree_to_base`,
  `_route_pr_worktree_to_resolving_conflict` -- live in `base_sync.py`.

Test patches that need to INTERCEPT a call from inside
`_refresh_base_and_worktrees` / `_sync_worktree_with_base` must target
`base_sync` directly because the call graph lives there; the same is
true for patches that need to intercept calls inside
`_squash_and_force_push` / `_first_commit_subject` (they live in
`branch_publication`).

Each helper preserves the existing security hardening and crash-recovery
semantics; downstream behavior is unchanged by these extractions.
Helpers remain prefixed with `_` because they are module-internal
contracts -- the public surface (the dispatcher entry points and the
stage handlers they route to) still lives in `workflow.py` and
`orchestrator/stages/`.
"""
from __future__ import annotations

import logging

from orchestrator.base_sync import _AUTO_REBASE_PARK_REASONS as _AUTO_REBASE_PARK_REASONS
from orchestrator.base_sync import _PR_REFRESH_DETOUR_LABELS as _PR_REFRESH_DETOUR_LABELS
from orchestrator.base_sync import _merge_base_into_worktree as _merge_base_into_worktree
from orchestrator.base_sync import (
    _park_auto_rebase_failure as _park_auto_rebase_failure,
)
from orchestrator.base_sync import _rebase_base_into_worktree as _rebase_base_into_worktree
from orchestrator.base_sync import _rebase_in_progress as _rebase_in_progress
from orchestrator.base_sync import (
    _recover_pending_auto_base_rebase as _recover_pending_auto_base_rebase,
)
from orchestrator.base_sync import (
    _refresh_base_and_worktrees as _refresh_base_and_worktrees,
)
from orchestrator.base_sync import (
    _route_pr_worktree_to_resolving_conflict as _route_pr_worktree_to_resolving_conflict,
)
from orchestrator.base_sync import (
    _sync_pr_worktree_to_base as _sync_pr_worktree_to_base,
)
from orchestrator.base_sync import _sync_worktree_with_base as _sync_worktree_with_base
from orchestrator.branch_publication import _CONVENTIONAL_RE as _CONVENTIONAL_RE
from orchestrator.branch_publication import _branch_ahead_behind as _branch_ahead_behind
from orchestrator.branch_publication import _first_commit_subject as _first_commit_subject
from orchestrator.branch_publication import _infer_subject_prefix as _infer_subject_prefix
from orchestrator.branch_publication import (
    _is_conventional_subject as _is_conventional_subject,
)
from orchestrator.branch_publication import _is_prefixed_subject as _is_prefixed_subject
from orchestrator.branch_publication import (
    _pr_title_from_commit_or_issue as _pr_title_from_commit_or_issue,
)
from orchestrator.branch_publication import _recent_base_subjects as _recent_base_subjects
from orchestrator.branch_publication import _squash_and_force_push as _squash_and_force_push
from orchestrator.git_plumbing import _GIT_NO_PROMPT_ENV as _GIT_NO_PROMPT_ENV
from orchestrator.git_plumbing import _TARGET_ROOT_LOCKS as _TARGET_ROOT_LOCKS
from orchestrator.git_plumbing import _TARGET_ROOT_LOCKS_LOCK as _TARGET_ROOT_LOCKS_LOCK
from orchestrator.git_plumbing import _authed_fetch as _authed_fetch
from orchestrator.git_plumbing import _authed_target_fetch as _authed_target_fetch
from orchestrator.git_plumbing import _git as _git
from orchestrator.git_plumbing import _git_hardened as _git_hardened
from orchestrator.git_plumbing import _push_branch as _push_branch
from orchestrator.git_plumbing import _target_root_lock as _target_root_lock
from orchestrator.verify import VerifyResult as VerifyResult
from orchestrator.verify import _head_sha as _head_sha
from orchestrator.verify import _run_verify_commands as _run_verify_commands
from orchestrator.verify import _truncate_verify_output as _truncate_verify_output
from orchestrator.verify import _worktree_dirty_files as _worktree_dirty_files
from orchestrator.worktree_lifecycle import _SLUG_SAFE_RE as _SLUG_SAFE_RE
from orchestrator.worktree_lifecycle import _branch_has_unpushed_commits as _branch_has_unpushed_commits
from orchestrator.worktree_lifecycle import _branch_name as _branch_name
from orchestrator.worktree_lifecycle import _cleanup_decompose_worktree as _cleanup_decompose_worktree
from orchestrator.worktree_lifecycle import _cleanup_question_worktree as _cleanup_question_worktree
from orchestrator.worktree_lifecycle import _cleanup_terminal_branch as _cleanup_terminal_branch
from orchestrator.worktree_lifecycle import _decompose_worktree_path as _decompose_worktree_path
from orchestrator.worktree_lifecycle import _ensure_decompose_worktree as _ensure_decompose_worktree
from orchestrator.worktree_lifecycle import _ensure_pr_worktree as _ensure_pr_worktree
from orchestrator.worktree_lifecycle import _ensure_worktree as _ensure_worktree
from orchestrator.worktree_lifecycle import _has_new_commits as _has_new_commits
from orchestrator.worktree_lifecycle import _repo_worktrees_root as _repo_worktrees_root
from orchestrator.worktree_lifecycle import _resolve_branch_name as _resolve_branch_name
from orchestrator.worktree_lifecycle import _sanitize_branch_segment as _sanitize_branch_segment
from orchestrator.worktree_lifecycle import _sanitize_slug as _sanitize_slug
from orchestrator.worktree_lifecycle import _worktree_path as _worktree_path

# Canonical inventory of the compatibility surface this hub re-exports.
# Every entry is imported above from a focused module (`git_plumbing`,
# `worktree_lifecycle`, `verify`, `branch_publication`, `base_sync`); keeping
# the list explicit makes the re-export surface auditable in one place and
# governs `from orchestrator.worktrees import *`. Underscore names stay in the
# list because the historical import surface these callers depend on is
# module-internal by design.
__all__ = [
    "VerifyResult",
    "_AUTO_REBASE_PARK_REASONS",
    "_CONVENTIONAL_RE",
    "_GIT_NO_PROMPT_ENV",
    "_PR_REFRESH_DETOUR_LABELS",
    "_SLUG_SAFE_RE",
    "_TARGET_ROOT_LOCKS",
    "_TARGET_ROOT_LOCKS_LOCK",
    "_authed_fetch",
    "_authed_target_fetch",
    "_branch_ahead_behind",
    "_branch_has_unpushed_commits",
    "_branch_name",
    "_cleanup_decompose_worktree",
    "_cleanup_question_worktree",
    "_cleanup_terminal_branch",
    "_decompose_worktree_path",
    "_ensure_decompose_worktree",
    "_ensure_pr_worktree",
    "_ensure_worktree",
    "_first_commit_subject",
    "_git",
    "_git_hardened",
    "_has_new_commits",
    "_head_sha",
    "_infer_subject_prefix",
    "_is_conventional_subject",
    "_is_prefixed_subject",
    "_merge_base_into_worktree",
    "_park_auto_rebase_failure",
    "_pr_title_from_commit_or_issue",
    "_push_branch",
    "_rebase_base_into_worktree",
    "_rebase_in_progress",
    "_recent_base_subjects",
    "_recover_pending_auto_base_rebase",
    "_refresh_base_and_worktrees",
    "_repo_worktrees_root",
    "_resolve_branch_name",
    "_route_pr_worktree_to_resolving_conflict",
    "_run_verify_commands",
    "_sanitize_branch_segment",
    "_sanitize_slug",
    "_squash_and_force_push",
    "_sync_pr_worktree_to_base",
    "_sync_worktree_with_base",
    "_target_root_lock",
    "_truncate_verify_output",
    "_worktree_dirty_files",
    "_worktree_path",
]

log = logging.getLogger(__name__)
