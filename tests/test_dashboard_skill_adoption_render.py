# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-adoption caption and diagnostic rendering tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_analytics_read as _analytics_read_module,
)

_ENABLE_LABEL = 'Enable'
_TRACK_SKILL_TRIGGERS_NAME = 'TRACK_SKILL_TRIGGERS'


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


class _SkillAdoptionRenderSupport(unittest.TestCase):
    """`_render_skill_adoption` leads with the session-adoption matrix and
    only nags to enable `TRACK_SKILL_TRIGGERS` when there is genuinely no
    evidence. A present row proves tracking is on -- `sessions > 0` means
    availability was recorded, an incidental reference means the stream was
    parsed -- so a zero-adoption window with rows captions a neutral
    genuine-0% result instead. Streamlit is faked so the render runs
    end-to-end and its captions can be observed.
    """

    def render_skill_panel(self, adoption_rows, *, skill_rows=None):
        _, dashboard = _reload()
        st = _SkillPanelStreamlit()
        if skill_rows is None:
            skill_rows = [self.build_rate_row()]
        dashboard._render_skill_adoption(
            st=st,
            skill_adoption_rows=adoption_rows,
            skill_rows=skill_rows,
            skill_matrix_rows=[],
        )
        return st

    def build_adoption_row(self, *, sessions, adopted, load_rows=0, incidental=0):
        from orchestrator.analytics.read import SkillAdoptionRow

        return SkillAdoptionRow(
            repo="owner/repo",
            skill="develop",
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            sessions=sessions,
            adopted=adopted,
            invocations=max(sessions, 1),
            load_rows=load_rows,
            incidental=incidental,
        )

    def build_rate_row(self):
        # skill_runs > 0 so the invocation-level diagnostics expander adds no
        # caption of its own -- the assertions then observe only the
        # adoption panel's own caption.
        return _analytics_read_module().SkillTriggerRateRow(
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=5,
            skill_runs=2,
            total_triggers=2,
        )

    def build_quiet_rate_row(self):
        # skill_runs == 0 so the diagnostics zero-trigger caption is exercised.
        return _analytics_read_module().SkillTriggerRateRow(
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=5,
            skill_runs=0,
            total_triggers=0,
        )


class SkillAdoptionCaptionRenderTest(_SkillAdoptionRenderSupport):
    def test_unadopted_available_caption_is_zero(self) -> None:
        # sessions > 0 proves availability was tracked, so a 0-adoption
        # window reads as a genuine 0%, never a "turn on tracking" nag.
        st = self.render_skill_panel([self.build_adoption_row(sessions=5, adopted=0)])
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("genuine 0% adoption", caption)
        self.assertNotIn(_ENABLE_LABEL, caption)
        self.assertNotIn(_TRACK_SKILL_TRIGGERS_NAME, caption)

    def test_incidental_only_caption_stays_neutral(self) -> None:
        # Incidental evidence with no availability still proves the stream
        # was parsed, so the caption stays neutral rather than recommending
        # the already-on switch.
        adoption_row = self.build_adoption_row(sessions=0, adopted=0, incidental=1)
        st = self.render_skill_panel([adoption_row])
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("incidental", caption)
        self.assertNotIn(_ENABLE_LABEL, caption)
        self.assertNotIn(_TRACK_SKILL_TRIGGERS_NAME, caption)

    def test_adopted_window_has_no_caption(self) -> None:
        st = self.render_skill_panel([self.build_adoption_row(sessions=5, adopted=3)])
        self.assertEqual(st.captions, [])

    def test_empty_rows_defer_hint_to_table(self) -> None:
        # No adoption rows -> the table itself renders the
        # `TRACK_SKILL_TRIGGERS` fallback; the panel adds no caption so the
        # switch reminder is not doubled.
        st = self.render_skill_panel([])
        self.assertEqual(st.captions, [])
        blob = "".join(st.markdowns)
        self.assertIn("orch-skilladopt-empty", blob)
        self.assertIn(_TRACK_SKILL_TRIGGERS_NAME, blob)


class SkillAdoptionDiagnosticRenderTest(_SkillAdoptionRenderSupport):
    def test_no_agent_exit_rows_shows_single_info(self) -> None:
        st = self.render_skill_panel([], skill_rows=[])
        self.assertEqual(len(st.infos), 1)
        self.assertIn("No `agent_exit` rows", st.infos[0])

    def test_load_only_caption_uses_loads(self) -> None:
        # sessions=0, load_rows>0, incidental=0: a session loaded a skill it
        # did not report available. The caption must name the loads (matching
        # the Invocation loads column), never "only incidental references".
        adoption_row = self.build_adoption_row(sessions=0, adopted=0, load_rows=2)
        st = self.render_skill_panel([adoption_row])
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("loaded", caption)
        self.assertNotIn("Only incidental", caption)
        self.assertNotIn(_ENABLE_LABEL, caption)

    def test_mixed_caption_uses_both_evidence(self) -> None:
        # sessions=0 with both load and incidental evidence: the caption
        # names both so it matches the Invocation loads and Incidental
        # references columns.
        st = self.render_skill_panel(
            [self.build_adoption_row(sessions=0, adopted=0, load_rows=2, incidental=1)],
        )
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("loaded", caption)
        self.assertIn("incidental", caption)
        self.assertNotIn(_ENABLE_LABEL, caption)

    def test_zero_trigger_diagnostic_stays_neutral(
        self,
    ) -> None:
        # A window with adoption evidence (sessions>0) but no run triggering a
        # skill must not tell the operator to enable a switch the adoption
        # caption just confirmed is on -- no caption in the panel nags to
        # enable tracking, and the diagnostic reports the genuine no-trigger.
        quiet = self.build_quiet_rate_row()
        st = self.render_skill_panel(
            [self.build_adoption_row(sessions=5, adopted=0)],
            skill_rows=[quiet],
        )
        joined = " ".join(st.captions)
        self.assertNotIn(_ENABLE_LABEL, joined)
        self.assertNotIn(_TRACK_SKILL_TRIGGERS_NAME, joined)
        self.assertTrue(
            any("No agent run triggered a skill" in caption for caption in st.captions),
        )


class _NullContext:
    """`with`-usable stand-in for `st.container(...)` / `st.columns(...)`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
