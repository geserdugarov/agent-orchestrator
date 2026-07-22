# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Skill analytics read result models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillTriggerRateRow:
    """Per-`(agent_role, backend)` skill-trigger aggregate over agent runs.

    Powers the dashboard's opt-in "Skill trigger rates" panel. The
    skill fields live in `analytics_events.extras` JSONB -- they are
    not promoted columns and the daily rollup does not carry them --
    so this reader scans the base table directly (no DDL, no view
    change). `runs` is every `agent_exit` row in the group; `skill_runs`
    is how many of those carried a `skills_triggered` key (the firm
    "the stream surfaced at least one skill" signal); `total_triggers`
    sums `skills_triggered_count` so a run that pulled `develop` three
    times weighs more than one clean trigger.

    `record_agent_exit` only writes the skill keys when
    `TRACK_SKILL_TRIGGERS` is on *and* a skill fired (an empty field is
    dropped, not written), so `skill_runs` is a *floor* on observed
    skill use: a `0` rate conflates a run that triggered nothing with
    one whose tracking was off. The dashboard captions the panel
    accordingly. NULL `agent_role` / `backend` bucket under `"unknown"`
    so a category is never silently dropped.
    """

    agent_role: str
    backend: str
    runs: int
    skill_runs: int = 0
    total_triggers: int = 0

    @property
    def rate(self) -> float:
        """Share of runs in the group that triggered >=1 skill (0.0-1.0).

        Returns `0.0` for a zero-run group so callers never divide by
        zero; the reader only emits rows for groups with at least one
        `agent_exit` run, so the guard is defensive.
        """
        return self.skill_runs / self.runs if self.runs else float()


@dataclass(frozen=True)
class SkillTriggerMatrixRow:
    """One `(repo, skill, agent_role, backend)` cell of the trigger matrix.

    Powers the dashboard's opt-in per-skill trigger matrix.
    `get_skill_trigger_matrix` combines the repo's `repo_skill_catalog`
    records (the universe of skills a repo offers, from the
    `skills_available` array) with the filtered `agent_exit` rows (the
    runs that actually fired a skill, from the `skills_triggered` array)
    -- both live in `analytics_events.extras` JSONB, so the reader scans
    the base table with no DDL and no rollup change.

    `skill_runs` counts how many runs in the cell *contained* the skill
    (one per run per distinct name in its `skills_triggered` list), not
    the total number of invocations -- a run that pulled `develop` three
    times still weighs one here. A cell with `skill_runs == 0` is a real
    "offered but never triggered" signal: the skill is in the repo's
    catalog and the `(agent_role, backend)` cohort ran in the window,
    but no such run reached for it (e.g. `developer / claude / review =
    0`). When the catalog records are missing the matrix degrades to
    just the observed-trigger cells -- no zero rows are invented.

    `runs` is the total number of `agent_exit` runs in the cell's
    `(repo, agent_role, backend)` cohort (every run, whether or not it
    fired this skill), so a low `skill_runs` reads against the cohort
    size rather than in a vacuum. It is always `>= skill_runs` and,
    because a cell only exists for a cohort that actually ran, always
    `>= 1`. This mirrors `SkillTriggerRateRow.runs` / `.skill_runs`.

    `agent_role` / `backend` bucket NULLs under `"unknown"` so a cohort
    is never silently dropped. The same `TRACK_SKILL_TRIGGERS`-off
    caveat as `SkillTriggerRateRow` applies: a `0` cannot distinguish a
    tracked-but-quiet run from one whose tracking was off.
    """

    repo: str
    skill: str
    agent_role: str
    backend: str
    runs: int = 0
    skill_runs: int = 0

    @property
    def rate(self) -> float:
        """Share of the cell's cohort runs that fired this skill (0.0-1.0).

        `skill_runs / runs`, guarded against a zero-run cell so callers
        never divide by zero -- a cell only exists for a cohort that ran,
        so the guard is defensive. Mirrors `SkillTriggerRateRow.rate`; a
        `0.0` rate is the offered-but-never-triggered catalog signal and
        carries the same `TRACK_SKILL_TRIGGERS`-off caveat.
        """
        return self.skill_runs / self.runs if self.runs else float()


@dataclass(frozen=True)
class SkillAdoptionRow:
    """One `(repo, skill, agent_role, backend)` cell of skill adoption
    aggregated by logical agent session rather than by raw agent run.

    Powers the dashboard's opt-in per-session skill-adoption view.
    `get_skill_adoption` first identifies each logical session from the
    `agent_exit` rows in the reporting window -- keyed by
    `resume_session_id`, then `session_id`, then a per-row fallback so an
    ID-less row is its own session -- and then reads that session's
    availability and load evidence from every `agent_exit` row before the
    window end (ignoring the window start and the stage filter, so a load
    from a prior stage or from before the window still counts, while a
    later load cannot leak backward). All the skill fields live in
    `analytics_events.extras` JSONB, so the reader scans the base table
    with no DDL and no rollup change, mirroring `SkillTriggerMatrixRow`.

    `sessions` is the denominator: how many logical sessions in the
    cohort had this skill *available* -- its `skills_available` set listed
    the skill, or, for a legacy load recorded before availability metadata
    existed, the skill was loaded while the session's `skills_available`
    key was absent entirely (an explicit empty set counts as metadata, so
    it does not imply availability). `adopted` is the numerator: how many
    of those sessions actually loaded the skill, counted once per session
    no matter how many runs reached for it. `adoption_rate` is
    `adopted / sessions`.

    `invocations`, `load_rows`, and `incidental` are explicitly
    window-scoped diagnostics -- they count only the reporting-window
    `agent_exit` rows, not the historical evidence. `invocations` is the
    cohort's window run count: every `(repo, agent_role, backend)` run in
    the window, whether or not it loaded this skill, so a low `load_rows`
    reads against the cohort's run volume (mirroring
    `SkillTriggerMatrixRow.runs`). `load_rows` counts the window runs that
    loaded the skill (one per run per distinct loaded name); `incidental`
    counts the window runs that referenced the skill's `SKILL.md` without
    loading it.

    `agent_role` / `backend` bucket NULLs under `"unknown"` so a cohort is
    never silently dropped. The same `TRACK_SKILL_TRIGGERS`-off caveat as
    `SkillTriggerRateRow` applies: with tracking off no skill keys are
    written, so a quiet cohort and an untracked one are indistinguishable.
    """

    repo: str
    skill: str
    agent_role: str
    backend: str
    sessions: int = 0
    adopted: int = 0
    invocations: int = 0
    load_rows: int = 0
    incidental: int = 0

    @property
    def adoption_rate(self) -> float:
        """Share of the cell's available sessions that loaded the skill.

        `adopted / sessions`, guarded against a zero-session cell so a
        row that exists only for its window diagnostics (a purely
        incidental reference, or a load whose session reported a
        different availability set) never divides by zero.
        """
        return self.adopted / self.sessions if self.sessions else float()
