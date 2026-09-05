# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The order an interrupted rebase is routed in, on the `recovery` owner."""

from __future__ import annotations

import contextlib
import dataclasses
import unittest
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from orchestrator.git import branch_transport, commands as _commands
from orchestrator.git.base_sync import (
    models,
    outcomes,
    persistence,
    recovery,
    snapshot,
    transfers,
)
from orchestrator.git.verification import probes as verification_probes
from tests.git.base_sync import base_sync_helpers as fixtures
from tests.git.base_sync.gate_reads_support import _gate_candidates, _gate_reads
from tests.git.base_sync.refresh_test_support import MOVED_CHECKOUT_SHA

FETCH_SNAPSHOT = "_fetch_recovery_snapshot"

COMPLETE_SNAPSHOT = "_complete_recovery_snapshot"

CLEAR_INELIGIBLE = "_clear_ineligible_recovery"

CLEAR_UNCHANGED = "_clear_unchanged_recovery"

# The park that keeps a relabelled attempt whole, and the reading that
# says a permission on the comment is still owed a push.
STRANDED = "_park_stranded_recovery"

LEFT_MID_TRANSFER = "_left_mid_transfer"

ROUTE_SNAPSHOT = "_route_recovery_snapshot"

RETRY_PUSH = "_retry_recovery_push"

FINALIZE_HELPER = "_finalize_recovered_rebase"

PUSH_BRANCH = "_push_branch"

DIRTY_FILES = "_worktree_dirty_files"

PUSHED_METHOD = "crash_recovery_pushed"

# The keyword a gated push names the commit it publishes by, and the one the
# finalize behind it records the same commit under.
REVISION = "revision"

LOCAL_HEAD = "local_head"

LEFTOVERS = ("scratch.txt",)

ALREADY_PUBLISHED = "_finalize_already_published_recovery"

UNKNOWN_COMPARISON = "_reject_unknown_recovery_comparison"

DIVERGED = "_park_diverged_recovery"

# Every answer a completed comparison can resolve into, and the owner it is
# selected on.
ANSWERS = (
    (outcomes, ALREADY_PUBLISHED),
    (outcomes, UNKNOWN_COMPARISON),
    (outcomes, DIVERGED),
    (recovery, RETRY_PUSH),
)

# Each completed comparison and the single answer it selects. The ahead-only
# row is the only one that reaches a push, which is what keeps a force-push
# off every head the tick could not prove is ahead of the remote it read.
ROUTE_CASES = (
    (
        fixtures._snapshot(remote_head=fixtures.RECOVERED_SHA),
        ALREADY_PUBLISHED,
    ),
    (fixtures._snapshot(), UNKNOWN_COMPARISON),
    (fixtures._snapshot(ahead=1, behind=2), DIVERGED),
    (fixtures._snapshot(ahead=1), RETRY_PUSH),
)

_OWNERS = MappingProxyType(
    {
        CLEAR_INELIGIBLE: snapshot,
        CLEAR_UNCHANGED: snapshot,
        COMPLETE_SNAPSHOT: snapshot,
        FETCH_SNAPSHOT: snapshot,
        ROUTE_SNAPSHOT: recovery,
    },
)


# The commit an approval owes a push for, which the rollback would abandon.
_ABANDONED_SHA = "ab5e0000" * 5

_APPROVED_SHA = "late_approved_sha"
_APPROVED_LEASE = "late_approved_lease"
_SPENDS = "late_spends"

# The round that publication's route still owes, recorded beside the approval.
_OWED_ROUND = ("review_round", 2)

_OWED = MappingProxyType({
    _APPROVED_SHA: _ABANDONED_SHA,
    _APPROVED_LEASE: fixtures.PRE_REBASE_SHA,
    _SPENDS: [list(_OWED_ROUND)],
})

_PARK_MESSAGE = "the push did not land"
_GIT_FAILED = 128


def _handled() -> MagicMock:
    """A collaborator stub that reports the tick as handled."""
    return MagicMock(return_value=True)


@contextlib.contextmanager
def _routed(**collaborators):
    """Patch the named recovery collaborators on the owner they live on."""
    with contextlib.ExitStack() as stack:
        for name, replacement in collaborators.items():
            stack.enter_context(
                patch.object(_OWNERS[name], name, replacement),
            )
        yield


@contextlib.contextmanager
def _every_answer(selected: dict):
    """Patch every answer the route can select, recording them by name."""
    with contextlib.ExitStack() as stack:
        for owner, name in ANSWERS:
            selected[name] = _handled()
            stack.enter_context(patch.object(owner, name, selected[name]))
        yield


class RolledBackDebtTest(unittest.TestCase):
    """What a reset-and-park keeps when the reset itself will not go through.

    The reset is what makes an approved commit unreachable, and so what
    licenses dropping the record naming it. Refused, the branch may still be
    standing on that commit -- and the approval, the head its push is pinned
    to, and the route bookkeeping that push closes are the only things naming
    any of it.
    """

    def test_a_failed_reset_keeps_the_whole_debt(self) -> None:
        owing = fixtures._sync_context(**_OWED)

        with self._reset_refusing(_GIT_FAILED):
            persistence._reset_clear_and_park(
                owing, fixtures.PRE_REBASE_SHA,
                message=_PARK_MESSAGE, reason=fixtures.PARK_PUSH_FAILED,
            )

        pinned = owing.gh.pinned_data(fixtures.ISSUE)
        self.assertEqual(pinned[_APPROVED_SHA], _ABANDONED_SHA)
        self.assertEqual(pinned[_APPROVED_LEASE], fixtures.PRE_REBASE_SHA)
        self.assertEqual(tuple(pinned[_SPENDS][0]), _OWED_ROUND)

    def test_a_landed_reset_drops_it(self) -> None:
        # What says the refusal above is about the reset rather than about the
        # record never being dropped: reset, the approved commit is only in
        # the reflog and a debt naming it is one nothing can pay.
        owing = fixtures._sync_context(**_OWED)

        with self._reset_refusing(0):
            persistence._reset_clear_and_park(
                owing, fixtures.PRE_REBASE_SHA,
                message=_PARK_MESSAGE, reason=fixtures.PARK_PUSH_FAILED,
            )

        pinned = owing.gh.pinned_data(fixtures.ISSUE)
        self.assertIsNone(pinned[_APPROVED_SHA])
        self.assertIsNone(pinned[_APPROVED_LEASE])
        self.assertNotIn(_SPENDS, pinned)

    @contextlib.contextmanager
    def _reset_refusing(self, returncode: int):
        """Answer every hardened git command with `returncode`."""
        with patch.object(
            _commands, "_git_hardened",
            MagicMock(return_value=fixtures._git_result(returncode=returncode)),
        ):
            yield


# Every state a clear under an ineligible label would strand, and what the
# road reads to find it: the record the attempt left of its own replay, the
# head the checkout answers with, and whether a permission on the comment
# still says a push is owed. Each case names only what it moves off the
# ordinary world -- terms pinned, branch still on the anchor, nothing granted.
_STRANDED_ATTEMPTS = MappingProxyType({
    "a replay the attempt recorded": {
        "pending_rewrite": fixtures.RECORDED_REWRITE,
    },
    "a permission nobody spent": {"mid_transfer": True},
    "a checkout git already moved": {"head": fixtures.RECOVERED_SHA},
    "a head this host cannot read": {"head": ""},
})


class RecoveryRouteTest(unittest.TestCase):
    """Every question is asked before the one it would make unsafe."""

    def test_an_unmoved_anchor_clears_unfetched(self) -> None:
        # An anchor over a checkout still standing on it is a promise to come
        # back, and nothing under a label the refresh does not drive is
        # coming back for it.
        context = fixtures._recovery_context(
            pending_rewrite=models._PendingRewrite(),
        )
        cleared = _handled()
        fetch = MagicMock()

        with _routed(
            **{CLEAR_INELIGIBLE: cleared, FETCH_SNAPSHOT: fetch},
        ), patch.object(
            verification_probes,
            "_head_sha",
            MagicMock(return_value=fixtures.PRE_REBASE_SHA),
        ):
            recovered = recovery._recover_pending_auto_base_rebase_context(
                self._relabelled(context),
            )

        self.assertTrue(recovered)
        cleared.assert_called_once()
        # An issue nobody is refreshing any more is not worth a network hop.
        fetch.assert_not_called()

    def test_a_stranded_attempt_is_held(self) -> None:
        # Everything a clear under this same relabel would come apart. A
        # replay the tick recorded leaves the branch standing on a rewrite
        # nothing names. A permission granted for a push nobody made leaves a
        # human's verdict licensed onto a commit no push carried. A checkout
        # git has already moved under the terms alone is the window between
        # `git rebase` returning and the write that names what it produced,
        # which the terms cannot tell from an attempt that never started. And
        # a head this host cannot read is no evidence the branch is anywhere
        # in particular. Each parks with every record intact.
        for described, held in _STRANDED_ATTEMPTS.items():
            with self.subTest(standing=described):
                self._assert_stranded(**held)

    def test_unreadable_snapshot_owns_the_tick(self) -> None:
        # The fetch already reset and parked, so returning True is what stops
        # the caller from rebasing against a head it could not verify.
        route = MagicMock()

        with _routed(
            **{
                FETCH_SNAPSHOT: MagicMock(return_value=None),
                ROUTE_SNAPSHOT: route,
            },
        ):
            recovered = recovery._recover_pending_auto_base_rebase_context(
                fixtures._recovery_context(),
            )

        self.assertTrue(recovered)
        route.assert_not_called()

    def test_unmoved_head_falls_back(self) -> None:
        unchanged = fixtures._snapshot(local_head=fixtures.PRE_REBASE_SHA)
        cleared = MagicMock(return_value=False)
        route = MagicMock()

        with _routed(
            **{
                FETCH_SNAPSHOT: MagicMock(return_value=unchanged),
                CLEAR_UNCHANGED: cleared,
                ROUTE_SNAPSHOT: route,
            },
        ):
            recovered = recovery._recover_pending_auto_base_rebase_context(
                fixtures._recovery_context(
                    pending_rewrite=models._PendingRewrite(),
                ),
            )

        # Nothing was rewritten, so there is nothing to compare and the same
        # tick continues into the normal rebase flow.
        self.assertFalse(recovered)
        cleared.assert_called_once()
        route.assert_not_called()

    def test_moved_head_is_routed_from_its_comparison(self) -> None:
        moved = fixtures._snapshot()
        route = _handled()

        with _routed(
            **{
                FETCH_SNAPSHOT: MagicMock(return_value=moved),
                ROUTE_SNAPSHOT: route,
            },
        ):
            recovery._recover_pending_auto_base_rebase_context(
                fixtures._recovery_context(),
            )

        self.assertIs(route.call_args.args[1], moved)

    def _relabelled(self, context):
        return dataclasses.replace(context, label="workflow:implementing")

    def _assert_stranded(
        self,
        *,
        pending_rewrite: models._PendingRewrite = fixtures.IN_FLIGHT_REWRITE,
        head: str = fixtures.PRE_REBASE_SHA,
        mid_transfer: bool = False,
    ) -> None:
        """The relabel parks with every record intact and fetches nothing."""
        context = fixtures._recovery_context(pending_rewrite=pending_rewrite)
        cleared = MagicMock()
        stranded = _handled()
        fetch = MagicMock()

        with _routed(
            **{CLEAR_INELIGIBLE: cleared, FETCH_SNAPSHOT: fetch},
        ), patch.object(outcomes, STRANDED, stranded), patch.object(
            transfers, LEFT_MID_TRANSFER, MagicMock(return_value=mid_transfer),
        ), patch.object(
            verification_probes, "_head_sha", MagicMock(return_value=head),
        ):
            recovered = recovery._recover_pending_auto_base_rebase_context(
                self._relabelled(context),
            )

        self.assertTrue(recovered)
        stranded.assert_called_once()
        cleared.assert_not_called()
        # An issue nobody is refreshing any more is not worth a network hop.
        fetch.assert_not_called()


class RecoveryComparisonTest(unittest.TestCase):
    """One completed comparison resolves into exactly one answer."""

    def test_each_comparison_selects_its_answer(self) -> None:
        for completed, answer in ROUTE_CASES:
            with self.subTest(answer=answer):
                self._assert_selects(completed, answer)

    def test_unverified_comparison_owns_the_tick(self) -> None:
        # `_complete_recovery_snapshot` already parked; no answer applies.
        with _routed(
            **{COMPLETE_SNAPSHOT: MagicMock(return_value=None)},
        ):
            routed = recovery._route_recovery_snapshot(
                fixtures._recovery_context(), fixtures._snapshot(),
            )

        self.assertTrue(routed)

    def _assert_selects(self, completed, answer: str) -> None:
        selected = {}

        with _every_answer(selected), _routed(
            **{COMPLETE_SNAPSHOT: MagicMock(return_value=completed)},
        ):
            self.assertTrue(
                recovery._route_recovery_snapshot(
                    fixtures._recovery_context(), fixtures._snapshot(),
                ),
            )

        self.assertIs(selected.pop(answer).call_args.args[1], completed)
        for unselected in selected.values():
            unselected.assert_not_called()


class RetryRecoveryPushTest(unittest.TestCase):
    """The reissued push is guarded, leased, and finalized as its own method."""

    def setUp(self) -> None:
        # The recovered head is a candidate for a pull request the remote
        # already carries, so it is measured before it is pushed. These tests
        # are about the guard, the lease, and the finalize -- the reading gets
        # its ordinary answers.
        _gate_reads(self)

    def test_ahead_head_is_pushed_under_lease(self) -> None:
        context = fixtures._recovery_context()
        push = _handled()
        finalize = _handled()

        with self._push_patches(push=push, finalize=finalize):
            pushed = recovery._retry_recovery_push(
                context, fixtures._snapshot(ahead=1),
            )

        self.assertTrue(pushed)
        self.assertEqual(
            push.call_args.args,
            (context.spec, fixtures.WORKTREE, fixtures.BRANCH),
        )
        # The lease pins the remote to the pre-rebase anchor, so a PR head
        # that moved out of band rejects the push instead of being clobbered.
        self.assertEqual(
            push.call_args.kwargs.get("force_with_lease"),
            fixtures.PRE_REBASE_SHA,
        )
        self.assertEqual(
            finalize.call_args.kwargs.get("method"), PUSHED_METHOD,
        )
        self.assertEqual(
            finalize.call_args.kwargs.get("local_head"),
            fixtures.RECOVERED_SHA,
        )

    def test_the_push_names_the_head_it_finalizes(self) -> None:
        # This recovery verified ONE head against the remote, and the gate
        # proves the checkout again before it measures. Left unbound, a commit
        # landing between the two reads is what the push carries while the
        # notice, the event, and the finalize all name the head the snapshot
        # holds -- a pull request standing on one commit under a record naming
        # another.
        context = fixtures._recovery_context()
        push = _handled()
        finalize = _handled()

        with self._push_patches(push=push, finalize=finalize):
            recovery._retry_recovery_push(
                context, fixtures._snapshot(ahead=1),
            )

        self.assertEqual(
            push.call_args.kwargs.get(REVISION), fixtures.RECOVERED_SHA,
        )
        self.assertEqual(
            finalize.call_args.kwargs.get(LOCAL_HEAD),
            push.call_args.kwargs.get(REVISION),
        )

    def test_a_checkout_that_moved_refuses_the_push(self) -> None:
        # The same window, seen from the race it closes: the checkout is no
        # longer standing on the head this recovery verified, so nothing is
        # published and the finalize never runs.
        push = MagicMock()
        finalize = MagicMock()
        _gate_candidates(self, MOVED_CHECKOUT_SHA)

        with self._push_patches(push=push, finalize=finalize):
            handled = recovery._retry_recovery_push(
                fixtures._recovery_context(), fixtures._snapshot(ahead=1),
            )

        self.assertTrue(handled)
        push.assert_not_called()
        finalize.assert_not_called()

    def test_dirty_worktree_parks_without_pushing(self) -> None:
        push = MagicMock()
        park = _handled()

        with self._push_patches(push=push, dirty=LEFTOVERS), patch.object(outcomes, "_park_dirty_recovery", park):
            parked = recovery._retry_recovery_push(
                fixtures._recovery_context(), fixtures._snapshot(ahead=1),
            )

        # Uncommitted edits mean the recovered head is not what a push would
        # publish, so the leftovers are reported before anything leaves.
        self.assertTrue(parked)
        self.assertEqual(park.call_args.args[2], list(LEFTOVERS))
        push.assert_not_called()

    def test_rejected_push_parks(self) -> None:
        finalize = MagicMock()
        park = _handled()

        with self._push_patches(
            push=MagicMock(return_value=False), finalize=finalize,
        ), patch.object(outcomes, "_park_failed_recovery_push", park):
            parked = recovery._retry_recovery_push(
                fixtures._recovery_context(), fixtures._snapshot(ahead=1),
            )

        self.assertTrue(parked)
        park.assert_called_once()
        finalize.assert_not_called()

    @contextlib.contextmanager
    def _push_patches(self, *, push, finalize=None, dirty=()):
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    verification_probes,
                    DIRTY_FILES,
                    MagicMock(return_value=list(dirty)),
                ),
            )
            stack.enter_context(
                patch.object(branch_transport, PUSH_BRANCH, push),
            )
            stack.enter_context(
                patch.object(
                    persistence,
                    FINALIZE_HELPER,
                    finalize or _handled(),
                ),
            )
            yield


if __name__ == "__main__":
    unittest.main()
