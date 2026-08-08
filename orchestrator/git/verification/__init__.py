# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Local-verification domain owners.

The `VerifyResult` model and the output budget its `output` field is sized
to live in ``models``; the redact-then-truncate pass that fills that field in
``output``; the HEAD and dirty-file probes that classify a worktree after one
verify command in ``probes``; one command's spawn / teardown / drain and the
verdict it earns in ``process``; and the `VERIFY_COMMANDS` sequencing the
validating stage calls in ``runner``. Every verification name is defined on one
of these owners, and callers import the owner they need directly, so this
initializer binds nothing and importing one owner never drags the others in.

No facade of this domain's own sits beside the package. The ``workflow`` hub
publishes a slice of these names for callers outside the tree: the HEAD and
dirty-file probes, both inventoried against ``probes``. Every other name, the
result model, the truncation pass, and the command runner among them, answers
on its owner alone. The hub resolves the
owner's own object and caches it, so the sites share identity but not a later
patch, and a test intercepting one of these helpers targets the owner --
``probes`` for the stage leaves that compare a HEAD watermark or refuse a dirty
tree, ``runner`` for the validating approval gate that spends the verify run.
"""
