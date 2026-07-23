# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Runtime identity, wildcard inventory, and patch routing for facades."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.reexport_test_facades import LAZY_FACADES, WORKFLOW_FACADE
from tests.reexport_test_support import lazy_targets, resolve_target


_ISSUE_NUMBER = 17


class ReexportRuntimeTest(unittest.TestCase):
    def test_targets_preserve_identity_and_import(self) -> None:
        for module, manifest in LAZY_FACADES:
            for name, target in lazy_targets(manifest).items():
                with self.subTest(module=module.__name__, name=name):
                    resolved = resolve_target(module, name, target)
                    self.assertIs(resolved.direct, resolved.expected)
                    self.assertIs(resolved.imported, resolved.expected)

    def test_wildcard_inventory_resolves(self) -> None:
        for module, _manifest in LAZY_FACADES:
            with self.subTest(module=module.__name__):
                namespace = {
                    name: getattr(module, name)
                    for name in module.__all__
                }
                self.assertEqual(set(namespace), set(module.__all__))
                self.assertTrue(all(
                    exported is getattr(module, name)
                    for name, exported in namespace.items()
                ))

    def test_workflow_handler_patch_stays_late_bound(self) -> None:
        issue = SimpleNamespace(number=_ISSUE_NUMBER)
        spec = SimpleNamespace(slug="owner/repo")
        ready_handler = Mock()
        with patch.object(
            WORKFLOW_FACADE,
            "_handle_ready",
            ready_handler,
        ):
            WORKFLOW_FACADE._route_issue_to_handler(
                None,
                spec,
                issue,
                "ready",
            )
        ready_handler.assert_called_once_with(None, spec, issue)
