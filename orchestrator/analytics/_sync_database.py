# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync connection and rollup-refresh operations."""

from __future__ import annotations

import logging
import time
from typing import Any

_DAILY_ROLLUP_VIEW = "analytics_daily_rollup"
log = logging.getLogger("orchestrator.analytics.sync")


def _default_connect(db_url: str) -> Any:
    """Lazy psycopg import so the module loads without the driver.

    `pyproject.toml` pins `psycopg[binary]`, but a sync that never
    runs (operator hasn't deployed Postgres) must not surface an
    ImportError -- the orchestrator's polling tick imports this module
    transitively via `config`. Defer the import to call time so the
    module-load path stays driver-free.
    """
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("psycopg is required for analytics_sync; run `uv sync --locked` to install it") from error
    return psycopg.connect(db_url)


def _default_json_adapter(payload: Any) -> Any:
    """Adapt dict / list to the psycopg JSON wrapper when available.

    Falls back to passing the raw Python object through; psycopg v3's
    default adaptation already handles dict / list as JSONB inserts
    so the wrapper is optional. The factory pattern lets tests inject
    `lambda v: v` and inspect raw structures.
    """
    try:
        from psycopg.types.json import Json
    except ImportError:
        return payload
    return Json(payload)


def _rollback_quietly(conn: Any, message: str) -> None:
    """Roll `conn` back, downgrading a rollback failure to a logged no-op.

    A rollback that itself raises leaves cleanup to the driver; there is
    nothing more the sync can do, so `message` is logged and the failure is
    swallowed rather than masking the original error that triggered the
    rollback.
    """
    try:
        conn.rollback()
    except Exception:
        log.exception(message)


def _close_quietly(conn: Any) -> None:
    """Close `conn`, downgrading a close failure to a logged no-op.

    The committed rows are already durable, so a driver whose `close` raises
    must not turn a successful sync into a failure.
    """
    try:
        conn.close()
    except Exception:
        log.exception("analytics_sync: connection close failed")


def _execute_rollup_refresh(conn: Any) -> None:
    """Run the non-concurrent rollup refresh and log its timing."""
    sql = f"REFRESH MATERIALIZED VIEW {_DAILY_ROLLUP_VIEW}"
    refresh_start = time.monotonic()
    log.info(
        "analytics_sync: refreshing materialized view %s",
        _DAILY_ROLLUP_VIEW,
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    log.info(
        "analytics_sync: refreshed %s in %.3fs",
        _DAILY_ROLLUP_VIEW,
        time.monotonic() - refresh_start,
    )


def _refresh_daily_rollup(conn: Any) -> None:
    """Refresh the daily rollup materialized view after a successful sync.

    Issues a non-concurrent `REFRESH MATERIALIZED VIEW` over the view
    defined in `analytics-db/init/01-schema.sql`. Non-concurrent is
    the safe default because it does not require the view to be
    populated and does not lock the events table; it does take an
    `ACCESS EXCLUSIVE` lock on the view itself for the duration of
    the rebuild, which is fine for the operator-driven sync (the
    dashboard re-reads on a 60-second cache and tolerates a brief
    blocked read).

    Exceptions are logged and swallowed so a refresh failure -- the
    view not existing yet on a pre-migration deployment, a transient
    Postgres error, a lock-wait timeout -- never aborts the sync.
    The committed inserts are already durable in `analytics_events`,
    so the caller's success contract is unaffected; the operator's
    log makes the refresh failure visible and the next sync's
    refresh recovers the rollup once the underlying issue is fixed.
    """
    try:
        _execute_rollup_refresh(conn)
    except Exception:
        log.exception(
            "analytics_sync: refresh of %s failed; sync still committed",
            _DAILY_ROLLUP_VIEW,
        )
        _rollback_quietly(conn, "analytics_sync: rollback after refresh failure failed")
