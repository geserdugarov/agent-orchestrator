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
  `_is_conventional_subject`, `_first_commit_subject`,
  `_pr_title_from_commit_or_issue`, `_branch_ahead_behind`, and
  `_squash_and_force_push` -- live in `branch_publication.py`.
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

from .base_sync import _AUTO_REBASE_PARK_REASONS as _AUTO_REBASE_PARK_REASONS
from .base_sync import _PR_REFRESH_DETOUR_LABELS as _PR_REFRESH_DETOUR_LABELS
from .base_sync import _merge_base_into_worktree as _merge_base_into_worktree
from .base_sync import (
    _park_auto_rebase_failure as _park_auto_rebase_failure,
)
from .base_sync import _rebase_base_into_worktree as _rebase_base_into_worktree
from .base_sync import _rebase_in_progress as _rebase_in_progress
from .base_sync import (
    _recover_pending_auto_base_rebase as _recover_pending_auto_base_rebase,
)
from .base_sync import (
    _refresh_base_and_worktrees as _refresh_base_and_worktrees,
)
from .base_sync import (
    _route_pr_worktree_to_resolving_conflict as _route_pr_worktree_to_resolving_conflict,
)
from .base_sync import (
    _sync_pr_worktree_to_base as _sync_pr_worktree_to_base,
)
from .base_sync import _sync_worktree_with_base as _sync_worktree_with_base
from .branch_publication import _CONVENTIONAL_RE as _CONVENTIONAL_RE
from .branch_publication import _branch_ahead_behind as _branch_ahead_behind
from .branch_publication import _first_commit_subject as _first_commit_subject
from .branch_publication import (
    _is_conventional_subject as _is_conventional_subject,
)
from .branch_publication import (
    _pr_title_from_commit_or_issue as _pr_title_from_commit_or_issue,
)
from .branch_publication import _squash_and_force_push as _squash_and_force_push
from .git_plumbing import _GIT_NO_PROMPT_ENV as _GIT_NO_PROMPT_ENV
from .git_plumbing import _TARGET_ROOT_LOCKS as _TARGET_ROOT_LOCKS
from .git_plumbing import _TARGET_ROOT_LOCKS_LOCK as _TARGET_ROOT_LOCKS_LOCK
from .git_plumbing import _authed_fetch as _authed_fetch
from .git_plumbing import _authed_target_fetch as _authed_target_fetch
from .git_plumbing import _git as _git
from .git_plumbing import _git_hardened as _git_hardened
from .git_plumbing import _push_branch as _push_branch
from .git_plumbing import _target_root_lock as _target_root_lock
from .verify import VerifyResult as VerifyResult
from .verify import _head_sha as _head_sha
from .verify import _run_verify_commands as _run_verify_commands
from .verify import _truncate_verify_output as _truncate_verify_output
from .verify import _worktree_dirty_files as _worktree_dirty_files
from .worktree_lifecycle import _SLUG_SAFE_RE as _SLUG_SAFE_RE
from .worktree_lifecycle import _branch_has_unpushed_commits as _branch_has_unpushed_commits
from .worktree_lifecycle import _branch_name as _branch_name
from .worktree_lifecycle import _cleanup_decompose_worktree as _cleanup_decompose_worktree
from .worktree_lifecycle import _cleanup_question_worktree as _cleanup_question_worktree
from .worktree_lifecycle import _cleanup_terminal_branch as _cleanup_terminal_branch
from .worktree_lifecycle import _decompose_worktree_path as _decompose_worktree_path
from .worktree_lifecycle import _ensure_decompose_worktree as _ensure_decompose_worktree
from .worktree_lifecycle import _ensure_pr_worktree as _ensure_pr_worktree
from .worktree_lifecycle import _ensure_worktree as _ensure_worktree
from .worktree_lifecycle import _has_new_commits as _has_new_commits
from .worktree_lifecycle import _repo_worktrees_root as _repo_worktrees_root
from .worktree_lifecycle import _sanitize_slug as _sanitize_slug
from .worktree_lifecycle import _worktree_path as _worktree_path

log = logging.getLogger(__name__)
