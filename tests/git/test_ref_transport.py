# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The lease-pinned ref plumbing the snapshot namespace is written through.

What the ref update DOES to a repository is covered against real git beside the
snapshot owners. What is asserted here is the envelope it carries and the two
refusals that must happen before any of it runs -- neither of which a real
remote would exercise, because both are about refusing to reach one.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import config
from orchestrator.git import authentication

from tests.git.authentication_test_support import (
    FAKE_TOKEN,
    SECRET_TOKEN,
    SUBPROCESS_RUN,
    TOKEN_RESOLVER,
    WORKTREE,
    _assert_hardened_fetch,
    _spec,
)
from tests.git.concurrency_test_support import (
    PROBE_DELAY_SECONDS,
    THREAD_TIMEOUT_SECONDS,
    _ConcurrencyProbe,
    _start_and_join,
)
from tests.git.transport_helpers import _GitRunRecorder

REF = "refs/orchestrator/late-split/issue-41/cycle-3/gen-1"

OBJECT_ID_LENGTH = 40

SHA = "a" * OBJECT_ID_LENGTH

OTHER_SHA = "b" * OBJECT_ID_LENGTH

PROXY_HIT = "http.proxy http://evil.example:8080\n"

PLUMBING_LOG = "orchestrator.git_plumbing"

ERROR = "ERROR"

PUSH = "push"

REF_WORKER_COUNT = 4


def _clean_probe() -> MagicMock:
    """A transport-config probe reporting nothing hijackable."""
    return MagicMock(returncode=1, stdout="", stderr="")


class _PushProbe:
    """Count how many ref updates are inside the target-root lock at once."""

    def __init__(self, probe: _ConcurrencyProbe) -> None:
        self._probe = probe

    def __call__(self, args, **_options):
        if PUSH in args:
            return self._probe.record(f"push({threading.get_ident()})")
        return _clean_probe()


def _failed_push() -> MagicMock:
    """A push GitHub rejected, with the token echoed back in its stderr."""
    return MagicMock(
        returncode=1, stdout="", stderr=f"denied for {SECRET_TOKEN}\n",
    )


class RefPushTest(unittest.TestCase):
    """A ref is published under the lease its caller established."""

    def test_a_create_leases_the_ref_as_absent(self) -> None:
        # The create lease: the ref must not exist. Anything looser is the
        # blind overwrite an immutable namespace cannot have.
        run_recorder = _GitRunRecorder(probe_result=_clean_probe())

        with (
            patch(SUBPROCESS_RUN, side_effect=run_recorder),
            patch.object(config, TOKEN_RESOLVER, return_value=FAKE_TOKEN),
        ):
            pushed = authentication._push_ref(
                _spec(), WORKTREE, ref=REF, revision=SHA, expected="",
            )

        self.assertTrue(pushed)
        argv = run_recorder.args
        self.assertIn(PUSH, argv)
        self.assertIn(f"--force-with-lease={REF}:", argv)
        self.assertIn(f"{SHA}:{REF}", argv)
        _assert_hardened_fetch(self, run_recorder, FAKE_TOKEN)

    def test_an_update_leases_what_was_read(self) -> None:
        run_recorder = _GitRunRecorder(probe_result=_clean_probe())

        with (
            patch(SUBPROCESS_RUN, side_effect=run_recorder),
            patch.object(config, TOKEN_RESOLVER, return_value=FAKE_TOKEN),
        ):
            authentication._push_ref(
                _spec(), WORKTREE, ref=REF, revision=SHA, expected=OTHER_SHA,
            )

        self.assertIn(
            f"--force-with-lease={REF}:{OTHER_SHA}", run_recorder.args,
        )

    def test_a_delete_sends_an_empty_refspec(self) -> None:
        run_recorder = _GitRunRecorder(probe_result=_clean_probe())

        with (
            patch(SUBPROCESS_RUN, side_effect=run_recorder),
            patch.object(config, TOKEN_RESOLVER, return_value=FAKE_TOKEN),
        ):
            authentication._delete_remote_ref(
                _spec(), WORKTREE, ref=REF, expected=SHA,
            )

        self.assertIn(f":{REF}", run_recorder.args)
        self.assertIn(f"--force-with-lease={REF}:{SHA}", run_recorder.args)


class RefTransportRefusalTest(unittest.TestCase):
    """Nothing token-bearing runs where the transport could be hijacked."""

    def test_a_missing_token_runs_nothing(self) -> None:
        run_recorder = _GitRunRecorder(probe_result=_clean_probe())

        with (
            patch(SUBPROCESS_RUN, side_effect=run_recorder),
            patch.object(config, TOKEN_RESOLVER, return_value=""),
            self.assertLogs(PLUMBING_LOG, level=ERROR),
        ):
            pushed = authentication._push_ref(
                _spec(), WORKTREE, ref=REF, revision=SHA, expected="",
            )

        self.assertFalse(pushed)
        self.assertIsNone(run_recorder.args)

    def test_a_hijackable_local_config_runs_nothing(self) -> None:
        # A local `http.proxy` would tunnel the token-bearing push through an
        # attacker's proxy, and a `-c` override on the command line does not
        # beat a URL-scoped variant of it.
        run_recorder = _GitRunRecorder(
            probe_result=MagicMock(returncode=0, stdout=PROXY_HIT, stderr=""),
        )

        with (
            patch(SUBPROCESS_RUN, side_effect=run_recorder),
            patch.object(config, TOKEN_RESOLVER, return_value=FAKE_TOKEN),
            self.assertLogs(PLUMBING_LOG, level=ERROR),
        ):
            deleted = authentication._delete_remote_ref(
                _spec(), WORKTREE, ref=REF, expected=SHA,
            )

        self.assertFalse(deleted)
        self.assertIsNone(run_recorder.args)

    def test_a_refused_update_never_logs_the_token(self) -> None:
        # git echoes the URL it was given, and an operator log is the same
        # surface one step over from the askpass the token is hidden behind.
        run_recorder = _GitRunRecorder(
            probe_result=_clean_probe(), command_result=_failed_push(),
        )

        with (
            patch(SUBPROCESS_RUN, side_effect=run_recorder),
            patch.object(config, TOKEN_RESOLVER, return_value=SECRET_TOKEN),
            self.assertLogs(PLUMBING_LOG, level=ERROR) as reported,
        ):
            pushed = authentication._push_ref(
                _spec(), WORKTREE, ref=REF, revision=SHA, expected="",
            )
            diagnostic = "\n".join(reported.output)

        self.assertFalse(pushed)
        self.assertNotIn(SECRET_TOKEN, diagnostic)


class RefUpdateSerializationTest(unittest.TestCase):
    """Ref updates against one target root run one at a time.

    The namespace a snapshot is written into is the one a verifying fetch
    reads back into the shared clone, so two workers updating it from two
    worktrees of the same target root would race the ref update one of them is
    supposed to be proving.
    """

    def test_ref_updates_serialize_per_root(self) -> None:
        probe = _ConcurrencyProbe(delay=PROBE_DELAY_SECONDS)

        with (
            patch.object(config, TOKEN_RESOLVER, return_value=FAKE_TOKEN),
            patch(SUBPROCESS_RUN, side_effect=_PushProbe(probe)),
        ):
            threads = [
                threading.Thread(
                    target=authentication._push_ref,
                    args=(_spec(), WORKTREE),
                    kwargs={"ref": REF, "revision": SHA, "expected": ""},
                )
                for _worker in range(REF_WORKER_COUNT)
            ]
            _start_and_join(threads, timeout=THREAD_TIMEOUT_SECONDS)
            for thread in threads:
                self.assertFalse(thread.is_alive())

        self.assertEqual(probe.maximum_in_flight, 1)


if __name__ == "__main__":
    unittest.main()
