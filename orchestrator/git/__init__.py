# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git execution domain owners.

Plain and hardened `git` invocation together with the local
transport-config probe live in ``commands``; the token-bearing askpass
session, the authenticated fetches, and the hardened push live in
``authentication``; the process-local per-target-root lock registry lives
in ``locks``. Every git-execution name is defined on one of these owners,
and callers import the owner they need directly, so this initializer binds
nothing and an import pulls in only what the chosen owner itself needs --
``authentication`` builds on ``commands`` and ``locks``, while those two
depend on nothing else in the package.

No facade of this domain's own sits beside the package. The ``workflow``
hub publishes a slice of these names for callers outside the tree: five
-- the two authenticated fetches and the push off ``authentication``,
and the plain and hardened runners off ``commands``, each inventoried
against the owner that defines it. Every other name, the no-prompt
environment and the whole lock surface among them, answers on its owner
alone. The hub resolves the owner's own object and caches it, so the
sites share identity but not a later patch, and a test intercepting one
of these helpers targets this package, because that is what every caller
in the tree names: the ``git/worktrees/``, ``git/publication/``, and
``git/base_sync/`` owners; the conflicts, documenting, implementing, and
validating stages for the fetches and the push; and the conflicts,
documenting, and fixing stages for the two runners. ``authentication``
names its logger
``orchestrator.git_plumbing`` rather than after this package, because that
is the name operator log filters select on.
"""
