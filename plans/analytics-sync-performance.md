# Analytics sync — performance design

## Context

Issue #366: `python -m orchestrator.analytics.sync` against the remote
Postgres at `100.121.198.94:5007` is too slow to be operationally useful.
Observed timings:

```text
progress lines=500  inserted=0 duplicate=500  malformed=0 elapsed=107.359s
progress lines=1000 inserted=0 duplicate=1000 malformed=0 elapsed=212.418s
```

That is ~210 ms per row at the wire, with every row landing as a duplicate
because the database already has the content hash. Extrapolated, 81k records
take ~5 hours — even though zero rows actually need to be written.

This plan answers three things:

1. How the sync works today (per-record loop) and why that's the bottleneck.
2. A phased design for batching and a server-side dedup pre-check.
3. What to measure and in what order, so we keep the simplest change that
   makes the sync fast enough rather than rewriting the whole thing on a
   guess.

## How it works today

Single function: `sync_jsonl_to_postgres` in
`orchestrator/analytics/sync.py:341`. The hot path is one Python loop over
the JSONL file, with no batching or pre-check:

1. Open one psycopg connection (one TCP / TLS handshake — accounted for in
   the "connection established" log line).
2. For every line in the file:
   * Strip / parse JSON, validate required keys (`_split_row`,
     `sync.py:165`).
   * Compute `sha256(json.dumps(record, sort_keys=True))` →
     `content_hash` (`sync.py:141`).
   * Build the parameter tuple and call `cur.execute(insert_sql, values)`
     (`sync.py:464`). The SQL is a single-row `INSERT ... ON CONFLICT
     (content_hash) DO NOTHING` against `analytics_events`
     (`sync.py:222-226`).
   * Inspect `cur.rowcount` to decide whether the row counted as
     `inserted` or `skipped_duplicate` (`sync.py:471`).
3. Emit a `progress` log line every 500 lines (`_PROGRESS_INTERVAL =
   500`, `sync.py:61`).
4. After the loop, `conn.commit()` runs once at the end (`sync.py:485`).
   The whole replay is a single Postgres transaction.

So: **each record is processed separately on the wire** — one
`cur.execute` per row, which translates to one Postgres protocol round-trip
per row even though every row carries identical SQL text. The single
end-of-run commit keeps WAL flushes off the per-row path, but the round-
trip latency to the remote host stays on it.

Why this dominates wall-clock:

* The numbers in the issue work out to ~210 ms per row. A SHA-256 over a
  few hundred bytes of JSON plus a JSON parse is microseconds — call it
  zero. Postgres' lookup against the unique index
  `analytics_events_content_hash_idx` is sub-millisecond. Everything else
  is the round-trip to the Postgres host plus per-statement parse / bind /
  exec overhead.
* Dedup work is **on the wire** today: every row is sent to Postgres
  even when its `content_hash` already exists. `ON CONFLICT DO NOTHING`
  protects the table from a duplicate write but doesn't save the network
  hop or the per-row protocol overhead, which is exactly the cost that's
  hurting us.
* Run-shape evidence: at lines=1000 the log shows `inserted=0 duplicate=
  1000`. The sync is paying full per-row latency to learn that every row
  is a no-op. That is the worst-case shape and also the most common one
  in steady state (re-sync after a prune, repeat sync between log
  rotations).

## Proposed design

Two changes, sequenced so each one's effect is measurable. The simplest
change goes first; the second only lands if measurements show Phase 1
isn't enough.

Constraints kept verbatim from the current behavior:

* `content_hash` stays the dedup key. `_canonical_json` /
  `_PROMOTED_COLUMNS` / `_REQUIRED_KEYS` / malformed-line tolerance are
  contracts we don't touch.
* Sync stays operator-driven and stays a no-op when env vars are unset
  or the file is absent (`sync.py:379-387`).
* Progress logging keeps the same operator-visible shape (lines,
  inserted, duplicate, malformed, elapsed); it just emits per batch
  instead of per `_PROGRESS_INTERVAL` lines.
* The single-transaction commit-at-end semantics stay. A mid-run crash
  rolls everything back, same as today.

### Phase 1 — Batch the INSERTs

Replace the per-row `cur.execute(insert_sql, values)` with a per-batch
flush. Concretely:

* Accumulate rows in a Python list as the loop iterates the JSONL file.
* Every `BATCH_SIZE` rows (target: 500 or 1000; pick by measurement),
  call `cur.executemany(insert_sql, batch)` and clear the buffer. Flush
  one final partial batch at EOF.
* Keep `ON CONFLICT (content_hash) DO NOTHING` — server-side dedup
  remains the correctness backstop.

Why this is the right first change:

* psycopg3's `cursor.executemany` against a parameterised statement
  issues the rows as a single protocol pipeline rather than N
  independent round-trips. A round-trip per ~500 rows instead of per
  row collapses the dominant cost by roughly 2-3 orders of magnitude
  for this workload — the projected 5h drops into the minutes range
  even without any other change.
* Memory footprint is bounded: one batch worth of rows in Python
  (kilobytes), no server-side state.
* Failure semantics get slightly coarser: a malformed batch aborts the
  transaction. Since malformed lines are filtered in Python *before*
  they enter the batch (`_split_row` returns `None`, the line is
  counted as `skipped_malformed`), the only path that can fail server-
  side is a row that violates a constraint we didn't anticipate — and
  the same failure exists today. The current `try/except` around the
  loop already rolls back and re-raises (`sync.py:486-491`).

Observability change:

* `inserted` / `skipped_duplicate` precision: today we read
  `cur.rowcount` per `execute`. After batching, `cur.rowcount` reports
  the per-`executemany` total, so we can compute `inserted_in_batch =
  rowcount`, `duplicate_in_batch = len(batch) - rowcount`. Per-row
  precision is preserved at batch boundaries; we just don't know which
  specific row in the batch was the duplicate. That's fine — the
  forensic key already in the schema (`source_path`, `source_line`,
  `content_hash`) is what an operator would use to chase down a
  specific record, not the per-row log line.

Measurement gate: run the batched implementation against the same 81k
file and the same remote host. If the run completes in single-digit
minutes, **stop here**. The pre-check in Phase 2 is only worth its
complexity if Phase 1 isn't fast enough.

### Phase 2 — Skip already-present rows before they hit the wire

If Phase 1's wall-clock is still uncomfortable (most likely shape: a
multi-MB file that is 100% duplicates against a database that already
has them all), add a startup pre-check that pulls the existing
`content_hash` set into Python and filters records before they enter
the batch:

* At the top of `sync_jsonl_to_postgres`, before opening the input
  file:
  ```python
  cur.execute(
      "SELECT content_hash FROM analytics_events "
      "WHERE content_hash IS NOT NULL"
  )
  existing = {row[0] for row in cur}
  ```
  This is one server-side scan over the unique index
  `analytics_events_content_hash_idx`. The wire payload is ~64 bytes
  per hash; 81k rows is ~5 MB streamed once. Python set membership is
  O(1) per lookup.
* In the loop, compute `content_hash` *before* `_split_row` /
  parameter-tuple construction. If it's already in `existing`, count
  it as `skipped_duplicate` and continue without touching the batch.
* Add the hash to `existing` after queueing the row, so two duplicate
  rows in the *same file* are deduped in-Python rather than going to
  the wire twice — small saving but free.

Why this composes cleanly with Phase 1:

* It is purely additive: the existing batched INSERT path stays the
  authoritative dedup mechanism. The pre-check just moves the common
  case off the wire.
* Steady-state cost of a "nothing new" re-sync drops to: one SELECT,
  one Python pass over the file, no INSERTs at all.
* Worst case (new file with no overlap): the pre-check still runs but
  loads an empty / mostly-empty set, then every row gets batched —
  same total work as Phase 1.

Failure modes worth calling out, with mitigations:

* **Concurrent writer races the pre-check.** Not in scope today — the
  sync is operator-driven, single-process (`docs/observability.md` and
  the docstring at `sync.py:33-38`). Even if a concurrent writer
  appeared later, `ON CONFLICT DO NOTHING` on the batched INSERT is
  still the backstop; a race only causes a row to be sent over the
  wire that turns out to be a no-op, which is exactly today's
  behavior.
* **Memory pressure from large tables.** 5 MB / 81k rows is fine. At
  10× scale (810k rows, ~50 MB of hashes) we'd still be inside what a
  long-running operator CLI can hold. If this ever becomes a concern,
  the pre-check can be replaced by a bloom filter or by a date-bounded
  query (`WHERE ts >= file_min_ts`), but neither is worth building
  speculatively.
* **NULL hashes from pre-`content_hash` rows.** Filtered by the
  `WHERE content_hash IS NOT NULL` predicate above; legacy rows don't
  pollute the set.

### Phase 3 (rejected for now, documented for completeness)

`COPY ... FROM STDIN` (binary or CSV) is faster than batched INSERT —
single statement, server-side bulk path, no per-row parse. It does not
honour `ON CONFLICT DO NOTHING`, so it needs either:

* a temporary staging table + a follow-on `INSERT ... SELECT ... FROM
  staging ON CONFLICT DO NOTHING`, or
* Phase 2's pre-check to guarantee the input contains no duplicates.

Either route adds non-trivial complexity (staging-table DDL, two-phase
transaction, larger blast radius on partial failure). Defer until
Phase 1 + Phase 2 measurements show the batched INSERT is still the
bottleneck. The most likely outcome is that Phase 1 alone is already
fast enough for the steady-state workload.

## What to keep untouched

Out of scope for this design:

* The `analytics_events` table shape, indexes, view, or unique
  constraint. The pre-check uses the existing
  `analytics_events_content_hash_idx`.
* The JSONL writer (`analytics.append_record`) and its canonical JSON
  form. Both ends of the round-trip share the same `sort_keys=True`
  encoding; changing one breaks dedup.
* CLI surface and env-var configuration. `--log-path`, `--db-url`,
  `--log-level` stay as-is; the implementation-only knob `BATCH_SIZE`
  lives as a module constant alongside `_PROGRESS_INTERVAL`, not as a
  CLI flag, until measurements show we need to tune it per host.
* Wiring the sync into the polling tick. Still operator-driven, per
  the docstring at `sync.py:33-38`.

## Implementation sketch (Phase 1 first, Phase 2 conditionally)

`orchestrator/analytics/sync.py`:

* Add `_BATCH_SIZE = 500` next to `_PROGRESS_INTERVAL`.
* Replace the per-row `cur.execute(insert_sql, values)` with an
  accumulator. Flush via `cur.executemany(insert_sql, batch)` at
  `len(batch) == _BATCH_SIZE` and once more after the loop. Maintain
  per-batch `inserted` / `skipped_duplicate` deltas off
  `cur.rowcount`.
* Move the `total_lines % _PROGRESS_INTERVAL == 0` check to fire after
  each batch flush instead of after each row.

If measurements demand Phase 2:

* Before opening the input file, fetch existing hashes into a Python
  set as above.
* In the loop, hash first, set-check, skip-or-batch.

`tests/`:

* Extend the fake psycopg cursor / connection in
  `tests/test_analytics_sync.py` (or wherever the sync tests live —
  pick by reading the existing test module's name) to record
  `executemany` calls. Cover:
  * Mixed-result batch: half inserted, half duplicate via a fake
    `rowcount`.
  * Final partial batch flush at EOF.
  * Malformed lines still skipped and counted before they reach the
    batch buffer.
  * (Phase 2) Pre-check populates the in-Python skip set; a row whose
    hash is in the set never reaches `executemany`.

`docs/observability.md`:

* Update the analytics-sync section so the per-batch log shape and the
  optional pre-check are mentioned alongside today's per-record
  description.

No schema migration. No new dependencies (`psycopg.Cursor.executemany`
is part of psycopg3, which `pyproject.toml` already pins).

## Recommendation

Land Phase 1 alone first and re-measure. The single change of swapping
`execute` → `executemany` is small, low-risk, and capable of taking the
projected 5-hour run into the minutes. Reserve the pre-check (Phase 2)
for the case where steady-state "nothing new" re-syncs still feel slow
after Phase 1.
