# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Process-local scheduler for concurrent per-issue handlers.

``IssueScheduler`` coordinates two executor pools while the typed submission,
state-inspection, reservation, and execution responsibilities live in focused
private leaves. The historical positional/keyword ``submit`` API remains
available beside ``SubmissionRequest``.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor

from orchestrator import _scheduler_mixins, _scheduler_submission

SchedulerExecutionMixin = _scheduler_mixins.SchedulerExecutionMixin
_Submission = _scheduler_submission.Submission
SubmissionRequest = _scheduler_submission.SubmissionRequest

_EXEMPT_POOL_WORKERS = 32


class IssueScheduler(SchedulerExecutionMixin):
    """Long-lived scheduler shared by every repository polling tick."""

    def __init__(
        self,
        *,
        global_cap: int,
        per_repo_cap: int,
        thread_name_prefix: str = "orch-worker",
    ) -> None:
        self._global_cap = max(1, int(global_cap))
        self._per_repo_cap = max(1, int(per_repo_cap))
        self._executor = ThreadPoolExecutor(
            max_workers=self._global_cap,
            thread_name_prefix=thread_name_prefix,
        )
        self._exempt_executor = ThreadPoolExecutor(
            max_workers=_EXEMPT_POOL_WORKERS,
            thread_name_prefix=f"{thread_name_prefix}-exempt",
        )
        self._lock = threading.RLock()
        self._active: set[tuple[str, int]] = set()
        self._tracked: set[tuple[str, int]] = set()
        self._per_repo_active: dict[str, int] = defaultdict(int)
        self._family_active_repos: set[str] = set()
        self._completed: list[Future] = []
        self._closed = False
