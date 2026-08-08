# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Branch-publication domain owners.

Branch inspection -- ahead/behind counts, commit-subject reads, and the
subject-shape predicates they feed -- lives in ``probes``; prefix inference
and PR-title selection live in ``titles``; the preconditions a squash is
planned from live in ``planning``; the reset, commit, force-push, and
rollback that spend that plan live in ``rewrite``; and ``squash`` composes
the two halves into the entry point stage handlers call. Callers import the
owner they need directly, so this initializer binds nothing and importing
``probes`` never drags the rewrite path in.

No facade of this domain's own sits beside the package. The ``workflow``
hub publishes a slice of these names for callers outside the tree: seven
-- the divergence probe, the first-commit-subject read, the two
subject-shape predicates, the two title helpers, and the squash entry
point -- each inventoried against the owner that defines it. Every other
name, the conventional-commit pattern and the recent-base-subject read
among them, answers on
its owner alone. The hub resolves the owner's own object and caches it, so the
sites share identity but not a later patch, and a test intercepting one of
these helpers targets the owner -- ``probes`` for base sync's divergence check,
for the ahead/behind reads the documenting, conflicts, and validating stages
take, and for the first-commit subject behind a fresh dev PR, ``titles`` for
the two helpers that PR falls back to, and ``squash`` for validating's
squash. ``orchestrator.branch_publication`` names only the logger ``rewrite``
reports on -- an operator's filter prefix rather than a module path.
"""
