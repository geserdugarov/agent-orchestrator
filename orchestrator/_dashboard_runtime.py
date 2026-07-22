# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit dashboard startup and page-level rendering."""
from __future__ import annotations

from typing import Any

from orchestrator import _dashboard_page_controls as page_controls
from orchestrator import dashboard_reads, dashboard_widgets


def main() -> None:
    """Run the Streamlit analytics page with lazily loaded dependencies."""
    import streamlit as st

    _run_dashboard(st)


def _load_dashboard_modules(st: Any) -> dashboard_widgets._DashboardModules:
    import pandas as pd

    from orchestrator import dashboard_charts, dashboard_theme

    return dashboard_widgets._DashboardModules(
        st=st,
        pd=pd,
        charts=dashboard_charts,
        theme=dashboard_theme,
    )


def _configure_dashboard(modules: dashboard_widgets._DashboardModules) -> None:
    modules.st.set_page_config(
        page_title="Orchestrator Analytics",
        layout="wide",
    )
    modules.st.markdown(modules.theme.PAGE_CSS, unsafe_allow_html=True)


def _stop_if_dashboard_unconfigured(
    modules: dashboard_widgets._DashboardModules,
) -> None:
    from orchestrator import dashboard as dashboard_facade

    message = dashboard_facade.db_unconfigured_message()
    if not message:
        return
    modules.st.warning(message)
    modules.st.stop()


def _run_dashboard(st: Any) -> None:
    modules = _load_dashboard_modules(st)
    _configure_dashboard(modules)
    _stop_if_dashboard_unconfigured(modules)
    _render_dashboard(
        modules,
        *dashboard_reads._read_static_metadata(st=modules.st),
    )


def _render_dashboard(
    modules: dashboard_widgets._DashboardModules,
    extent: Any,
    options: Any,
) -> None:
    if extent.min_ts is None or extent.max_ts is None:
        dashboard_widgets._render_no_data(
            st=modules.st,
            extent=extent,
            theme=modules.theme,
        )
        return
    page = page_controls._prepare_dashboard_page(modules, extent, options)
    loaded = dashboard_widgets._load_dashboard_data(modules, page)
    if loaded is None:
        return
    dashboard_widgets._render_dashboard_widgets(modules, page, loaded)
