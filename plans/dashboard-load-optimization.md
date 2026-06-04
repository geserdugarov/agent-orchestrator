# Dashboard load-time optimization

## Symptom

A cold visit to `http://localhost:8501/` (Streamlit dashboard,
`orchestrator/dashboard.py`) takes ~25-30 s before the page paints. Every
filter change / refresh re-pays the same cost when the 60 s cache window
expires (`@st.cache_data(ttl=60)`).

## Root cause: yes, it is Postgres I/O — but not the data volume.

The database is small (~79 k rows, 51 MB on disk, all indexes hot). A
single query that touches the entire table returns in 200-500 ms. The
problem is not the *amount* of data the dashboard pulls; it is the
**number of round-trips** and the **per-trip overhead** against a
non-local Postgres.

Measured against a remote Postgres reached over a VPN-style overlay
network from the dashboard host:

| Operation                        | Time     |
| -------------------------------- | -------- |
| TCP connect to Postgres host     | ~213 ms  |
| `psycopg.connect()` + close      | ~1070 ms |
| `SELECT 1` (cursor re-used conn) | ~209 ms  |
| Whole-table aggregate (~79 k rows) | 200-500 ms |

Per-query network RTT is ~209 ms; full connection setup adds another
~860 ms on top because the handshake costs ~5 round-trips. **Local
in-DB execution time is dominated by network latency by an order of
magnitude.**

### What the dashboard does on every cold load

`main()` in `orchestrator/dashboard.py` issues the following
synchronously, one after the other, **each opening a fresh connection**
in `orchestrator/analytics/read.py::_query` (which calls
`_default_connect` then `conn.close()` per call):

1. `get_data_extent` — 1 query
2. `get_filter_options` — **5 queries**, one per dropdown column
   (`repo`, `event`, `stage`, `backend`, `agent_role`), each a
   `SELECT DISTINCT … FROM analytics_events`
3. `get_summary` × 2 (current + previous window for KPI deltas) —
   each one fires **3 queries** (totals, by-event, by-stage) → **6 queries**
4. `get_time_series` — 1
5. `get_stage_breakdown` — 1
6. `get_recent_agent_exits` — 1
7. `get_top_cost_issues` (`get_issues`) — 1
8. `get_review_round_breakdown` — 1
9. `get_backend_efficiency` — 1
10. `get_repo_breakdown` — 1
11. `get_cost_coverage` — 1
12. `get_backend_daily_tokens` — 1
13. `get_hourly_heatmap` — 1
14. `get_throughput_breakdown` — 1

**Total: ~24 SQL statements, each carrying a fresh connection
handshake.**

### Back-of-envelope cost model

- 24 × ~1070 ms connect overhead ≈ **25.7 s** burned in the TCP/TLS/auth
  handshake.
- ~4 s of actual SQL execution + result transfer (measured sum of all
  windowed queries was 3.96 s; the 5 distinct-column queries add ~1 s).
- → **~30 s end-to-end** on a cold load — matches the observed symptom.

If the same 24 statements ran on a single pooled connection, the
estimated cost drops to:
- 1 × ~1070 ms connect + 23 × ~209 ms RTT + 4 s SQL ≈ **~10 s**

If the dashboard also batched related aggregates into fewer round-trips
(see Layer 3 below): **~3-5 s**.

### Secondary findings

- `get_summary` issues 3 separate aggregates that all scan the same
  windowed row-set; they can collapse into one.
- `get_filter_options` runs 5 independent `SELECT DISTINCT` against the
  same table; each costs a full RTT + a partial scan.
- Both `get_summary` and `get_time_series` re-scan the full table when
  the user is on the default `All` preset and there is no repo filter,
  yet 96 % of rows are `stage_evaluation` events the page never plots —
  the time-series filters by `event`, the summary needs the totals only
  for KPIs.
- `COUNT(DISTINCT (repo, issue))` in `get_summary` is a row-constructor
  count-distinct, which Postgres cannot satisfy with a simple aggregate
  and falls back to a hash/sort. Cheap at 79 k rows, expensive at 10 M.
- The dashboard reads the **previous window**'s summary just to render
  KPI delta pills. That doubles the most expensive summary query for a
  small visual.
- Streamlit's `@st.cache_data(ttl=60)` keys on the window + filters; a
  user adjusting a date input invalidates *all* entries because the key
  changes, so the cache helps for static views but not for exploration.

## Proposed optimizations (in priority order)

Layers 1-2 are the highest-leverage changes and should land first.
Layers 3-5 are follow-ups that compound the wins.

### Layer 1 — Stop opening a connection per query (single biggest win)

`orchestrator/analytics/read.py::_query` creates and closes a
`psycopg.Connection` for every call. With remote Postgres at ~1 s per
handshake, this dwarfs everything else.

**Change:** introduce a per-request (or per-Streamlit-session)
connection scope:

```python
# orchestrator/analytics/read.py
@contextmanager
def analytics_connection(connect=None, db_url=None) -> Iterator[Any]:
    url = _resolve_db_url(db_url)
    conn = (connect or _default_connect)(url)
    try:
        yield conn
    finally:
        conn.close()

def _query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall() or [])
```

Then in `dashboard.py::main()`:

```python
with analytics_read.analytics_connection() as conn:
    extent = analytics_read.get_data_extent(conn=conn)
    options = analytics_read.get_filter_options(conn=conn)
    summary = _read_summary(*key, _conn=conn)
    ...
```

All `get_*` helpers grow an optional `conn` parameter; when set, they
re-use it instead of calling `_default_connect`. Tests pass a fake
connection just like today (the fake connect-fn shape stays for
backward compatibility).

**Streamlit caveat.** The dashboard's `_read_*` wrappers in
`orchestrator/dashboard.py` (`_read_summary`, `_read_time_series`,
`_read_stage_breakdown`, etc., around lines 1024-1144) are
`@st.cache_data`-decorated and key on all positional/keyword args.
A raw `psycopg.Connection` is not a hashable cache key and would
either crash the wrapper or, worse, treat every new connection as a
cache miss. Two viable patterns; pick one when implementing Layer 1:

1. **Underscore-prefix the connection arg** so Streamlit excludes it
   from the cache key: `def _read_summary(start, end, …, *, _conn)`.
   Streamlit's `cache_data` documents that args whose names start with
   `_` are not hashed.
2. **Keep the cached wrappers connection-free** and push connection
   scoping one level out: the wrapper holds onto a module-level
   thread-local connection (lazily opened on first use, closed on
   Streamlit-session teardown) and checks out / returns the connection
   inside the cached function body. The cache then keys on only the
   filter tuple, which is what we want anyway.

Pattern (2) is the cleaner fit because the cached wrappers already
exist for the express purpose of memoizing on the filter tuple; the
connection is an implementation detail of the read, not a logical
input. The plan below assumes (2) unless explicitly noted.

**Expected speedup:** 24 × 1.07 s → 1 × 1.07 s + 23 × ~0.21 s. Saves
**~20 s** off cold load.

Even better: a process-wide pool so the first user pays the 1 s
handshake and subsequent reloads pay nothing. `pyproject.toml` only
installs `psycopg[binary]`; `psycopg_pool` is a separate distribution
and is not currently a runtime dep. Two options for the pool:

- **Pool-free:** a single module-level thread-local connection (one
  per dashboard worker thread) with a manual reconnect on
  `psycopg.OperationalError`. No new dependency, ~30 lines of code,
  loses the auto-recycling and `max_lifetime` knobs a real pool gives
  you but is sufficient for the single-tenant local dashboard.
  Must open the connection with `autocommit=True` (or wrap every
  `_query` in a `rollback`-on-failure / commit-on-success guard) —
  the current `_query` is safe only because it closes the connection
  after every call, so successful SELECTs implicitly drop their
  transaction and failed ones go away with the socket. A persistent
  connection inherits psycopg's default "implicit transaction on
  first statement" behavior, which would leave the session idle in
  transaction after every SELECT (holding xmin and blocking vacuum)
  and, on any query error, in `aborted` state — every subsequent
  read on the same thread-local would then raise
  `InFailedSqlTransaction` until something rolled it back. Autocommit
  avoids both. If a future change needs an explicit transaction (it
  shouldn't — this path is read-only), wrap it in
  `with conn.transaction():` rather than disabling autocommit
  globally. The reconnect handler must also close-and-replace the
  thread-local on any `OperationalError` *or* `InterfaceError` so a
  broken socket does not get reused for the next read.
- **Pooled:** add `psycopg_pool>=3.2` to `dependencies` in
  `pyproject.toml` (and refresh `uv.lock`), then use
  `psycopg_pool.ConnectionPool(min_size=1, max_size=4)`. Justify the
  new dep in the PR description per the "no new deps without
  justification" rule in `CLAUDE.md`.

Start with the pool-free path in PR 1 to keep the change scope tight;
revisit the pooled variant only if connection churn becomes a
measurable issue under multi-user load (the dashboard is currently
single-tenant).

### Layer 2 — Issue independent reads in parallel

Once Layer 1 is in place the dominant cost is the remaining 23 × 209 ms
sequential RTTs (~5 s). All 13 read functions are independent — they
take the same filter set and return disjoint result types.

**Change:** dispatch the read fan-out with a small `ThreadPoolExecutor`
in `dashboard.py::main()`:

```python
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=8) as pool:
    fut_summary       = pool.submit(_read_summary, *key)
    fut_prev_summary  = pool.submit(_read_summary, *prev_key)
    fut_ts            = pool.submit(_read_time_series, *key)
    ...
summary       = fut_summary.result()
prev_summary  = fut_prev_summary.result()
...
```

Each thread takes its own pooled connection. With 8 in-flight queries,
the wall-clock collapses to roughly the slowest single query in each
"wave" of 8 + Python overhead.

**Expected speedup:** 5 s sequential → ~1 s parallel. Saves **~4 s**.

`psycopg.Connection` is not thread-safe for concurrent use; use one
connection per thread via the pool, do not share a single
`Connection` between threads.

### Layer 3 — Collapse multi-query readers into single round-trips

Three reader functions issue more than one SQL statement under the
hood, each paying a separate RTT:

- `get_summary` — totals + by_event + by_stage → can be **one** query
  using `WITH t AS (… )` CTE or a single grouped query whose rows are
  reduced in Python.
- `get_filter_options` — 5 separate `SELECT DISTINCT` → can be **one**
  query that unions the five distinct columns:

  ```sql
  SELECT 'repo'       AS dim, repo       AS value FROM analytics_events WHERE repo       IS NOT NULL
  UNION SELECT 'event',      event       FROM analytics_events WHERE event      IS NOT NULL
  UNION SELECT 'stage',      stage       FROM analytics_events WHERE stage      IS NOT NULL
  UNION SELECT 'backend',    backend     FROM analytics_events WHERE backend    IS NOT NULL
  UNION SELECT 'agent_role', agent_role  FROM analytics_events WHERE agent_role IS NOT NULL
  ORDER BY dim, value
  ```

  with the result bucketed in Python by `dim`. The trailing
  `ORDER BY dim, value` preserves the per-column ascending order that
  `_distinct_strings` produces today
  (`orchestrator/analytics/read.py::_distinct_strings`) and that the
  dashboard / tests rely on
  (`tests/test_analytics_read.py::FilterOptionsTest`). If the planner
  later prefers an unordered union for cost reasons, sort each bucket
  in Python after the fetch — the lists are tiny (a few hundred values
  at most). Each leg already hits a column-specific partial scan in
  ~210 ms; one statement still beats five RTTs.

- KPI-delta reads (`prev_summary`) — the dashboard needs only
  `total_cost_usd`, `total_tokens` (4 columns), `total_agent_runs`. A
  dedicated `get_kpi_prev(start, end, …)` that returns just these
  scalars beats reusing the full `get_summary` shape.

**Expected speedup:** ~2 extra RTTs eliminated × 0.21 s ≈ **0.4 s**, plus
half the Postgres CPU for these widgets.

### Layer 4 — Pre-aggregate hot rollups (daily materialized view)

Once the table grows past ~1 M rows the per-window aggregates will
start eating into the budget Layers 1-3 freed up. Two options, in
ascending order of operational cost:

- **Daily-rollup materialized view** keyed on `(day, repo, issue,
  event, stage, backend, cost_source)` carrying `SUM(cost_usd)`,
  `SUM(input_tokens)`, `SUM(output_tokens)`, `SUM(cache_read_tokens)`,
  `SUM(cache_write_tokens)`, `COUNT(*)`,
  `SUM(CASE WHEN exit_code <> 0 THEN 1 ELSE 0 END)`,
  `SUM(CASE WHEN event = 'agent_exit' AND timed_out = true THEN 1
  ELSE 0 END)` (so the `Summary.timed_out_agent_runs` KPI
  surfaced in `orchestrator/dashboard.py`'s reliability tiles can
  read from the rollup instead of falling back to `analytics_events`),
  `SUM(duration_s)`, and `SUM(CASE WHEN duration_s IS NOT NULL THEN 1
  ELSE 0 END)` (so consumers can recover `AVG(duration_s)` as
  `SUM/COUNT`). The `issue` column has to be in the key because every
  dashboard read accepts an `issue_filter`
  (`orchestrator/dashboard.py::main` builds `key`/`prev_key` with
  `issue_filter`); without it, an issue-scoped view double-counts.
  The duration sum + count are required for backend efficiency
  (`orchestrator/analytics/read.py::get_backend_efficiency` emits
  `AVG(duration_s)`); plain `SUM` is unsafe because averaging averages
  across days does not preserve the row-weighted mean.

  With those additions, KPIs, time series, stage breakdown, repo
  breakdown, backend efficiency, and throughput can read from this
  table instead of `analytics_events`, dropping a 79 k-row scan to a
  ~few-hundred-row scan. Refresh nightly or after each
  `analytics.sync` cycle (the sync job already has the wakeup).
- **Raw-table fallback for survivors.** Widgets that genuinely need
  per-row resolution — the hourly heatmap, recent agent exits, the
  top-cost issues drill-down, and the review-round breakdown — keep
  hitting `analytics_events` / `analytics_agent_runs` directly. Cover
  these with `pg_stat_statements`-driven manual indexes if they
  surface in the dashboard's slow-query log after the rollup cutover.

A daily rollup is the canonical pattern for this dashboard shape and
keeps the raw events queryable for the drill-down view, which is the
only widget that genuinely needs per-row resolution.

**Expected speedup:** at current scale, modest (~0.3 s); at 10× current
volume, **multi-second**.

### Layer 5 — Cache & UX nits

- Wrap the filter-options and data-extent reads in
  `@st.cache_data(show_spinner=False, ttl=300)` — they are currently
  called directly at `orchestrator/dashboard.py:882-884` with no
  Streamlit caching, so every rerun pays a fresh round-trip even
  though the values change rarely. A 5-minute TTL is appropriate
  because the data only grows as `analytics.sync` runs; if staleness
  matters, gate invalidation on the sync wakeup instead of a fixed
  TTL.
- Render the topbar / filter bar / KPI strip from the first reader to
  return rather than blocking on every reader. The current code
  already does this for the spend value via `topbar_slot.markdown(...)`;
  extend the pattern so the page paints in stages instead of going
  blank for the whole load.
- Show a single in-line "Loading analytics…" spinner instead of letting
  Streamlit show no feedback for ~30 s. (Each `@st.cache_data` call
  already has `show_spinner=False`; re-enable for the first call only.)
- Consider moving the analytics Postgres on-host (replace the
  remote VPN-reached instance with a local Postgres on the dashboard
  host) — RTT drops from ~209 ms to ~0.1 ms and every layer above gets
  a free order-of-magnitude win.

### Layer 6 — Optional: drop the per-widget event/stage filter scan

Many widgets re-emit `event = 'agent_exit'` or `event = 'stage_enter'`
predicates that are already covered by the partial indexes
`analytics_events_agent_exit_idx` / `analytics_events_stage_enter_idx`.
Verify with `EXPLAIN ANALYZE` that the planner picks these on the
default `All` preset; if not, rewriting the predicate to match the
index's `WHERE event = '…'` literal exactly (no parameter placeholder)
or adding an explicit `event` column to the rollup view will let the
planner skip the event filter at scan time.

## Suggested rollout

1. **PR 1** — Layer 1: connection per request + opt-in `conn=` param on
   every reader. No behavioral change, just plumbing. Largest single
   win, lowest risk.
2. **PR 2** — Layer 2: parallel fan-out in `dashboard.py::main()`,
   guarded by an env flag so we can A/B against the sequential path.
3. **PR 3** — Layer 3: collapsed `get_summary` + unioned
   `get_filter_options`. Touches read-model SQL but the public
   signatures stay.
4. **PR 4** — Layer 5 UX polish (incremental render + spinner) so the
   user perceives the page as fast even before Layer 4 lands.
5. **PR 5** — Layer 4: daily-rollup materialized view + cutover for the
   widgets that don't need raw rows. Schema migration + sync-job hook
   for refresh.

## Measurement / acceptance

Add a tiny instrumentation block in `dashboard.py::main()` that wraps
the read fan-out with `time.perf_counter()` and logs
`dashboard.load: total=X.Xs reads=N` at INFO. Acceptance bar:

- After Layer 1: cold load **< 10 s**.
- After Layers 1+2: cold load **< 5 s**.
- After Layers 1+2+3: cold load **< 3 s**.
- After Layers 1+2+3+5 (with on-host Postgres): cold load **< 1 s**.
