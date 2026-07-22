# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy trajectory-viewer facade with a direct-launch entrypoint."""
from __future__ import annotations

if __package__:
    from orchestrator._trajectory_dashboard_bootstrap import (
        bootstrap_trajectory_dashboard,
    )
else:
    from _trajectory_dashboard_bootstrap import bootstrap_trajectory_dashboard


_FACADE = bootstrap_trajectory_dashboard(__file__, __name__, __package__)
__getattr__ = _FACADE.resolve_export
__dir__ = _FACADE.exported_dir
main = _FACADE.main
trajectory_reader = _FACADE.trajectory_reader


if __name__ == "__main__":
    main()
