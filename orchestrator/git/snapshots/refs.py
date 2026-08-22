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

Where it lands is qualified by the repository it came from, and the fetch and
the resolution are one locked step. Several `REPOS` entries may share a
`target_root`, so the clone a snapshot is fetched into is a store two of them
write: an unqualified local name would have the second force-fetch overwrite
the first, and a resolution taken after the lock was released would answer for
whichever fetch landed last. Both are the same failure read two ways -- a
verification against a candidate this call never saw, and a child copying files
out of the other repository's work.

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
from orchestrator.git import authentication, commands, locks
from orchestrator.git.snapshots import namespace
from orchestrator.git.worktrees import paths

# The channel the authenticated transport already reports on: these are an
# `ls-remote`, a push, a fetch, and a `rev-parse`, so an operator following a
# snapshot that could not be taken reads the same plumbing they filter for
# when a fetch or a push misbehaves.
log = logging.getLogger("orchestrator.git_plumbing")

# The lease that says the ref must not exist yet, which is the only lease a
# create may run under.
_ABSENT_LEASE = ""

# What separates a truncated repository segment from the digest that keeps it
# injective, in the spelling the branch namespace already uses for its own
# lossy rewrites.
_DIGEST_MARK = "__h"


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


# The two answers that mean the remote no longer has the ref, and therefore
# that this host's copy of it is holding objects nothing points at.
_GONE = frozenset((
    SnapshotOutcome.DELETED, SnapshotOutcome.ABSENT,
))


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
    mirror = local_snapshot_ref(spec, ref)
    # One lock over both, because the answer is about what THIS fetch brought:
    # another worktree of the same target root fetching the same ref between
    # them would have the resolution report on its landing rather than ours.
    with locks._target_root_lock(spec.target_root):
        fetched = authentication._authed_fetch(
            spec, f"+{ref}:{mirror}", cwd=worktree,
        )
        if fetched.returncode != 0:
            log.error(
                "%s: %s could not be fetched back after it was created: %s",
                spec.slug, ref, (fetched.stderr or "").strip(),
            )
            return SnapshotOutcome.REFUSED
        resolved = _local_ref_sha(worktree, mirror)
    if resolved == sha:
        return SnapshotOutcome.PROVEN
    log.error(
        "%s: %s was fetched but resolves here to %r rather than to the "
        "candidate %s", spec.slug, ref, resolved, sha,
    )
    return SnapshotOutcome.MISMATCH


def delete_snapshot_ref(
    spec: config.RepoSpec, worktree: Path, *, ref: str, sha: str,
) -> SnapshotOutcome:
    """Reclaim one snapshot ref, treating an absent one as already reclaimed.

    `ABSENT` is a success and is reported as its own answer rather than folded
    into `DELETED`, because the two describe different histories: one of them
    is this call's write, and the other is a call that already happened and
    whose record never landed. A reclamation retried after a crash is the
    second one, every time.

    `sha` is the commit the caller preserved, and it is required rather than
    inferred. Leasing against whatever the ref happens to be at now would
    delete a re-pointed ref as readily as ours: the read would observe the new
    commit, the lease would match it, and the delete would succeed -- which is
    the blind write the create refuses, aimed at destruction, and this is the
    one operation whose blast radius is somebody else's content rather than a
    refused push. So a ref carrying anything but the exact candidate this
    generation preserved is a `MISMATCH` and is left alone for a human, and
    the lease is pinned to that expected commit rather than to the reading.
    """
    if not namespace.is_snapshot_ref(ref):
        log.error("refusing to delete %r: not a snapshot ref", ref)
        return SnapshotOutcome.REFUSED
    reclaimed = _reclaimed_remote(spec, worktree, ref, sha)
    if reclaimed in _GONE:
        _drop_mirror(spec, worktree, ref)
    return reclaimed


def _reclaimed_remote(
    spec: config.RepoSpec, worktree: Path, ref: str, sha: str,
) -> SnapshotOutcome:
    """What the remote did with the one ref this generation preserved."""
    observed = authentication._remote_ref_sha(spec, worktree, ref)
    if observed is None:
        return SnapshotOutcome.UNREADABLE
    if not observed:
        return SnapshotOutcome.ABSENT
    if observed != sha:
        log.error(
            "%s: %s carries %s rather than the candidate %s it preserved; "
            "leaving it untouched", spec.slug, ref, observed, sha,
        )
        return SnapshotOutcome.MISMATCH
    deleted = authentication._delete_remote_ref(
        spec, worktree, ref=ref, expected=sha,
    )
    return SnapshotOutcome.DELETED if deleted else SnapshotOutcome.REFUSED


def local_snapshot_ref(spec: config.RepoSpec, ref: str) -> str:
    """The local ref THIS repository's copy of one snapshot lands under.

    The repository segment is the same sanitized slug the per-issue branch
    namespace is built from, so what keeps two `REPOS` entries off one
    another's branches keeps them off one another's snapshots. Published
    because the child a split creates is told to read the snapshot out of this
    name, so the instruction and the fetch have to be one string.

    Bounded here rather than by the namespace, because bounding it is a
    rewrite and a rewrite has to stay injective: configuration bounds a slug
    at nothing, so a long one is replaced by a prefix of itself plus the
    content digest the branch namespace already uses for its own lossy
    rewrites. Two repositories with a shared prefix therefore still land on
    two refs, which is the whole property the segment exists for.
    """
    return namespace.local_snapshot_ref(
        ref=ref, repository=_repository_segment(spec.slug),
    )


def _repository_segment(slug: str) -> str:
    """A ref-safe, bounded, injective segment naming one repository."""
    sanitized = paths._sanitize_branch_segment(slug)
    if len(sanitized) <= namespace.MAX_REPOSITORY_SEGMENT:
        return sanitized
    digest = paths._slug_digest(slug)
    kept = namespace.MAX_REPOSITORY_SEGMENT - len(digest) - len(_DIGEST_MARK)
    return _DIGEST_MARK.join((sanitized[:kept], digest))


def _drop_mirror(
    spec: config.RepoSpec, worktree: Path, ref: str,
) -> None:
    """Reclaim this host's copy of a snapshot whose remote ref is gone.

    Best-effort and last: what the caller was asked to settle is the remote,
    and a local ref left behind is this host's disk rather than the
    repository's. It is still dropped, because a mirror nothing deletes holds
    the snapshot's objects against `gc` for as long as the clone lives.
    """
    mirror = local_snapshot_ref(spec, ref)
    with locks._target_root_lock(spec.target_root):
        dropped = commands._git_hardened(
            "update-ref", "-d", mirror, cwd=worktree,
        )
    if dropped.returncode != 0:
        log.warning(
            "%s: local snapshot %s could not be dropped: %s",
            spec.slug, mirror, (dropped.stderr or "").strip(),
        )


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
