# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused projection assertions for analytics test rows and queries."""

from operator import attrgetter


def assert_row_fields(test_case, record, expected_fields: dict[str, object]) -> None:
    """Compare named row attributes while keeping expectations at the call site."""
    for field_name, expected_field in expected_fields.items():
        test_case.assertEqual(getattr(record, field_name), expected_field, field_name)


def assert_column_values(test_case, rows, expected_columns: dict[str, list]) -> None:
    """Compare projected result columns while keeping expectations visible."""
    for field_name, expected_column in expected_columns.items():
        test_case.assertEqual(
            list(map(attrgetter(field_name), rows)),
            expected_column,
            field_name,
        )


def assert_sql_fragments(test_case, sql: str, fragments: tuple[str, ...]) -> None:
    """Assert that one query contains every named contract fragment."""
    for fragment in fragments:
        test_case.assertIn(fragment, sql)
