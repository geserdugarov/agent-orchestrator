# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-stage handlers for the orchestrator state machine.

The dispatcher (`orchestrator.workflow._process_issue`) still owns the
label->handler routing; modules under this package own the bodies of
those handlers and their stage-private helpers. `workflow.py` re-exports
each handler under its original `_handle_*` name so direct test
references and intra-handler calls keep working.
"""
