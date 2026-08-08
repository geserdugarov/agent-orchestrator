# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree naming, creation, recovery, cleanup, and terminal owners.

Slug sanitization, branch and path derivation, and the pinned / legacy
branch resolver live in ``paths``; candidate-branch discovery and the
unpushed-commit probes live in ``recovery``; the issue / PR worktree
creators and the new-commit probe they gate on live in ``creation``; the
decomposer's scratch checkout lifecycle lives in ``decomposition``;
per-issue worktree removal and local branch deletion live in ``cleanup``,
and the question / PR-terminal teardowns that compose them live in
``terminal``. Every worktree name is defined on one of these owners, and
callers import the owner they need directly, so this initializer binds
nothing and importing one owner never drags the others in.

No facade of this domain's own sits beside the package. The ``workflow``
hub publishes a slice of these names for callers outside the tree:
fourteen -- the two sanitizers, the branch and worktree-path
derivations and the pinned / legacy resolver, the unpushed-commit probe,
the two creators and the new-commit probe, the decomposer's path,
creation, and removal, and the two teardowns -- each inventoried against
the owner that defines it. Every other name, the slug pattern and the
worktrees root among them, answers on its owner alone. The hub resolves
the owner's own object and caches it, so the sites
share identity but not a later patch, and a test intercepting one of these
helpers targets the owner: the stage handlers name it just as the
``git/base_sync/`` and ``workflow/engine/`` callers do. ``cleanup``,
``creation``, ``decomposition``, and ``terminal`` name their logger
``orchestrator.worktree_lifecycle`` rather than after this package, because
that is the name operator log filters select on.
"""
