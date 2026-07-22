# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard read connection, filtering, and static metadata."""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from orchestrator.analytics import read as analytics_read


STATIC_METADATA_TTL_SECONDS = 300


def _filter_list(
    filter_values: Optional[Sequence[str]],
) -> Optional[list[str]]:
    """Convert a cached filter tuple back to the read model's list arg."""
    if filter_values is None:
        return None
    return list(filter_values)


def _scoped_read(getter: Callable[..., Any], /, **filters: Any) -> Any:
    """Run one windowed read on the per-thread analytics connection."""
    with analytics_read.analytics_connection() as conn:
        return getter(conn=conn, **filters)


def _read_data_extent():
    return _scoped_read(analytics_read.get_data_extent)


def _read_filter_options():
    return _scoped_read(analytics_read.get_filter_options)


def _read_static_metadata(*, st: Any):
    """Read the data extent and filter options through cached wrappers."""
    read_data_extent = st.cache_data(
        show_spinner=False,
        ttl=STATIC_METADATA_TTL_SECONDS,
    )(_read_data_extent)
    read_filter_options = st.cache_data(
        show_spinner=False,
        ttl=STATIC_METADATA_TTL_SECONDS,
    )(_read_filter_options)
    try:
        return read_data_extent(), read_filter_options()
    except analytics_read.AnalyticsReadError as error:
        st.error(
            "Could not load analytics filter options: "
            f"{error}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()


def _read_filter_kwargs(key: tuple) -> dict[str, Any]:
    return {
        "start": key[0],
        "end": key[1],
        "repo": key[2],
        "events": _filter_list(key[3]),
        "stages": _filter_list(key[4]),
        "issue": key[5],
    }


def _read_filtered(
    getter: Callable[..., Any],
    key: tuple,
    **extra_filters: Any,
) -> Any:
    filters = _read_filter_kwargs(key)
    filters.update(extra_filters)
    return _scoped_read(getter, **filters)
