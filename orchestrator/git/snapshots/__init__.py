# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The immutable remote snapshot a superseded candidate is preserved as.

An oversized committed candidate that is split stops being anybody's branch:
the parent becomes an umbrella with no implementation of its own, and the work
lives on only as whatever its children reuse. So before a child exists the
candidate is preserved under a ref nothing in the ordinary workflow writes,
and every later reader is pointed at that ref rather than at a branch a human,
a merge, or an auto-delete is free to move.

The owners divide by what a caller is asking. Which ref one generation's
snapshot IS, and what a value has to look like to be one at all, is
``namespace``; creating, proving, and reclaiming it over the authenticated
transport is ``refs``.

Four properties are the whole contract, and each is a refusal rather than a
convention:

- **One namespace.** A snapshot ref is built from the generation's own
  identity and validated against the one pattern this domain writes, so a ref
  assembled from a hand-edited pinned field is refused instead of pushed.
- **No blind overwrite.** Every write is lease-pinned to what a remote read
  established: a create leases the ref as absent, and a ref already there at
  another commit is a mismatch reported to the caller, never a force.
- **Exact SHA, proved twice.** The remote is asked what the ref resolves to
  and the answer has to be the exact frozen candidate; then the ref is fetched
  back and resolved locally, because "a child can obtain this" is the property
  the whole namespace exists for and an `ls-remote` does not establish it.
- **Absent is success.** A deletion that finds nothing there has nothing to
  do, which is what makes reclamation idempotent across a crash between the
  push and the write that would have recorded it.

Callers import the owner they need, so this initializer binds nothing: the
namespace is pure string policy and costs nothing, while the ref operations
cost the authenticated transport.
"""
