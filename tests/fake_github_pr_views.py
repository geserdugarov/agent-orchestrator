# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Read-only views over pull-request fake state and histories."""
from __future__ import annotations

from tests.fake_models import FakePR


class _PullHistoryView:
    @property
    def posted_pr_comments(self) -> list[tuple[int, str]]:
        return self._pull_history._posted_pr_comments

    @property
    def opened_prs(self) -> list[FakePR]:
        return self._pull_history._opened_prs

    @property
    def merge_calls(self) -> list[tuple[int, str, str]]:
        return self._pull_history._merge_calls

    @property
    def deleted_remote_branches(self) -> list[str]:
        return self._pull_history._deleted_remote_branches


class _PullStateView:
    @property
    def existing_open_pr(self) -> dict[str, FakePR]:
        return self._pull_state._existing_open_pr

    @property
    def pulls(self) -> dict[int, FakePR]:
        return self._pull_state._pulls

    @property
    def merge_returns_ok(self) -> bool:
        return self._pull_state._merge_returns_ok

    @property
    def delete_remote_branch_returns_ok(self) -> bool:
        return self._pull_state._delete_remote_branch_returns_ok
