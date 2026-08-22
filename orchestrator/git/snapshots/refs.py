# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Creating, proving, and reclaiming one immutable snapshot ref.

Three operations, each of which answers with what it established rather than
with a bare boolean, because the caller's next move differs per answer: a ref
already at the exact candidate is the crashed tick's own work and the step is
done, a ref at some other commit is a namespace collision nobody may resolve
automatically, and a remote nobody could ask is a retry rather than a verdict.

**Create is create-or-verify.** The remote is asked first, and what it says
decides the write: nothing there is created under a lease that says so, the
exact candidate is the answer this call wanted, and anything else is reported
as a mismatch and left exactly where it is. There is no branch here that
overwrites -- an immutable ref that can be re-pointed is not immutable, and the
one thing worse than failing to preserve a candidate is preserving something
else under the name every child is about to be told to read.

**Proving is a fetch, not a read.** `ls-remote` says a ref resolves to a SHA on
the server; it does not say the objects behind it can be obtained. What every
child is promised is that they can obtain this candidate, so the ref is fetched
back into the clone the worktrees share and resolved there, and only a local
resolution equal to the frozen candidate is a proof. A namespace the token can
write and not read would otherwise pass every check until the first child tried
to use it.

**Absent is success.** A deletion that finds no ref has nothing to reclaim, and
saying so is what makes reclamation idempotent across the crash between the
push that deleted a ref and the write that would have recorded it. What is
still there is deleted under a lease pinned to the SHA this call just read, so
a ref somebody re-pointed in between is refused rather than destroyed.

Every ref is checked against the namespace before anything is asked of the
remote, because the value arrives from a durable ledger a human can edit and
all three operations are writes -- or, in the delete's case, a destruction --
against somebody's repository.
"""
from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Optional

from orchestrator import config
from orchestrator.git import authentication, commands
from orchestrator.git.snapshots import namespace

# The channel the authenticated transport already reports on: these are an
# `ls-remote`, a push, a fetch, and a `rev-parse`, so an operator following a
# snapshot that could not be taken reads the same plumbing they filter for
# when a fetch or a push misbehaves.
log = logging.getLogger("orchestrator.git_plumbing")

# The lease that says the ref must not exist yet, which is the only lease a
# create may run under.
_ABSENT_LEASE = ""


class SnapshotOutcome(Enum):
    """What one snapshot operation established.

    A plain `Enum` rather than a `StrEnum`: nothing here is written to a pinned
    comment or a sink -- what a caller records is the ledger entry's own state
    -- so a member renamed here is a refactor rather than a migration.
    """

    CREATED = "created"
    PRESENT = "present"
    PROVEN = "proven"
    MISMATCH = "mismatch"
    UNREADABLE = "unreadable"
    REFUSED = "refused"
    ABSENT = "absent"
    DELETED = "deleted"


def create_snapshot_ref(
    spec: config.RepoSpec, worktree: Path, *, ref: str, sha: str,
) -> SnapshotOutcome:
    """Preserve one exact commit under `ref`, or say why it was not.

    `PRESENT` and `CREATED` are both success and are kept apart because only
    one of them wrote anything: a retry after a crash finds the ref it already
    pushed and reports `PRESENT`, which is what tells an operator reading the
    log that the second attempt cost a read rather than a write.

    `MISMATCH` is never resolved here. A ref in this namespace is derived from
    one generation's identity, so another commit under it means either an
    identity two adjudications shared or somebody writing into the namespace by
    hand -- and both of those are questions for a human, while the automatic
    answer would be overwriting the only copy of somebody's candidate.
    """
    if not namespace.is_snapshot_ref(ref):
        log.error("refusing to create %r: not a snapshot ref", ref)
        return SnapshotOutcome.REFUSED
    observed = authentication._remote_ref_sha(spec, worktree, ref)
    if observed is None:
        return SnapshotOutcome.UNREADABLE
    if observed == sha:
        return SnapshotOutcome.PRESENT
    if observed:
        log.error(
            "%s already carries %s at %s, not the candidate %s it was to "
            "preserve; leaving it untouched",
            spec.slug, ref, observed, sha,
        )
        return SnapshotOutcome.MISMATCH
    created = authentication._push_ref(
        spec, worktree, ref=ref, revision=sha, expected=_ABSENT_LEASE,
    )
    return SnapshotOutcome.CREATED if created else SnapshotOutcome.REFUSED


def prove_snapshot_ref(
    spec: config.RepoSpec, worktree: Path, *, ref: str, sha: str,
) -> SnapshotOutcome:
    """Fetch the snapshot back and prove it resolves here to `sha`.

    The half an `ls-remote` cannot answer. Every child this split creates is
    told to read the candidate out of this ref, so what has to be established
    is that the ref can be OBTAINED -- a namespace a token may write and not
    read, or one a fetch refspec cannot name, would pass a remote read and fail
    the first child that tried to use it.

    `MISMATCH` here is the sharper of the two mismatches: the remote agreed a
    moment ago and what landed locally is a different commit, so nothing about
    the candidate the children would be cut from can be vouched for.
    """
    if not namespace.is_snapshot_ref(ref):
        log.error("refusing to fetch %r: not a snapshot ref", ref)
        return SnapshotOutcome.REFUSED
    fetched = authentication._authed_fetch(
        spec, f"+{ref}:{ref}", cwd=worktree,
    )
    if fetched.returncode != 0:
        log.error(
            "%s: %s could not be fetched back after it was created: %s",
            spec.slug, ref, (fetched.stderr or "").strip(),
        )
        return SnapshotOutcome.REFUSED
    resolved = _local_ref_sha(worktree, ref)
    if resolved == sha:
        return SnapshotOutcome.PROVEN
    log.error(
        "%s: %s was fetched but resolves here to %r rather than to the "
        "candidate %s", spec.slug, ref, resolved, sha,
    )
    return SnapshotOutcome.MISMATCH


def delete_snapshot_ref(
    spec: config.RepoSpec, worktree: Path, *, ref: str,
) -> SnapshotOutcome:
    """Reclaim one snapshot ref, treating an absent one as already reclaimed.

    `ABSENT` is a success and is reported as its own answer rather than folded
    into `DELETED`, because the two describe different histories: one of them
    is this call's write, and the other is a call that already happened and
    whose record never landed. A reclamation retried after a crash is the
    second one, every time.

    Pinned to the SHA this call just read, so a ref re-pointed between the read
    and the delete is refused. The alternative -- deleting whatever is there --
    is the same blind write the create refuses, aimed at destruction.
    """
    if not namespace.is_snapshot_ref(ref):
        log.error("refusing to delete %r: not a snapshot ref", ref)
        return SnapshotOutcome.REFUSED
    observed = authentication._remote_ref_sha(spec, worktree, ref)
    if observed is None:
        return SnapshotOutcome.UNREADABLE
    if not observed:
        return SnapshotOutcome.ABSENT
    deleted = authentication._delete_remote_ref(
        spec, worktree, ref=ref, expected=observed,
    )
    return SnapshotOutcome.DELETED if deleted else SnapshotOutcome.REFUSED


def _local_ref_sha(worktree: Path, ref: str) -> Optional[str]:
    """Resolve a fetched snapshot ref in this checkout, or None.

    Hardened for the reason every read of an agent-writable worktree is, and
    for the one that matters most to a commit named by id: `refs/replace/` and
    the graft file both make git answer for one commit under another's name,
    and both live in the clone the agent's worktree shares. Peeled to a commit
    so a ref somebody pointed at a tag object reads as the work rather than as
    the label on it.
    """
    resolved = commands._git_hardened(
        "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
        cwd=worktree,
    )
    named = (resolved.stdout or "").strip()
    if resolved.returncode != 0 or not named:
        return None
    return named
