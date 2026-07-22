# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-adoption and invocation diagnostic widgets."""
from __future__ import annotations

from typing import Any, Optional, Sequence

from orchestrator.analytics.read import (
    SkillAdoptionRow,
    SkillTriggerMatrixRow,
    SkillTriggerRateRow,
)
from orchestrator.dashboard_cards import _card_header_html
from orchestrator.dashboard_html import _skill_triggers_html
from orchestrator.dashboard_skill_adoption import (
    _skill_adoption_html,
    parse_skill_adoption_sort,
)
from orchestrator.dashboard_skill_matrix import (
    _skill_matrix_html,
    parse_skill_matrix_sort,
)


NO_AGENT_EXITS_MESSAGE = "No `agent_exit` rows match the current filters."


def _render_skill_adoption(
    *,
    st: Any,
    skill_adoption_rows: Sequence[SkillAdoptionRow],
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Render session adoption and invocation diagnostics."""
    from orchestrator import dashboard as _dashboard

    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Skill adoption",
                "Share of agent sessions that loaded each available skill, "
                "by repo, role, and backend (requires TRACK_SKILL_TRIGGERS)",
            ),
            unsafe_allow_html=True,
        )
        if not skill_rows:
            st.info("No `agent_exit` rows match the current filters.")
            return
        adopt_sort_key, adopt_sort_desc = parse_skill_adoption_sort(
            st.query_params
        )
        st.markdown(
            _skill_adoption_html(
                skill_adoption_rows,
                sort_key=adopt_sort_key,
                descending=adopt_sort_desc,
            ),
            unsafe_allow_html=True,
        )
        caption = _skill_adoption_zero_caption(skill_adoption_rows)
        if caption is not None:
            st.caption(caption)
        _dashboard._render_skill_invocation_diagnostics(
            st=st,
            skill_rows=skill_rows,
            skill_matrix_rows=skill_matrix_rows,
            tracking_confirmed=bool(skill_adoption_rows),
        )


def _skill_adoption_zero_caption(
    skill_adoption_rows: Sequence[SkillAdoptionRow],
) -> Optional[str]:
    """Return a neutral caption for a genuine zero-adoption window."""
    if not skill_adoption_rows:
        return None
    if any(row.adopted for row in skill_adoption_rows):
        return None
    if any(row.sessions for row in skill_adoption_rows):
        return (
            "Skills were available to sessions this window but none loaded "
            "one -- a genuine 0% adoption, not missing tracking."
        )
    return _skill_adoption_evidence_caption(skill_adoption_rows)


def _skill_adoption_evidence_caption(
    skill_adoption_rows: Sequence[SkillAdoptionRow],
) -> str:
    loaded = any(row.load_rows for row in skill_adoption_rows)
    incidental = any(row.incidental for row in skill_adoption_rows)
    if loaded and incidental:
        return (
            "Skills were loaded and referenced incidentally this window, but "
            "no session reported one available to adopt."
        )
    if loaded:
        return (
            "Skills were loaded this window, but no session reported one "
            "available to adopt."
        )
    return (
        "Only incidental skill references were recorded this window; no "
        "session reported a skill available to adopt."
    )


def _render_skill_invocation_diagnostics(
    *,
    st: Any,
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
    tracking_confirmed: bool = False,
) -> None:
    """Render per-run skill diagnostics in a collapsed expander."""
    with st.expander(
        "Invocation-level diagnostics · per-run skill triggers",
        expanded=False,
    ):
        st.markdown(_skill_triggers_html(skill_rows), unsafe_allow_html=True)
        if not any(row.skill_runs for row in skill_rows):
            if tracking_confirmed:
                st.caption("No agent run triggered a skill in this window.")
            else:
                st.caption(
                    "No skill triggers recorded in this window. Enable "
                    "`TRACK_SKILL_TRIGGERS` (default off) so "
                    "`record_agent_exit` records which skills each run pulls."
                )
        matrix_sort_key, matrix_sort_desc = parse_skill_matrix_sort(
            st.query_params
        )
        st.markdown(
            _skill_matrix_html(
                skill_matrix_rows,
                sort_key=matrix_sort_key,
                descending=matrix_sort_desc,
            ),
            unsafe_allow_html=True,
        )


def _render_skill_triggers(
    *,
    st: Any,
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Render the compatibility trigger-rate skill panel."""
    from orchestrator import dashboard as _dashboard

    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Skill trigger rates",
                "Share of agent runs that triggered a skill, by role and "
                "backend (requires TRACK_SKILL_TRIGGERS)",
            ),
            unsafe_allow_html=True,
        )
        if not skill_rows:
            st.info(NO_AGENT_EXITS_MESSAGE)
            return
        st.markdown(_skill_triggers_html(skill_rows), unsafe_allow_html=True)
        if not any(row.skill_runs for row in skill_rows):
            st.caption(
                "No skill triggers recorded in this window. Enable "
                "`TRACK_SKILL_TRIGGERS` (default off) so "
                "`record_agent_exit` records which skills each run pulls."
            )
        _dashboard._render_skill_matrix_expander(
            st=st,
            skill_matrix_rows=skill_matrix_rows,
        )


def _render_skill_matrix_expander(
    *,
    st: Any,
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Render the per-skill trigger matrix in a collapsed expander."""
    with st.expander(
        "Per-skill trigger matrix · which skills each "
        "repo × role × backend cohort reaches for",
        expanded=False,
    ):
        matrix_sort_key, matrix_sort_desc = parse_skill_matrix_sort(
            st.query_params
        )
        st.markdown(
            _skill_matrix_html(
                skill_matrix_rows,
                sort_key=matrix_sort_key,
                descending=matrix_sort_desc,
            ),
            unsafe_allow_html=True,
        )
