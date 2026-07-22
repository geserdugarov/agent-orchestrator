# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In-memory connection and cursor doubles for analytics sync tests."""

from __future__ import annotations

from dataclasses import dataclass, field


REFRESH_STATEMENT = "REFRESH MATERIALIZED VIEW"
Batch = tuple[str, list[tuple]]


class FakeCursor:
    """Record startup reads, refreshes, and batched inserts."""

    def __init__(self, store: "FakeConnection") -> None:
        self._store = store
        self.rowcount = 0
        self._select_rows: list[tuple] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Never suppress sync failures."""

    def execute(self, sql: str, sql_params=None) -> None:
        self._store.select_calls.append((sql, sql_params))
        if self._store.raise_on_refresh is not None and REFRESH_STATEMENT in sql.upper():
            raise self._store.raise_on_refresh
        if sql.lstrip().upper().startswith("SELECT"):
            source = self._store.pre_check_hashes
            if source is None:
                source = self._store.seen_hashes
            self._select_rows = [(content_hash,) for content_hash in sorted(source)]
        else:
            self._select_rows = []
        self.rowcount = len(self._select_rows)

    def __iter__(self):
        return iter(self._select_rows)

    def executemany(self, sql: str, params_sequence) -> None:
        if self._store.raise_on_executemany is not None:
            raise self._store.raise_on_executemany
        params_list = list(params_sequence)
        self._store.batches.append((sql, params_list))
        inserted_count = 0
        for row_params in params_list:
            content_hash = row_params[-1]
            if content_hash in self._store.seen_hashes:
                self._store.duplicate_calls.append((sql, row_params))
            else:
                self._store.seen_hashes.add(content_hash)
                self._store.inserts.append((sql, row_params))
                inserted_count += 1
        self.rowcount = inserted_count


@dataclass
class FakeConnection:
    """Capture sync persistence and transaction effects."""

    inserts: list[tuple[str, tuple]] = field(default_factory=list)
    duplicate_calls: list[tuple[str, tuple]] = field(default_factory=list)
    batches: list[Batch] = field(default_factory=list)
    select_calls: list[tuple[str, object]] = field(default_factory=list)
    seen_hashes: set[str] = field(default_factory=set)
    pre_check_hashes: set[str] | None = None
    commit_called: int = 0
    rollback_called: int = 0
    close_called: int = 0
    raise_on_executemany: Exception | None = None
    raise_on_refresh: Exception | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_called += 1

    def rollback(self) -> None:
        self.rollback_called += 1

    def close(self) -> None:
        self.close_called += 1

    def as_connect(self, _url: str) -> "FakeConnection":
        """Serve as the sync's connection factory."""
        return self


class NegativeRowcountCursor:
    """Emulate a driver that strips per-batch row counts."""

    rowcount = -1

    def __init__(self) -> None:
        self.calls: list[list[tuple]] = []

    def executemany(self, sql: str, params_sequence) -> None:
        self.calls.append(list(params_sequence))


class RejectingBatchCursor:
    """Fail if an empty-batch path invokes the driver."""

    rowcount = 0

    def executemany(self, sql: str, params_sequence) -> None:
        raise AssertionError("executemany must not run on empty batch")
