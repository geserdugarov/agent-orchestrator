# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""State inspection, reservation, and execution layers for the scheduler."""
from __future__ import annotations

import contextlib
import logging
from concurrent.futures import Future
from typing import Any, Iterator, Optional

from orchestrator import _scheduler_submission

log = logging.getLogger("orchestrator.scheduler")


class SchedulerViewMixin:
    """Read-only scheduler state and temporary active tracking."""

    @property
    def global_cap(self) -> int:
        return self._global_cap

    @property
    def per_repo_cap(self) -> int:
        return self._per_repo_cap

    def active_count(self, repo_slug: Optional[str] = None) -> int:
        """Return counted in-flight workers, globally or for one repo."""
        with self._lock:
            if repo_slug is None:
                return len(self._active)
            return self._per_repo_active.get(repo_slug, 0)

    def is_active(self, repo_slug: str, issue_number: int) -> bool:
        """Return whether a counted or tracked claim owns an issue key."""
        issue_key = (repo_slug, int(issue_number))
        with self._lock:
            return issue_key in self._active or issue_key in self._tracked

    @contextlib.contextmanager
    def track_active(
        self,
        repo_slug: str,
        issue_number: int,
    ) -> Iterator[bool]:
        """Temporarily claim an issue without consuming a cap slot."""
        issue_key = (repo_slug, int(issue_number))
        claimed = False
        with self._lock:
            if issue_key not in self._active and issue_key not in self._tracked:
                self._tracked.add(issue_key)
                claimed = True
        try:
            yield claimed
        finally:
            if claimed:
                with self._lock:
                    self._tracked.discard(issue_key)


class SchedulerReservationMixin(SchedulerViewMixin):
    """Atomic slot admission, logging, and release."""

    def _cap_skip_reason_locked(
        self,
        submission: _scheduler_submission.Submission,
    ) -> Optional[str]:
        if len(self._active) >= self._global_cap:
            return "global_cap"
        repo_active = self._per_repo_active.get(submission.repo_slug, 0)
        if repo_active >= submission.per_repo_cap:
            return "per_repo_cap"
        return None

    def _skip_reason_locked(
        self,
        submission: _scheduler_submission.Submission,
    ) -> Optional[str]:
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
        self,
        submission: _scheduler_submission.Submission,
        reason: str,
    ) -> None:
        if reason == "duplicate_active":
            log.debug(
                "scheduler skip repo=%s issue=#%s reason=duplicate_active",
                submission.repo_slug,
                submission.issue_number,
            )
            return
        if reason == "global_cap":
            log.info(
                "scheduler skip repo=%s issue=#%s reason=global_cap "
                "(active=%d cap=%d)",
                submission.repo_slug,
                submission.issue_number,
                len(self._active),
                self._global_cap,
            )
            return
        if reason == "per_repo_cap":
            log.info(
                "scheduler skip repo=%s issue=#%s reason=per_repo_cap "
                "(active=%d cap=%d)",
                submission.repo_slug,
                submission.issue_number,
                self._per_repo_active.get(submission.repo_slug, 0),
                submission.per_repo_cap,
            )
            return
        log.info(
            "scheduler skip repo=%s issue=#%s reason=%s",
            submission.repo_slug,
            submission.issue_number,
            reason,
        )

    def _reserve_slot_locked(
        self,
        submission: _scheduler_submission.Submission,
    ) -> None:
        if submission.cap_exempt:
            self._tracked.add(submission.key)
        else:
            self._active.add(submission.key)
            self._per_repo_active[submission.repo_slug] += 1
        if submission.family:
            self._family_active_repos.add(submission.repo_slug)

    def _release_slot_locked(
        self,
        submission: _scheduler_submission.Submission,
    ) -> None:
        if submission.cap_exempt:
            self._tracked.discard(submission.key)
        else:
            self._active.discard(submission.key)
            active_count = self._per_repo_active.get(submission.repo_slug, 0)
            if active_count <= 1:
                self._per_repo_active.pop(submission.repo_slug, None)
            else:
                self._per_repo_active[submission.repo_slug] = active_count - 1
        if submission.family:
            self._family_active_repos.discard(submission.repo_slug)


class SchedulerExecutionMixin(SchedulerReservationMixin):
    """Worker dispatch, completion draining, and shutdown coordination."""

    def submit(self, *args: Any, **kwargs: Any) -> bool:
        """Dispatch a typed request or the historical submit call shape."""
        request = _scheduler_submission.bind_submission_request(args, kwargs)
        submission = _scheduler_submission.normalize_submission(
            request,
            self._per_repo_cap,
        )
        with self._lock:
            skip_reason = self._skip_reason_locked(submission)
            if skip_reason is not None:
                self._log_skip_locked(submission, skip_reason)
                return False
            self._reserve_slot_locked(submission)
            return self._start_worker_locked(submission)

    def reap(self) -> int:
        """Drain completed futures and log worker exceptions."""
        with self._lock:
            drained_futures = self._completed
            self._completed = []
        for future in drained_futures:
            error = future.exception()
            if error is not None:
                log.error("scheduler worker raised", exc_info=error)
        return len(drained_futures)

    def shutdown(self, *, wait: bool = True) -> None:
        """Close submission, stop both executors, and drain completions."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)
        self._exempt_executor.shutdown(wait=wait)
        self.reap()

    def _start_worker_locked(
        self,
        submission: _scheduler_submission.Submission,
    ) -> bool:
        executor = (
            self._exempt_executor
            if submission.cap_exempt
            else self._executor
        )
        try:
            future = executor.submit(submission.fn)
        except RuntimeError:
            self._release_slot_locked(submission)
            return False
        future.add_done_callback(
            lambda completed_future: self._on_worker_done(
                completed_future,
                submission,
            ),
        )
        return True

    def _on_worker_done(
        self,
        future: Future,
        submission: _scheduler_submission.Submission,
    ) -> None:
        with self._lock:
            self._release_slot_locked(submission)
            self._completed.append(future)


SchedulerExecutionMixin.submit.__signature__ = (
    _scheduler_submission._SUBMIT_METHOD_SIGNATURE
)
