# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics sync responsibility-boundary tests."""

import unittest
from types import MappingProxyType


from orchestrator.analytics import _sync_rows

_ROW_MAPPING_MODULE = 'orchestrator.analytics._sync_row_mapping'


_MAPPING_MEMBER_MODULES = MappingProxyType({
    "_build_insert_sql": _ROW_MAPPING_MODULE,
    "_content_hash": "orchestrator.analytics._sync_row_parse",
    "_prepare_record": _ROW_MAPPING_MODULE,
    "_row_values": _ROW_MAPPING_MODULE,
    "_RowProvenance": _ROW_MAPPING_MODULE,
})


_SYNC_DRIVEN_MEMBERS = (
    "_build_insert_sql",
    "_prepare_record",
    "_row_values",
    "_RowProvenance",
)


class SyncRowMappingExtractionTest(unittest.TestCase):
    """The record -> DB-row mapping (the promoted-column schema, the
    canonical-JSON content hash, and per-record validation) lives in focused
    parsing and mapping leaves. The `_sync_rows` compatibility hub and ingest
    driver retain the historical objects.
    """

    def test_mapping_members_have_named_leaves(self) -> None:
        for name, module_name in _MAPPING_MEMBER_MODULES.items():
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(_sync_rows, name).__module__,
                    module_name,
                )

    def test_sync_reaches_the_sync_rows_objects(self) -> None:
        from orchestrator.analytics import sync

        for name in _SYNC_DRIVEN_MEMBERS:
            with self.subTest(name=name):
                self.assertIs(getattr(sync, name), getattr(_sync_rows, name))
