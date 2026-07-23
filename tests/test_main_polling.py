# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop repository fan-out tests."""

import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import main_helpers as _helpers


class PollingLoopFanOutTest(unittest.TestCase):
    def test_once_ticks_every_configured_spec(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop"),
                }
            ) as main_mod,
        ):
            # Recording the (spec.slug, gh.slug) pairing surfaces a regression
            # that crossed wires (spec for alpha paired with beta's gh).
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            # Parallel fan-out makes the call order non-deterministic; the
            # invariant is that every (spec, paired client) tuple appears
            # exactly once and the pairing is correct.
            self.assertEqual(
                set(recorder.calls),
                {(_helpers._ALPHA_REPO, _helpers._ALPHA_REPO), (_helpers._BETA_REPO, _helpers._BETA_REPO)},
            )
            self.assertEqual(len(recorder.calls), 2)
            clients.by_slug[_helpers._ALPHA_REPO].ensure_workflow_labels.assert_called_once()
            clients.by_slug[_helpers._BETA_REPO].ensure_workflow_labels.assert_called_once()

    def test_repo_tick_error_does_not_block_others(self) -> None:
        # The whole point of catching per-repo failures: one repo wedged in
        # an unhandled error must not stop the others from advancing. With
        # parallel fan-out the exception is isolated inside the per-repo
        # worker, so the surviving repos still complete their ticks even
        # though the failing repo's worker raised.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop\ngamma/three|{td}|main"),
                }
            ) as main_mod,
        ):
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder(
                on_tick=lambda gh, spec: _helpers._raise_on_slug(
                    spec,
                    _helpers._ALPHA_REPO,
                    "simulated alpha failure",
                ),
            )

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            # Returned 0 (loop swallowed the per-repo exception) and every
            # spec was attempted -- order is non-deterministic under
            # parallel fan-out, so assert on the set.
            self.assertEqual(rc, 0)
            self.assertEqual(
                set(recorder.slugs),
                {_helpers._ALPHA_REPO, _helpers._BETA_REPO, "gamma/three"},
            )
            self.assertEqual(len(recorder.slugs), 3)

    def test_legacy_single_repo_still_works(self) -> None:
        # No REPOS set: main.py must still run a single tick using the
        # legacy REPO/TARGET_REPO_ROOT/BASE_BRANCH trio. The single-repo
        # path stays in-thread (no executor) so a deployment that does
        # not use REPOS sees no behavior change.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            self.assertEqual(recorder.slugs, [_helpers._LEGACY_REPO])
            # No executor: the tick runs on the same thread `main` was
            # called from. A regression that always spawned a worker
            # thread (even for one repo) would show a different tid here.
            self.assertEqual(recorder.threads, [threading.get_ident()])

    def test_repos_run_concurrently(self) -> None:
        # The whole point of fan-out: configured repos must overlap. A
        # `Barrier(N)` requires every worker to arrive before any can
        # leave, so it deadlocks under sequential iteration and the
        # bounded timeout surfaces that regression as a test failure.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop\ngamma/three|{td}|main"),
                }
            ) as main_mod,
        ):
            tick_probe = _helpers._BarrierTick(3)
            clients = _helpers._ClientFactory()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=tick_probe),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            self.assertEqual(
                set(tick_probe.completed),
                {_helpers._ALPHA_REPO, _helpers._BETA_REPO, "gamma/three"},
            )

    def test_labels_initialize_once_per_spec(self) -> None:
        # `ensure_workflow_labels` must run exactly once per configured
        # repo at startup -- not on every tick. Re-running the label
        # bootstrap on each tick would burn API calls on a no-op and
        # change behavior on label edits between ticks.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop"),
                }
            ) as main_mod,
        ):
            clients = _helpers._ClientFactory()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            self.assertEqual(set(clients.by_slug), {_helpers._ALPHA_REPO, _helpers._BETA_REPO})
            for client in clients.by_slug.values():
                client.ensure_workflow_labels.assert_called_once()
