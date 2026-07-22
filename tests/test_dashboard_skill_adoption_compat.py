# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-trigger compatibility shim tests."""

import inspect


import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_analytics_read as _analytics_read_module,
)


ROLE_DEVELOPER = "developer"


BACKEND_CLAUDE = "claude"


class _SkillPanelStreamlit:
    """Fake `st` recording the calls the skill-panel renderers make.

    Records the markdown / caption / info payloads and the expander labels,
    and hands back a null context for `container` / `expander`, so the
    render runs end-to-end without the optional Streamlit dependency.
    """

    def __init__(self, query_params=None):
        self.query_params = query_params or {}
        self.markdowns: list = []
        self.captions: list = []
        self.infos: list = []
        self.expanders: list = []

    def container(self, **kwargs):
        return _NullContext()

    def expander(self, label, **kwargs):
        self.expanders.append(label)
        return _NullContext()

    def markdown(self, html, **kwargs) -> None:
        self.markdowns.append(html)

    def caption(self, text) -> None:
        self.captions.append(text)

    def show_information(self, text) -> None:
        self.infos.append(text)

    def __getattr__(self, attribute_name):
        if attribute_name == "info":
            return self.show_information
        raise AttributeError(attribute_name)


class SkillTriggersCompatShimTest(unittest.TestCase):
    """`_render_skill_triggers` / `_render_skill_matrix_expander` are stable
    compatibility entry points on the `orchestrator.dashboard` facade:
    keyword-only signatures rendering the "Skill trigger rates" card and its
    fold-out matrix, reachable by name for external callers and patch points.
    """

    def test_shims_are_reexported_from_the_facade(self) -> None:
        _, dashboard = _reload()
        self.assertTrue(hasattr(dashboard, "_render_skill_triggers"))
        self.assertTrue(hasattr(dashboard, "_render_skill_matrix_expander"))

    def test_shim_signatures_preserved(self) -> None:
        _, dashboard = _reload()
        triggers = inspect.signature(dashboard._render_skill_triggers)
        self.assertEqual(
            list(triggers.parameters),
            ["st", "skill_rows", "skill_matrix_rows"],
        )
        for name in triggers.parameters:
            self.assertEqual(
                triggers.parameters[name].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
        expander = inspect.signature(dashboard._render_skill_matrix_expander)
        self.assertEqual(
            list(expander.parameters),
            ["st", "skill_matrix_rows"],
        )

    def test_triggers_shim_renders_trigger_rates_card(self) -> None:
        _, dashboard = _reload()
        st = _SkillPanelStreamlit()
        dashboard._render_skill_triggers(
            st=st,
            skill_rows=[self._rate_row()],
            skill_matrix_rows=[self._matrix_row()],
        )
        blob = "".join(st.markdowns)
        # The original card header, aggregate table, and fold-out matrix.
        self.assertIn("Skill trigger rates", blob)
        self.assertIn("orch-skills", blob)
        self.assertIn("orch-skillmatrix", blob)
        self.assertTrue(
            any("Per-skill trigger matrix" in label for label in st.expanders),
        )

    def test_expander_shim_opens_collapsed_matrix(self) -> None:
        _, dashboard = _reload()
        st = _SkillPanelStreamlit()
        dashboard._render_skill_matrix_expander(
            st=st,
            skill_matrix_rows=[self._matrix_row()],
        )
        self.assertTrue(
            any("Per-skill trigger matrix" in label for label in st.expanders),
        )
        self.assertIn("orch-skillmatrix", "".join(st.markdowns))

    def _rate_row(self):
        return _analytics_read_module().SkillTriggerRateRow(
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=4,
            skill_runs=1,
            total_triggers=1,
        )

    def _matrix_row(self):
        from orchestrator.analytics.read import SkillTriggerMatrixRow

        return SkillTriggerMatrixRow(
            repo="owner/repo",
            skill="develop",
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=4,
            skill_runs=1,
        )


class _NullContext:
    """`with`-usable stand-in for `st.container(...)` / `st.columns(...)`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
