# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy Streamlit dashboard facade with a direct-launch entrypoint."""
from __future__ import annotations

if __package__:
    from orchestrator._dashboard_facade_bootstrap import bootstrap_dashboard
else:
    from _dashboard_facade_bootstrap import bootstrap_dashboard


_FACADE = bootstrap_dashboard(
    __file__,
    __name__,
    __package__,
)
__getattr__ = _FACADE.resolve_export
__dir__ = _FACADE.exported_dir
main = _FACADE.main
analytics_read = _FACADE.analytics_read
analytics = _FACADE.analytics


if __name__ == "__main__":
    main()
