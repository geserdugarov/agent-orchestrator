# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Process-local scheduler for per-issue handlers.

The polling loop drives per-issue work concurrently across repos under a
global cap and a per-repo cap. This module owns the process-local
in-flight state and the executor that actually runs the work. It is a
plain library -- no GitHub or workflow imports -- so the tick loop can
hand work to it without importing the workflow facade.

API:

* ``submit(repo_slug, issue_number, fn, *, family=False, per_repo_cap=None)``
  -- nonblocking. Returns True when a worker thread was dispatched, False
  when the call was skipped (duplicate active issue, global cap reached,
  per-repo cap reached, family slot already taken, or the scheduler has
  been shut down).
* ``reap()`` -- nonblocking. Drains completed futures, logs any worker
  exception, returns the number of futures drained. Completion markers
  (in-flight set, per-repo counter, family flag) are cleared in the
  worker's done-callback, NOT here, so a follow-up ``submit`` for the
  same issue is unblocked the instant the worker exits even if ``reap``
  is never called. ``reap`` exists for failure logging and as an explicit
  drain hook for tests / shutdown.
* ``shutdown(*, wait=True)`` -- nonblocking submit path is closed first,
  then the executor is shut down and any leftover failures drained
  through ``reap``.

The in-flight set keys on ``(repo_slug, issue_number)``: an issue
already running in one repo does not block the same issue number in a
different repo. The family-aware gate (cross-issue writers like
``decomposing`` / ``blocked`` / ``umbrella``) is one shared slot per
repo, NOT per (repo, issue), so a single family worker on a repo blocks
every other family worker on that repo regardless of issue number while
still leaving non-family workers free to run.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

log = logging.getLogger(__name__)


class IssueScheduler:
    """Process-local scheduler/executor for per-issue handlers.

    Construct once at process start and reuse across every tick. Caller
    owns the polling loop and drives ``submit`` / ``reap`` calls from
    there; the scheduler itself does not poll GitHub.
    """

    def __init__(
        self,
        *,
        global_cap: int,
        per_repo_cap: int,
        thread_name_prefix: str = "orch-worker",
    ) -> None:
        self._global_cap = max(1, int(global_cap))
        self._per_repo_cap = max(1, int(per_repo_cap))
        # max_workers must be at least 1; using the global cap means the
        # executor itself never queues -- every accepted submit gets a
        # live worker immediately, which is the whole point of the
        # nonblocking submit contract.
        self._executor = ThreadPoolExecutor(
            max_workers=self._global_cap,
            thread_name_prefix=thread_name_prefix,
        )
        # Reentrant because `submit` holds the lock through
        # `executor.submit` + `add_done_callback`; if the worker
        # completes between those two calls, `add_done_callback` fires
        # the callback synchronously in the submitter's thread and
        # `_on_worker_done` needs to reacquire this same lock.
        self._lock = threading.RLock()
        self._active: set[tuple[str, int]] = set()
        self._per_repo_active: dict[str, int] = defaultdict(int)
        self._family_active_repos: set[str] = set()
        # Completed futures awaiting `reap`. Done-callbacks append here
        # AFTER clearing the in-flight markers so a follow-up submit for
        # the same key is unblocked the instant the worker exits, even
        # if `reap` has not been called yet.
        self._completed: list[Future] = []
        self._closed = False

    # -- introspection ------------------------------------------------

    @property
    def global_cap(self) -> int:
        return self._global_cap

    @property
    def per_repo_cap(self) -> int:
        return self._per_repo_cap

    def active_count(self, repo_slug: Optional[str] = None) -> int:
        """Number of currently in-flight workers, total or per-repo."""
        with self._lock:
            if repo_slug is None:
                return len(self._active)
            return self._per_repo_active.get(repo_slug, 0)

    def is_active(self, repo_slug: str, issue_number: int) -> bool:
        with self._lock:
            return (repo_slug, int(issue_number)) in self._active

    # -- submit/reap/shutdown ----------------------------------------

    def submit(
        self,
        repo_slug: str,
        issue_number: int,
        fn: Callable[[], None],
        *,
        family: bool = False,
        per_repo_cap: Optional[int] = None,
    ) -> bool:
        """Try to dispatch ``fn`` for the given issue. Nonblocking.

        Returns True when a worker was dispatched, False when the call
        was skipped. Skip reasons (any one is sufficient):
        * scheduler is shut down,
        * the (repo_slug, issue_number) is already in flight,
        * the global active-worker cap is reached,
        * the per-repo cap (caller-provided override or default) is reached,
        * ``family=True`` and another family worker on this repo is in flight.

        The override ``per_repo_cap`` is the per-spec ``parallel_limit``
        from ``RepoSpec`` -- the issue allows different repos to declare
        different caps; the default ``per_repo_cap`` set at construction
        is the fallback for repos that did not override.
        """
        key = (repo_slug, int(issue_number))
        cap = self._per_repo_cap if per_repo_cap is None else max(1, int(per_repo_cap))
        # The whole reserve → executor.submit → add_done_callback
        # sequence runs under `self._lock`. Without this, a worker
        # can complete between `executor.submit` returning and
        # `add_done_callback` being registered: a concurrent
        # `shutdown(wait=True)` would then complete its executor drain
        # and its one `reap()` BEFORE the done-callback fires, so the
        # worker's failure is never logged and its in-flight marker
        # never released. Holding the lock through both steps means a
        # concurrent shutdown blocks until callback registration is
        # finished, and the lock is reentrant so the synchronous
        # firing of `add_done_callback` for an already-done future
        # (very-fast worker) can reacquire it in `_on_worker_done`.
        with self._lock:
            if self._closed:
                return False
            if key in self._active:
                return False
            if len(self._active) >= self._global_cap:
                return False
            if self._per_repo_active.get(repo_slug, 0) >= cap:
                return False
            if family and repo_slug in self._family_active_repos:
                return False
            self._active.add(key)
            self._per_repo_active[repo_slug] += 1
            if family:
                self._family_active_repos.add(repo_slug)
            try:
                future = self._executor.submit(fn)
            except RuntimeError:
                # Executor was shut down between the closed-check
                # above and the submit call (the executor and the
                # `_closed` flag are not the same gate). Roll back the
                # reservation so the next tick can retry without a
                # phantom in-flight marker.
                self._release_slot_locked(key, repo_slug, family=family)
                return False
            future.add_done_callback(
                lambda fut, _key=key, _slug=repo_slug, _family=family:
                self._on_worker_done(fut, _key, _slug, _family)
            )
        return True

    def reap(self) -> int:
        """Drain completed futures, log any worker exception. Nonblocking.

        Completion markers are cleared in the worker's done-callback, so
        ``reap`` does not gate "duplicate submit" recovery; its sole
        purpose is to log failures and to make failures observable on
        the tick thread (so an exception in a worker is not lost when
        the future is the only reference to it).

        Returns the count of futures drained on this call.
        """
        with self._lock:
            drained = self._completed
            self._completed = []
        for fut in drained:
            exc = fut.exception()
            if exc is not None:
                log.error(
                    "scheduler worker raised", exc_info=exc,
                )
        return len(drained)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting new submits, then drain.

        Closing the submit path first means a tick currently iterating
        cannot keep enqueueing work after shutdown was requested. With
        ``wait=True`` the call blocks until in-flight workers exit; with
        ``wait=False`` it returns immediately and the workers finish in
        the background.

        Safe to call repeatedly: each call honors its own ``wait``
        argument. ``shutdown(wait=False)`` followed by
        ``shutdown(wait=True)`` will still block on the second call
        until in-flight workers exit, and the trailing ``reap`` drains
        any failures that landed in between. A prior early-return on
        repeated calls would have made the second ``wait=True`` a
        silent no-op and stranded those completions.
        """
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)
        # Drain anything that completed during shutdown so the failure
        # log captures workers that raised on the way out.
        self.reap()

    # -- internals ---------------------------------------------------

    def _release_slot_locked(
        self, key: tuple[str, int], repo_slug: str, *, family: bool
    ) -> None:
        """Drop the in-flight markers for ``key``. Caller holds ``self._lock``."""
        self._active.discard(key)
        count = self._per_repo_active.get(repo_slug, 0)
        if count <= 1:
            self._per_repo_active.pop(repo_slug, None)
        else:
            self._per_repo_active[repo_slug] = count - 1
        if family:
            self._family_active_repos.discard(repo_slug)

    def _on_worker_done(
        self,
        future: Future,
        key: tuple[str, int],
        repo_slug: str,
        family: bool,
    ) -> None:
        # Marker release and completion-queue append happen in ONE
        # critical section so the transition is atomic from `reap`'s
        # perspective. Without this, a caller could observe
        # `is_active() == False` (slot released) and then call `reap`
        # before the callback re-acquired the lock to append the
        # future -- the worker's exception would be drained into the
        # empty list and silently dropped if no later reap ran. Holding
        # one lock for both steps guarantees that any reap which sees
        # the cleared marker also sees the completed future.
        with self._lock:
            self._release_slot_locked(key, repo_slug, family=family)
            self._completed.append(future)
