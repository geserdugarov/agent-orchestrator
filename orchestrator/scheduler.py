# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Process-local scheduler for per-issue handlers.

The polling loop drives per-issue work concurrently across repos under a
global cap and a per-repo cap. This module owns the process-local
in-flight state and the executor that actually runs the work. It is a
plain library -- no GitHub or workflow imports -- so the tick loop can
hand work to it without importing the workflow facade.

API:

* ``submit(repo_slug, issue_number, fn, *, family=False, cap_exempt=False,
  per_repo_cap=None)``
  -- nonblocking. Returns True when a worker thread was dispatched, False
  when the call was skipped (duplicate active issue, global cap reached,
  per-repo cap reached, family slot already taken, or the scheduler has
  been shut down). ``cap_exempt=True`` skips the global and per-repo cap
  checks (and does not consume a cap slot) while still honoring the
  duplicate-active gate and the family mutex; used by no-agent family
  buckets and closed-issue terminal finalizations so a pure label /
  dep-graph walk or a cheap done/rejected flip never gets blocked by
  ordinary implementation work this tick.
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
* ``track_active(repo_slug, issue_number)`` -- context manager that
  registers ``(repo, issue)`` in the in-flight set for the duration of
  the block without bumping the per-repo counter. The family-bucket
  drain in ``_dispatch_via_scheduler`` uses it so per-issue
  ``is_active`` checks (notably the pre-tick base refresh's worktree
  skip) keep working for the issue currently being processed inside
  the bucket task.

The in-flight set keys on ``(repo_slug, issue_number)``: an issue
already running in one repo does not block the same issue number in a
different repo. The family-aware gate (cross-issue writers like
``decomposing`` / ``blocked`` / ``umbrella``) is one shared slot per
repo, NOT per (repo, issue), so a single family worker on a repo blocks
every other family worker on that repo regardless of issue number while
still leaving non-family workers free to run.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

log = logging.getLogger(__name__)


_EXEMPT_POOL_WORKERS: int = 32
"""Worker-thread pool size for the cap-exempt executor.

Deliberately independent of ``global_cap``: cap-exempt work is by
definition not subject to ``MAX_PARALLEL_ISSUES_GLOBAL``, so sizing
this pool against the cap would silently re-impose it. A multi-repo
orchestrator can have one no-agent family bucket per repo plus a handful
of closed-issue terminal finalizations in flight at once, and those
handlers are short (label / dep-graph walk, or a done/rejected flip with
branch cleanup), so a fixed generous bound covers any realistic
deployment without spinning up unbounded threads. A rare burst past the
bound (e.g. many PRs merged at once) simply queues on this executor and
drains quickly -- still never blocked by cap-counted agent work.
"""


@dataclass(frozen=True)
class _Submission:
    """Normalized inputs that travel together through slot reservation."""

    repo_slug: str
    issue_number: int
    fn: Callable[[], None]
    family: bool
    cap_exempt: bool
    per_repo_cap: int

    @property
    def key(self) -> tuple[str, int]:
        return self.repo_slug, self.issue_number


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
        # Cap-exempt submits (no-agent family buckets and closed-issue
        # terminal finalizations) run on this dedicated executor so they
        # cannot queue behind cap-counted work. If they shared the main
        # pool, an exempt submit accepted past the cap would still wait
        # for a cap-counted worker to exit before the executor handed it a
        # thread, defeating the whole "exempt work always runs this tick"
        # contract. Sized INDEPENDENTLY of ``global_cap`` so a tight cap
        # (e.g. ``global_cap=1``) does not transitively cap exempt
        # throughput across repos: a deployment with N repos can have N
        # exempt buckets in flight at once even though only one ordinary
        # worker may run at a time. The fixed bound is intentionally
        # generous -- exempt handlers are fast (label / dep-graph walk, or
        # a done/rejected flip with branch cleanup; no agent), so a single
        # shared pool of this size accommodates
        # any realistic multi-repo deployment without spinning up
        # unbounded threads.
        self._exempt_executor = ThreadPoolExecutor(
            max_workers=_EXEMPT_POOL_WORKERS,
            thread_name_prefix=f"{thread_name_prefix}-exempt",
        )
        # Reentrant because `submit` holds the lock through
        # `executor.submit` + `add_done_callback`; if the worker
        # completes between those two calls, `add_done_callback` fires
        # the callback synchronously in the submitter's thread and
        # `_on_worker_done` needs to reacquire this same lock.
        self._lock = threading.RLock()
        self._active: set[tuple[str, int]] = set()
        # Per-key markers claimed via `track_active` -- the family-bucket
        # drain registers the family issue currently being processed so
        # `is_active` reports True and the pre-tick base refresh skips
        # its worktree. Kept in a SEPARATE set from `_active` so the
        # tracking claim does NOT inflate the global-cap counter (which
        # uses `len(self._active)`) or the per-repo counter. The
        # duplicate-active gate in `submit` consults BOTH sets so a
        # fanout submit for the same issue cannot slip in concurrently
        # with the bucket's in-flight iteration on that issue.
        #
        # ``submit(cap_exempt=True)`` also lands its sentinel here: the
        # exempt path skips the cap counters by design, so storing the
        # marker in ``_tracked`` keeps it visible to ``is_active`` and
        # the duplicate-active gate without inflating ``active_count``.
        # The two uses do not collide: the bucket sentinel always uses
        # issue number 0 while ``track_active`` per-iteration claims use
        # real (positive) issue numbers.
        self._tracked: set[tuple[str, int]] = set()
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
        key = (repo_slug, int(issue_number))
        with self._lock:
            return key in self._active or key in self._tracked

    # -- submit/reap/shutdown ----------------------------------------

    def submit(
        self,
        repo_slug: str,
        issue_number: int,
        fn: Callable[[], None],
        *,
        family: bool = False,
        cap_exempt: bool = False,
        per_repo_cap: Optional[int] = None,
    ) -> bool:
        """Try to dispatch ``fn`` for the given issue. Nonblocking.

        Returns True when a worker was dispatched, False when the call
        was skipped. Skip reasons (any one is sufficient):
        * scheduler is shut down,
        * the (repo_slug, issue_number) is already in flight,
        * the global active-worker cap is reached (unless ``cap_exempt``),
        * the per-repo cap (caller-provided override or default) is
          reached (unless ``cap_exempt``),
        * ``family=True`` and another family worker on this repo is in flight.

        ``cap_exempt=True`` bypasses BOTH cap checks and does not increment
        either cap counter -- the in-flight marker lands in ``_tracked``
        instead of ``_active`` so it stays visible to ``is_active`` and
        the duplicate-active gate without consuming a cap slot. The
        family mutex still applies when ``family=True`` so the exempt
        bucket cannot overlap with a concurrent family worker on the
        same repo. Used by no-agent family buckets (blocked / umbrella
        parent dep-graph walks) and closed-issue terminal finalizations
        (a merged-PR / closed-question issue's cheap done/rejected flip):
        both must always get their turn, so ordinary implementation work
        this tick cannot block them.

        The override ``per_repo_cap`` is the per-spec ``parallel_limit``
        from ``RepoSpec`` -- the issue allows different repos to declare
        different caps; the default ``per_repo_cap`` set at construction
        is the fallback for repos that did not override.
        """
        submission = _Submission(
            repo_slug=repo_slug,
            issue_number=int(issue_number),
            fn=fn,
            family=family,
            cap_exempt=cap_exempt,
            per_repo_cap=(
                self._per_repo_cap
                if per_repo_cap is None
                else max(1, int(per_repo_cap))
            ),
        )
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
            skip_reason = self._skip_reason_locked(submission)
            if skip_reason is not None:
                self._log_skip_locked(submission, skip_reason)
                return False
            self._reserve_slot_locked(submission)
            return self._start_worker_locked(submission)

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
        self._exempt_executor.shutdown(wait=wait)
        # Drain anything that completed during shutdown so the failure
        # log captures workers that raised on the way out.
        self.reap()

    # -- internals ---------------------------------------------------

    def _cap_skip_reason_locked(
        self, submission: _Submission,
    ) -> Optional[str]:
        """Return the first active cap reached by a counted submission."""
        if len(self._active) >= self._global_cap:
            return "global_cap"
        repo_active = self._per_repo_active.get(submission.repo_slug, 0)
        if repo_active >= submission.per_repo_cap:
            return "per_repo_cap"
        return None

    def _skip_reason_locked(
        self, submission: _Submission,
    ) -> Optional[str]:
        """Return the first reservation gate that rejects a submission."""
        if self._closed:
            return "closed"
        if submission.key in self._active or submission.key in self._tracked:
            return "duplicate_active"
        if not submission.cap_exempt:
            cap_reason = self._cap_skip_reason_locked(submission)
            if cap_reason is not None:
                return cap_reason
        if (
            submission.family
            and submission.repo_slug in self._family_active_repos
        ):
            return "family_slot_held"
        return None

    def _log_skip_locked(
        self, submission: _Submission, reason: str,
    ) -> None:
        """Log a rejected submission with cap context where applicable."""
        if reason == "duplicate_active":
            # Duplicate work is expected when a worker straddles ticks; other
            # skip reasons remain operator-visible at INFO.
            log.debug(
                "scheduler skip repo=%s issue=#%s reason=duplicate_active",
                submission.repo_slug, submission.issue_number,
            )
            return
        if reason == "global_cap":
            log.info(
                "scheduler skip repo=%s issue=#%s reason=global_cap "
                "(active=%d cap=%d)",
                submission.repo_slug, submission.issue_number,
                len(self._active), self._global_cap,
            )
            return
        if reason == "per_repo_cap":
            log.info(
                "scheduler skip repo=%s issue=#%s reason=per_repo_cap "
                "(active=%d cap=%d)",
                submission.repo_slug, submission.issue_number,
                self._per_repo_active.get(submission.repo_slug, 0),
                submission.per_repo_cap,
            )
            return
        log.info(
            "scheduler skip repo=%s issue=#%s reason=%s",
            submission.repo_slug, submission.issue_number, reason,
        )

    def _reserve_slot_locked(self, submission: _Submission) -> None:
        """Claim the issue, cap counters, and optional family mutex."""
        if submission.cap_exempt:
            self._tracked.add(submission.key)
        else:
            self._active.add(submission.key)
            self._per_repo_active[submission.repo_slug] += 1
        if submission.family:
            self._family_active_repos.add(submission.repo_slug)

    def _start_worker_locked(self, submission: _Submission) -> bool:
        """Submit reserved work and install its atomic release callback."""
        executor = (
            self._exempt_executor
            if submission.cap_exempt
            else self._executor
        )
        try:
            future = executor.submit(submission.fn)
        except RuntimeError:
            # Executor shutdown and the `_closed` flag are distinct gates; a
            # failed submit must release its reservation for a later retry.
            self._release_slot_locked(submission)
            return False
        future.add_done_callback(
            lambda completed_future: self._on_worker_done(
                completed_future, submission,
            )
        )
        return True

    def _release_slot_locked(
        self, submission: _Submission,
    ) -> None:
        """Drop a submission's markers. Caller holds ``self._lock``.

        ``submission.cap_exempt`` mirrors the value passed at submit time: an
        exempt submit lives in ``_tracked`` instead of ``_active`` /
        ``_per_repo_active``, so the release path symmetric to it
        clears that single set and leaves the cap counters alone.
        """
        if submission.cap_exempt:
            self._tracked.discard(submission.key)
        else:
            self._active.discard(submission.key)
            count = self._per_repo_active.get(submission.repo_slug, 0)
            if count <= 1:
                self._per_repo_active.pop(submission.repo_slug, None)
            else:
                self._per_repo_active[submission.repo_slug] = count - 1
        if submission.family:
            self._family_active_repos.discard(submission.repo_slug)

    def _on_worker_done(
        self, future: Future, submission: _Submission,
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
            self._release_slot_locked(submission)
            self._completed.append(future)

    @contextlib.contextmanager
    def track_active(
        self, repo_slug: str, issue_number: int,
    ) -> Iterator[bool]:
        """Register ``(repo_slug, issue_number)`` as in-flight for the block.

        Used by the family-bucket drain in ``_dispatch_via_scheduler``: the
        bucket itself owns the family slot via the parent submit's
        ``family=True``, but the parent submit is keyed on a sentinel
        issue number, NOT on the issue currently being processed inside
        the drain. Without per-iteration tracking, ``is_active(repo, n)``
        would report False for the family issue actually being worked on
        -- and ``_refresh_base_and_worktrees`` would race the agent by
        rebasing the worktree under the live worker.

        The marker lives in a SEPARATE set (``_tracked``) so it does NOT
        count toward the global cap (``len(self._active)``) or the
        per-repo counter. The bucket's parent submit already accounts
        for the one executor worker; folding the inner claim into the
        cap counters would let a single bucket starve unrelated fanout
        submits under tight ``global_cap`` (e.g. 2) even though only
        one worker thread is actually executing.

        Yields a bool ``claimed``: True if this call reserved the marker,
        False if the key was already in flight (active or tracked) by
        another owner. The drain must check the yielded value and skip
        ``_process_issue`` when ``claimed`` is False -- otherwise two
        workers could run the same issue handler concurrently. The
        bucket dispatch path classifies issues by a fresh label read on
        the tick thread, so within one tick this race is impossible;
        the guard catches the cross-tick window where tick N classified
        ``#X`` as fanout and submitted it, tick N+1 reclassified
        ``#X`` as family-aware (after a relabel) and folded it into the
        bucket, and the bucket reached ``#X`` before the fanout worker
        from tick N exited.
        """
        key = (repo_slug, int(issue_number))
        claimed = False
        with self._lock:
            if key not in self._active and key not in self._tracked:
                self._tracked.add(key)
                claimed = True
        try:
            yield claimed
        finally:
            if claimed:
                with self._lock:
                    self._tracked.discard(key)
