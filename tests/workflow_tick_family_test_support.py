# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Family-scheduling probes for workflow tick tests."""
from __future__ import annotations

import threading
import time

from tests import workflow_tick_parallel_test_support as support


class _SlowFamilyProbe:
    def __init__(self, *, fanout_count: int) -> None:
        self.fanout_done: list[int] = []
        self.released_after_fanout = False
        self._expected_fanout = fanout_count
        self._family_holding = threading.Event()
        self._family_release = threading.Event()
        self._lock = threading.Lock()

    def process(self, _gh, _spec, issue) -> None:
        if issue.number == 1:
            self._family_holding.set()
            self._family_release.wait(timeout=support._SYNC_TIMEOUT_SECONDS)
            return
        with self._lock:
            self.fanout_done.append(issue.number)

    def release_after_fanout(self) -> None:
        if not self._family_holding.wait(timeout=support._SYNC_TIMEOUT_SECONDS):
            self._family_release.set()
            return
        deadline = time.monotonic() + support._SYNC_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.fanout_done) == self._expected_fanout:
                    self.released_after_fanout = True
                    break
            time.sleep(support._FAMILY_POLL_DELAY_SECONDS)
        self._family_release.set()

    def cleanup(self, thread: threading.Thread) -> None:
        self._family_release.set()
        thread.join(timeout=support._SYNC_TIMEOUT_SECONDS)


def _seed_overlap_issues(github: support.FakeGitHubClient) -> None:
    issue_labels = (
        (1, support.LABEL_DECOMPOSING),
        (2, support.LABEL_BLOCKED),
        (4, support.LABEL_UMBRELLA),
        (5, None),
        (support._FANOUT_ISSUE_NUMBER, support.LABEL_IMPLEMENTING),
    )
    for issue_number, label in issue_labels:
        github.add_issue(support.make_issue(issue_number, label=label))


def _flaky_workflow_label(_client, issue):
    if getattr(issue, "number", None) == 2:
        raise RuntimeError("simulated label-read failure")
    return support._ORIGINAL_WORKFLOW_LABEL(issue)


def _simulate_family_child_state(client, _spec, issue) -> None:
    if issue.number == 10:
        state = client.read_pinned_state(issue)
        for child_number in state.get("children") or []:
            child = client.get_issue(int(child_number))
            child_state = client.read_pinned_state(child)
            if not child_state.get(support.KEY_PARENT_NUMBER):
                child_state.set(support.KEY_PARENT_NUMBER, issue.number)
                child_state.set(support.KEY_AWAITING_HUMAN, False)
                child_state.set(support.KEY_PARK_REASON, None)
                client.write_pinned_state(child, child_state)
        client.set_workflow_label(issue, support.LABEL_BLOCKED)
        client.write_pinned_state(issue, state)
        return
    child_state = client.read_pinned_state(issue)
    if child_state.get(support.KEY_PARENT_NUMBER) or child_state.get(support.KEY_AWAITING_HUMAN):
        return
    child_state.set(support.KEY_AWAITING_HUMAN, True)
    child_state.set(support.KEY_PARK_REASON, "blocked_no_children")
    client.write_pinned_state(issue, child_state)
