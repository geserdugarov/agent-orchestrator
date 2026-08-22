# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Which ref one generation's snapshot is, and what may not be one.

A custom ref namespace rather than a branch, and that is the whole design
decision: a branch is a thing GitHub lists, rulesets protect, pull requests
attach to, and an auto-delete removes when one of those merges. A snapshot has
to outlive all of that -- it is the only copy of a candidate whose branch is
about to be superseded -- so it is written where nothing in the ordinary
workflow reaches. `refs/heads/` is not it, `refs/tags/` is a signed and
protected surface with its own policy, and `refs/pull/` is GitHub's.

Whether the production token may write this namespace under production
rulesets is an operational question a capability check against a disposable
repository answers before rollout, not one this module can decide -- but WHICH
namespace is asked about is decided here, once, so the check and the code are
about the same refs.

The name is built from the generation's identity and nothing else. A ref
carrying a title, a branch name, or a scope would be a ref carrying whatever a
human wrote, and this is a value pushed to a remote: the components are three
numbers, the pattern that accepts them is exact, and a name that does not match
it is refused rather than pushed. That refusal is not decoration -- the
identity a ref is assembled from is read back out of a pinned comment a human
can edit, so "the fields were numbers" is a claim to check rather than assume.
"""
from __future__ import annotations

import re

# The namespace one snapshot ref is written under. Everything about it is
# deliberate: `refs/orchestrator/` is a root nothing else in this repository or
# in GitHub's own layout writes, and `late-split/` says which of this
# orchestrator's mechanisms owns the refs beneath it.
SNAPSHOT_NAMESPACE = "refs/orchestrator/late-split"

# The one shape a snapshot ref may take, anchored whole. Three numbers, in the
# order a reader walks a lineage: which issue was split, which cycle of it, and
# which generation inside that cycle -- so two attempts at the same issue never
# name one ref, and a ref names exactly one adjudicated candidate.
_SNAPSHOT_REF_RE = re.compile(
    r"\Arefs/orchestrator/late-split"
    r"/issue-(?:[1-9][0-9]*)"
    r"/cycle-(?:[1-9][0-9]*)"
    r"/gen-(?:0|[1-9][0-9]*)\Z",
)

# How long a snapshot ref may be. Well past what three real identities produce
# and far short of any path limit, so a number nobody wrote cannot become a ref
# nobody can delete.
MAX_SNAPSHOT_REF = 200


class InvalidSnapshotRef(Exception):
    """A value is not a ref this domain may create, prove, or reclaim."""


def snapshot_ref(*, issue_number: int, cycle_id: int, generation: int) -> str:
    """Return the one ref this generation's snapshot is written under.

    Deterministic, which is what makes the create idempotent: a tick that
    pushed the ref and died before recording it re-derives the same name from
    the same frozen record, finds the ref already at the exact candidate, and
    treats the step as done rather than creating a second one.

    Raises rather than returning a best effort. The identities arrive from a
    pinned comment, so a damaged one would otherwise be interpolated into a ref
    this domain then pushed and could not recognize again.
    """
    built = (
        f"{SNAPSHOT_NAMESPACE}"
        f"/issue-{_identity(issue_number, 'issue_number')}"
        f"/cycle-{_identity(cycle_id, 'cycle_id')}"
        f"/gen-{_counter(generation)}"
    )
    if not is_snapshot_ref(built):
        raise InvalidSnapshotRef("the assembled ref is not in the namespace")
    return built


def is_snapshot_ref(ref: object) -> bool:
    """Whether a value is a ref this domain wrote and may act on.

    Asked of every ref before it is created, proved, or deleted, because each
    of those is a write against a remote and the value reaching them comes off
    a ledger entry a human can edit. A ref outside the namespace is somebody
    else's -- a branch, a tag, a pull-request ref -- and this domain deleting
    one would be destroying an artifact it never created.
    """
    if not isinstance(ref, str) or len(ref) > MAX_SNAPSHOT_REF:
        return False
    return _SNAPSHOT_REF_RE.match(ref) is not None


def _identity(given: object, name: str) -> int:
    """Return a positive whole identity, or refuse the ref built from it."""
    if not _whole_number(given) or given <= 0:
        raise InvalidSnapshotRef(f"{name} is not an identity")
    return given


def _counter(given: object) -> int:
    """Return a non-negative whole counter, or refuse the ref built from it."""
    if not _whole_number(given) or given < 0:
        raise InvalidSnapshotRef("generation is not a counter")
    return given


def _whole_number(given: object) -> bool:
    """Whether a value is a real integer -- not a bool, float, or string.

    Spelled here rather than borrowed from the late domain because this
    package is below the workflow and must not import it: a git owner that
    reached up into `workflow/` for a predicate would make the transport
    depend on the mode that happens to use it today.
    """
    return isinstance(given, int) and not isinstance(given, bool)
