# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Credential-safe analytics database URL rendering."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_REDACTED_QUERY_PARAMS = frozenset(("user", "password", "passfile", "sslpassword"))


def _redacted_netloc(parts: Any) -> str:
    if not parts.username and not parts.password:
        return parts.netloc
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"***@{netloc}" if netloc else "***"


def _redacted_query(query: str) -> str:
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    redacted_pairs = [
        (key, "***" if key.lower() in _REDACTED_QUERY_PARAMS else param_value) for key, param_value in pairs
    ]
    if redacted_pairs == pairs:
        return query
    return urlencode(redacted_pairs, safe="*")


def _redact_db_url(url: str) -> str:
    """Strip credentials from a libpq URL before it lands in a log line.

    `ANALYTICS_DB_URL` is a libpq URL that may carry credentials in
    two distinct places: the `user:password@` netloc prefix and the
    `?user=&password=&sslpassword=&passfile=` query string. This CLI
    surfaces connection logs to operators and occasionally to shared
    dashboards, so both forms collapse to `***` before printing -- a
    remote-Postgres password never lands in stdout or in any log
    aggregator the host forwards to.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<db-url-unparseable>"
    return urlunsplit(
        (
            parts.scheme,
            _redacted_netloc(parts),
            parts.path,
            _redacted_query(parts.query),
            parts.fragment,
        )
    )
