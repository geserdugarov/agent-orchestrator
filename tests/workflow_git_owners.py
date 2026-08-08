# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The git module each name a workflow test intercepts is defined on.

Which module a mock has to land on is a property of the caller, not of the
name: a stage that imports its git owner can only be intercepted on that owner,
and a mock left on the `orchestrator.workflow` facade beside it would let the
real command run. Recording the defining module once here keeps every test that
holds the same probe pointed at the same owner, and gives `workflow_patch_runner`
one table to resolve its hermetic mocks against.
"""
from __future__ import annotations

from types import MappingProxyType
from unittest.mock import DEFAULT, patch

from orchestrator.git import authentication as _authentication
from orchestrator.git import commands as _commands
from orchestrator.git.base_sync import (
    pre_pr as _base_sync_pre_pr,
    refresh as _base_sync_refresh,
)
from orchestrator.git.publication import (
    probes as _publication_probes,
    squash as _squash,
    titles as _publication_titles,
)
from orchestrator.git.verification import (
    probes as _verification_probes,
    runner as _verify_runner,
)
from orchestrator.git.worktrees import (
    creation as _worktree_creation,
    decomposition as _worktree_decomposition,
    paths as _worktree_paths,
    recovery as _worktree_recovery,
    terminal as _worktree_terminal,
)

GIT_SEAM_OWNERS = MappingProxyType({
    "_authed_fetch": _authentication,
    "_branch_ahead_behind": _publication_probes,
    "_branch_has_unpushed_commits": _worktree_recovery,
    "_cleanup_decompose_worktree": _worktree_decomposition,
    "_cleanup_question_worktree": _worktree_terminal,
    "_cleanup_terminal_branch": _worktree_terminal,
    "_decompose_worktree_path": _worktree_decomposition,
    "_ensure_decompose_worktree": _worktree_decomposition,
    "_ensure_pr_worktree": _worktree_creation,
    "_ensure_worktree": _worktree_creation,
    "_first_commit_subject": _publication_probes,
    "_git": _commands,
    "_git_hardened": _commands,
    "_has_new_commits": _worktree_creation,
    "_head_sha": _verification_probes,
    "_infer_subject_prefix": _publication_titles,
    "_push_branch": _authentication,
    "_rebase_base_into_worktree": _base_sync_pre_pr,
    "_rebase_in_progress": _base_sync_pre_pr,
    "_refresh_base_and_worktrees": _base_sync_refresh,
    "_resolve_branch_name": _worktree_paths,
    "_run_verify_commands": _verify_runner,
    "_squash_and_force_push": _squash,
    "_worktree_dirty_files": _verification_probes,
    "_worktree_path": _worktree_paths,
})


def seam_patch(seam: str, replacement=DEFAULT):
    """Patch one git name on the owner that defines it.

    Omitting `replacement` neutralizes the seam with a fresh mock, which is
    what a caller that only has to keep the real command from running wants.
    """
    return patch.object(GIT_SEAM_OWNERS[seam], seam, replacement)
