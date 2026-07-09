# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pure read model over the opt-in trajectory sink (`TRAJECTORY_LOG_PATH`).

Streamlit-free, import-light counterpart to `orchestrator.analytics.read`:
where that module queries the analytics Postgres, this one reads the local
JSONL file the trajectory sink appends to and shapes its `agent_trajectory`
records for the viewer page in `orchestrator.trajectory_dashboard`. The
trajectory sink is deliberately never replayed into Postgres (see
`docs/observability.md`), so the JSONL file is the only source for this data.

Everything here is import-light -- only stdlib plus `orchestrator.analytics`
(for the `TRAJECTORY_LOG_PATH` module attribute) -- so importing it never
pulls Streamlit into the polling tick's import surface. The page module owns
the Streamlit rendering; this module owns the parsing, filtering, the
filter-option / summary aggregation, the normalized per-run timeline (which
folds an old steps-only record and a new record with interleaved text turns
into one ordered prompt -> steps -> output sequence), the run- and per-turn
usage views (the denormalized `run_usage` summary plus claude's per-turn
`turns`, with `usage_for_turn` and the `cost_usd` / `total_tokens` / `model`
convenience accessors), and the synthetic-fixture predicate (which flags the
test-suite records an inherited file may carry) -- all of which are pure and
unit-tested.

Resilience contract mirrors the rest of the codebase: a missing file, a
malformed line, a record that is not an `agent_trajectory`, or a renamed /
absent field yields a smaller result, never an exception. Records the sink
already redacted and truncated are surfaced verbatim -- the viewer is a
read-only window onto an already-sanitised file.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from orchestrator import analytics

log = logging.getLogger(__name__)

# Event kind the trajectory sink writes. The file is single-producer
# (`append_trajectory_record`) so in practice every line carries this
# event, but the reader filters on it defensively so a hand-edited or
# concatenated file cannot smuggle a foreign record into the viewer.
TRAJECTORY_EVENT = "agent_trajectory"

# The two synthetic timeline-entry kinds that bracket a run's recorded
# steps -- the leading orchestrator prompt and the trailing final answer.
# Between them the step kinds ride through verbatim (`tool_call`,
# `tool_result`, and -- on records written since the timeline feature --
# `assistant_message` / `user_message` text turns). See
# `TrajectoryRun.timeline`.
TIMELINE_PROMPT = "prompt"
TIMELINE_OUTPUT = "output"

# Tells that mark a record as a synthetic test fixture rather than a real
# run. The trajectory sink predates this viewer, so a file an operator
# inherits can carry records the test suite wrote when it happened to run
# with the sink enabled. Any one tell is enough -- see
# `TrajectoryRun.is_fixture`.
_FIXTURE_PROMPT = "ignored"          # the sentinel prompt fixtures pass
_FIXTURE_SESSION_PREFIX = "sess-"    # synthetic ids; a real id is a uuid
_FIXTURE_SKILL_TOOL = "Skill"        # a run whose every step is a Skill call

UNCONFIGURED_LOG_MESSAGE = (
    "`TRAJECTORY_LOG_PATH` is not configured. The trajectory sink is "
    "opt-in and default-off, so no trajectories have been recorded. Set "
    "`TRAJECTORY_LOG_PATH=/path/to/trajectories.jsonl` in the environment "
    "and **relaunch** the orchestrator so `record_agent_exit` starts "
    "appending records, then relaunch this viewer."
)


@dataclass(frozen=True)
class TrajectoryStepView:
    """One ordered step of a run: a `tool_call` or its `tool_result`.

    The fields mirror the record's `steps[]` entries, normalised to
    plain strings so the page never has to guard against `None`: `name`
    is the tool name on a call (empty on a result), `tool_id` joins a
    result back to its call (empty when the stream omitted it), and
    `content` is the already-redacted-and-truncated payload (empty when
    the sink stored `None` for an empty body). `turn` is the 0-based index
    of the assistant turn that produced the step (claude billed steps
    only); a `tool_result` / `user_message` step is a turn *input*, not
    billed output, so it stays `None`, as does every step on a codex or
    pre-usage record.
    """

    kind: str
    name: str = ""
    tool_id: str = ""
    content: str = ""
    turn: Optional[int] = None

    @property
    def is_call(self) -> bool:
        return self.kind == "tool_call"

    @property
    def is_result(self) -> bool:
        return self.kind == "tool_result"


@dataclass(frozen=True)
class TimelineEntry:
    """One entry in a run's normalized, ordered timeline.

    `TrajectoryRun.timeline` folds the record's leading prompt
    (`user_input`), its ordered `steps[]`, and its trailing final
    `output` into a single sequence so the viewer can walk an old
    steps-only record and a new record whose steps interleave
    `assistant_message` / `user_message` text turns the same way. `kind`
    is `prompt` / `output` for the two synthetic brackets and otherwise
    the underlying step's own kind. `name` / `tool_id` carry the tool
    metadata on a `tool_call` (empty on results, message turns, and the
    two brackets); `content` is the already-redacted body. `turn` carries
    the step's assistant-turn index (see `TrajectoryStepView.turn`) so the
    page can render the per-turn usage line at the boundary while walking
    the timeline, and stays `None` on the two brackets and on turn inputs.
    """

    kind: str
    content: str = ""
    name: str = ""
    tool_id: str = ""
    turn: Optional[int] = None

    @property
    def is_prompt(self) -> bool:
        return self.kind == TIMELINE_PROMPT

    @property
    def is_output(self) -> bool:
        return self.kind == TIMELINE_OUTPUT


@dataclass(frozen=True)
class TurnUsageView:
    """Per-turn token usage for one claude assistant turn (`message.id`).

    Mirrors one entry of the record's `turns[]` array. `turn` is the
    0-based index the sibling `steps[].turn` refer to (`None` only when a
    hand-edited record dropped it, which leaves the turn unreachable by
    `usage_for_turn`). `cache_write_tokens` is the summed 5m + 1h cache-
    creation bucket. `cost_usd` is always an *estimate* -- `cost_source`
    is `estimated`, or `unknown-price` with `cost_usd=None` for an unpriced
    model -- never the authoritative run figure. Codex records carry no
    turns, so this view is claude-only today.
    """

    turn: Optional[int] = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = ""

    @property
    def total_tokens(self) -> int:
        # Claude's four buckets are disjoint (cache read / write are not part
        # of `input_tokens`), so summing them is the true per-turn throughput.
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class RunUsageView:
    """Run-level usage summary denormalized onto an `agent_trajectory` record.

    Mirrors the record's `run_usage` object -- the run's `UsageMetrics`
    minus `backend` (already a record field): `models` in first-seen order,
    the token buckets, the derived `turns` count, and the *authoritative*
    run `cost_usd` / `cost_source` (which, unlike a turn's, may be
    `reported`). This is the run headline and codex's only usage surface
    (codex has no per-turn detail). Every field is defensively coerced so a
    hand-edited or pre-usage line never crashes the reader.
    """

    models: tuple[str, ...] = ()
    turns: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = ""

    @property
    def total_tokens(self) -> int:
        # Sum the claude cache buckets alongside input / output: they are
        # disjoint from `input_tokens` on claude, and 0 on codex (whose
        # `cached_tokens` is a subset of `input_tokens` and stays out of the
        # sum), so the same expression is the true total for both backends.
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class TrajectoryRun:
    """One `agent_trajectory` record, parsed and normalised for display.

    `seq` is the record's 0-based position in the file -- a stable
    identity for the selected run that survives filtering / sorting.
    Optional context fields the sink drops when `None` (`session_id`,
    `review_round`, `retry_count`) keep their absence: strings default
    to empty, the two integers stay `Optional`.
    """

    seq: int
    ts: str
    repo: str
    issue: int
    stage: str = ""
    agent_role: str = ""
    backend: str = ""
    session_id: str = ""
    review_round: Optional[int] = None
    retry_count: Optional[int] = None
    user_input: str = ""
    system_prompt: str = ""
    output: str = ""
    tools: tuple[str, ...] = ()
    skills_triggered: tuple[str, ...] = ()
    skills_available: tuple[str, ...] = ()
    steps: tuple[TrajectoryStepView, ...] = ()
    run_usage: Optional[RunUsageView] = None
    turns: tuple[TurnUsageView, ...] = ()
    truncated: bool = False

    @property
    def tool_calls(self) -> int:
        # Only `tool_call` steps count -- `assistant_message` /
        # `user_message` turns on newer records must not inflate the tally.
        return sum(1 for s in self.steps if s.is_call)

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def model(self) -> str:
        """The primary model name -- first of `run_usage.models`, else empty.

        Empty on a codex run that recorded no model and on a pre-usage
        record (`run_usage is None`), so the viewer can print it unguarded.
        """
        if self.run_usage is None or not self.run_usage.models:
            return ""
        return self.run_usage.models[0]

    @property
    def cost_usd(self) -> Optional[float]:
        """The authoritative run cost, or `None` when unpriced / pre-usage.

        Reads the run summary (not the per-turn estimates, which need not
        sum to it); `None` when `run_usage` is absent or its cost was
        `no-usage` / `unknown-price`.
        """
        return self.run_usage.cost_usd if self.run_usage is not None else None

    @property
    def cost_source(self) -> str:
        """The run cost's provenance (`reported` / `estimated` / ...).

        Empty string on a pre-usage record so the viewer can print it
        unguarded.
        """
        return self.run_usage.cost_source if self.run_usage is not None else ""

    @property
    def total_tokens(self) -> int:
        """Run-total tokens across all buckets, 0 on a pre-usage record."""
        return self.run_usage.total_tokens if self.run_usage is not None else 0

    @cached_property
    def _turn_map(self) -> dict[int, TurnUsageView]:
        # Index turns by their 0-based `turn` for the O(1) `usage_for_turn`
        # lookup the viewer does per turn boundary. Built once and cached on
        # the instance's __dict__ (cached_property writes there directly, so
        # it works on this frozen dataclass); turns with no index are skipped
        # and a duplicate index keeps the last, mirroring the sink's
        # last-record-per-id discipline.
        return {t.turn: t for t in self.turns if t.turn is not None}

    def usage_for_turn(self, turn: Optional[int]) -> Optional[TurnUsageView]:
        """The per-turn usage for a 0-based `turn` index, or `None`.

        Lets a timeline entry find the usage of the assistant turn that
        produced it in O(1). Returns `None` for a `turn=None` input (a
        `tool_result` / `user_message` step or a bracket) and for an index
        with no recorded turn -- a codex run, a pre-usage record, or a turn
        the sink's budget dropped from `turns[]`.
        """
        if turn is None:
            return None
        return self._turn_map.get(turn)

    @property
    def timeline(self) -> tuple[TimelineEntry, ...]:
        """The prompt, then the ordered steps, then the final output.

        A normalized view across record vintages: an old record carrying
        only `tool_call` / `tool_result` steps and a new record whose
        steps interleave `assistant_message` / `user_message` turns both
        yield one ordered sequence bracketed by the prompt and output. A
        bracket is omitted when its field is empty, so a record that never
        captured a prompt or produced an output simply starts or ends on
        its steps. The step entries preserve `steps[]` order verbatim, so
        the tool-call timeline a viewer renders is unchanged -- the
        prompt and output are added around it, not woven into it.
        """
        entries: list[TimelineEntry] = []
        if self.user_input:
            entries.append(
                TimelineEntry(kind=TIMELINE_PROMPT, content=self.user_input)
            )
        for s in self.steps:
            entries.append(
                TimelineEntry(
                    kind=s.kind,
                    content=s.content,
                    name=s.name,
                    tool_id=s.tool_id,
                    turn=s.turn,
                )
            )
        if self.output:
            entries.append(
                TimelineEntry(kind=TIMELINE_OUTPUT, content=self.output)
            )
        return tuple(entries)

    @property
    def is_fixture(self) -> bool:
        """True when the record looks like a synthetic test fixture.

        The trajectory file an operator inherits can carry records the
        test suite wrote when it ran with the sink enabled. Three tells,
        any one of which marks a run synthetic:

        * the sentinel prompt `ignored` the fixtures pass when the prompt
          text is irrelevant to the assertion;
        * a `sess-*` session id (the fixtures' synthetic ids; a real
          `result.session_id` is a uuid, never this prefix);
        * a Skill-only run -- every recorded step is a `Skill` tool call,
          with no real tool work -- the shape the skill-trigger fixtures
          emit.

        Surfaced as a marker so a viewer can flag these and consumed by
        `filter_runs(exclude_fixtures=True)` so they can be dropped,
        without anyone hand-curating the file.
        """
        if self.user_input.strip().lower() == _FIXTURE_PROMPT:
            return True
        if self.session_id.startswith(_FIXTURE_SESSION_PREFIX):
            return True
        if self.steps and all(
            s.is_call and s.name == _FIXTURE_SKILL_TOOL for s in self.steps
        ):
            return True
        return False

    def detail_label(self) -> str:
        """The per-run half of `label()`: stage/role, backend, round, ts.

        The repo and issue are chosen separately in the viewer's
        cascading run selector, so this drops them and keeps only the
        cohort the operator picks between within one issue, e.g.
        `documenting/developer · claude · round 0 · 2026-06-30T...`.
        """
        stage = self.stage or "—"
        role = self.agent_role or "—"
        backend = self.backend or "—"
        round_suffix = (
            f" · round {self.review_round}"
            if self.review_round is not None
            else ""
        )
        return f"{stage}/{role} · {backend}{round_suffix} · {self.ts}"

    def label(self) -> str:
        """One-line label for the run picker.

        Leads with the issue / repo so the operator can scan by target,
        then the `detail_label` cohort and the timestamp.
        """
        return f"#{self.issue} {self.repo} · {self.detail_label()}"


@dataclass(frozen=True)
class FilterOptions:
    """Distinct filter values across a set of runs, each sorted."""

    repos: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    agent_roles: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrajectorySummary:
    """Headline counts for the filtered run set (the KPI strip)."""

    total_runs: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    total_tool_calls: int = 0
    truncated_runs: int = 0
    total_cost_usd: float = 0.0


def resolve_log_path() -> Optional[Path]:
    """Return the configured trajectory log path, or `None` when off.

    Reads the live `analytics.TRAJECTORY_LOG_PATH` module attribute (the
    sink parses it from the env at import) rather than re-parsing the
    env here, so the viewer and the producer agree on the path and tests
    can `patch.object(analytics, "TRAJECTORY_LOG_PATH", ...)`.
    """
    return analytics.TRAJECTORY_LOG_PATH


def log_unconfigured_message() -> Optional[str]:
    """Return the opt-in banner when the sink is off, else `None`."""
    if resolve_log_path() is None:
        return UNCONFIGURED_LOG_MESSAGE
    return None


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int coercion; `None` on anything non-numeric."""
    if isinstance(value, bool):  # bool is an int subclass -- reject it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort float coercion; `None` on anything non-numeric.

    Used for `cost_usd`, which the sink stores as a float or omits (`null`)
    when the run was unpriced. `bool` is rejected (as in `_coerce_int`) and a
    non-numeric string yields `None`, so a hand-edited line never crashes the
    reader nor coerces an absent cost to `0.0`.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str:
    """Normalise a possibly-absent scalar to a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    """Normalise a record's list-of-names field to a string tuple."""
    if not isinstance(value, list):
        return ()
    return tuple(_coerce_str(v) for v in value if v is not None)


def _as_list(value: Any) -> list:
    """Return `value` when it is a list, else `[]`.

    Guards the array-shaped record fields (`steps`, `turns`) before the
    parser iterates them: a hand-edited record carrying a scalar there
    (`"turns": 1`) must yield an empty section, not a `TypeError` -- the
    same never-crash contract `_coerce_str_tuple` already gives the
    names fields.
    """
    return value if isinstance(value, list) else []


def _parse_step(raw: Any) -> Optional[TrajectoryStepView]:
    """Parse one `steps[]` entry; `None` when it is not a usable dict."""
    if not isinstance(raw, dict):
        return None
    kind = _coerce_str(raw.get("kind"))
    if not kind:
        return None
    return TrajectoryStepView(
        kind=kind,
        name=_coerce_str(raw.get("name")),
        tool_id=_coerce_str(raw.get("tool_id")),
        content=_coerce_str(raw.get("content")),
        turn=_coerce_int(raw.get("turn")),
    )


def _parse_run_usage(raw: Any) -> Optional[RunUsageView]:
    """Parse the record's `run_usage` object; `None` when absent / malformed.

    A pre-usage record has no `run_usage` key and a hand-edited one may carry
    a non-dict there -- both yield `None` so the run's `cost_usd` / `model` /
    `total_tokens` degrade to their empty defaults rather than raising.
    """
    if not isinstance(raw, dict):
        return None
    return RunUsageView(
        models=_coerce_str_tuple(raw.get("models")),
        turns=_coerce_int(raw.get("turns")),
        input_tokens=_coerce_int(raw.get("input_tokens")) or 0,
        output_tokens=_coerce_int(raw.get("output_tokens")) or 0,
        cached_tokens=_coerce_int(raw.get("cached_tokens")) or 0,
        cache_read_tokens=_coerce_int(raw.get("cache_read_tokens")) or 0,
        cache_write_tokens=_coerce_int(raw.get("cache_write_tokens")) or 0,
        cost_usd=_coerce_float(raw.get("cost_usd")),
        cost_source=_coerce_str(raw.get("cost_source")),
    )


def _parse_turn(raw: Any) -> Optional[TurnUsageView]:
    """Parse one `turns[]` entry; `None` when it is not a usable dict."""
    if not isinstance(raw, dict):
        return None
    return TurnUsageView(
        turn=_coerce_int(raw.get("turn")),
        model=_coerce_str(raw.get("model")),
        input_tokens=_coerce_int(raw.get("input_tokens")) or 0,
        output_tokens=_coerce_int(raw.get("output_tokens")) or 0,
        cache_read_tokens=_coerce_int(raw.get("cache_read_tokens")) or 0,
        cache_write_tokens=_coerce_int(raw.get("cache_write_tokens")) or 0,
        cost_usd=_coerce_float(raw.get("cost_usd")),
        cost_source=_coerce_str(raw.get("cost_source")),
    )


def parse_record(obj: Any, *, seq: int) -> Optional[TrajectoryRun]:
    """Parse one decoded JSONL object into a `TrajectoryRun`.

    Returns `None` when `obj` is not a dict or is not an
    `agent_trajectory` record, so a foreign / malformed record is
    skipped rather than rendered. Every field is coerced defensively:
    the record was written by this codebase, but the viewer must not
    crash on a hand-edited or partially-written line.
    """
    if not isinstance(obj, dict):
        return None
    if obj.get("event") != TRAJECTORY_EVENT:
        return None
    steps = tuple(
        step
        for step in (_parse_step(s) for s in _as_list(obj.get("steps")))
        if step is not None
    )
    turns = tuple(
        turn
        for turn in (_parse_turn(t) for t in _as_list(obj.get("turns")))
        if turn is not None
    )
    return TrajectoryRun(
        seq=seq,
        ts=_coerce_str(obj.get("ts")),
        repo=_coerce_str(obj.get("repo")),
        issue=_coerce_int(obj.get("issue")) or 0,
        stage=_coerce_str(obj.get("stage")),
        agent_role=_coerce_str(obj.get("agent_role")),
        backend=_coerce_str(obj.get("backend")),
        session_id=_coerce_str(obj.get("session_id")),
        review_round=_coerce_int(obj.get("review_round")),
        retry_count=_coerce_int(obj.get("retry_count")),
        user_input=_coerce_str(obj.get("user_input")),
        system_prompt=_coerce_str(obj.get("system_prompt")),
        output=_coerce_str(obj.get("output")),
        tools=_coerce_str_tuple(obj.get("tools")),
        skills_triggered=_coerce_str_tuple(obj.get("skills_triggered")),
        skills_available=_coerce_str_tuple(obj.get("skills_available")),
        steps=steps,
        run_usage=_parse_run_usage(obj.get("run_usage")),
        turns=turns,
        truncated=bool(obj.get("truncated")),
    )


def read_trajectories(path: Optional[Path] = None) -> list[TrajectoryRun]:
    """Read every `agent_trajectory` record, newest first.

    `path` defaults to the configured `TRAJECTORY_LOG_PATH`; an absent
    path (sink disabled) or a missing file yields an empty list. Blank
    lines, non-JSON lines, and non-`agent_trajectory` records are
    skipped silently -- the same "malformed lines do not stop the read"
    contract the sink's prune honours. An `OSError` reading the file is
    logged and downgraded to an empty list so the page can render its
    empty state instead of a stack trace.

    Records are returned sorted by `ts` descending (most recent first),
    with the original file order as a stable tie-breaker so two records
    sharing a second-precision timestamp keep their append order.
    """
    if path is None:
        path = resolve_log_path()
    if path is None:
        return []
    runs: list[TrajectoryRun] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for seq, line in enumerate(fh):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                run = parse_record(obj, seq=seq)
                if run is not None:
                    runs.append(run)
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("could not read trajectory log %s: %s", path, e)
        return []
    # Sort newest-first; `-seq` keeps the most recently appended record
    # ahead of an equal-timestamp predecessor while staying a total order.
    runs.sort(key=lambda r: (r.ts, r.seq), reverse=True)
    return runs


def _distinct_sorted(
    runs: Sequence[TrajectoryRun], key: Callable[[TrajectoryRun], str]
) -> tuple[str, ...]:
    """Distinct, sorted, non-empty values of `key` across `runs`.

    The shared collector behind every `FilterOptions` dimension: an empty
    value -- a run that omitted that field -- is dropped so the sidebar never
    offers a blank choice, and the result is sorted for a stable dropdown.
    """
    return tuple(sorted({value for value in (key(r) for r in runs) if value}))


def filter_options(runs: Sequence[TrajectoryRun]) -> FilterOptions:
    """Collect the distinct, sorted filter values across `runs`.

    Empty dimension values are dropped so the sidebar never offers a
    blank choice for a record that omitted (e.g.) its stage.
    """
    return FilterOptions(
        repos=_distinct_sorted(runs, lambda r: r.repo),
        backends=_distinct_sorted(runs, lambda r: r.backend),
        agent_roles=_distinct_sorted(runs, lambda r: r.agent_role),
        stages=_distinct_sorted(runs, lambda r: r.stage),
    )


def _matches_query(run: TrajectoryRun, needle: str) -> bool:
    """Case-insensitive substring match across every free-text field.

    Spans the prompt, system prompt, final output, each step's tool
    name and content, and the skill / tool name sets so the operator
    can find a run by anything it carried -- a file path it touched, a
    tool it called, a skill it triggered, or a phrase in its answer.
    """
    haystacks: list[str] = [
        run.repo,
        run.stage,
        run.agent_role,
        run.user_input,
        run.system_prompt,
        run.output,
    ]
    haystacks.extend(run.tools)
    haystacks.extend(run.skills_triggered)
    haystacks.extend(run.skills_available)
    for step in run.steps:
        haystacks.append(step.name)
        haystacks.append(step.content)
    return any(needle in h.lower() for h in haystacks if h)


def filter_runs(
    runs: Sequence[TrajectoryRun],
    *,
    repo: Optional[str] = None,
    backends: Optional[Sequence[str]] = None,
    agent_roles: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    query: Optional[str] = None,
    exclude_fixtures: bool = False,
) -> list[TrajectoryRun]:
    """Return the subset of `runs` matching every supplied filter.

    A `None` or empty multi-value filter (`backends` / `agent_roles` /
    `stages`) means "no constraint on this dimension" -- the friendlier
    viewer default, distinct from the analytics dashboard's tri-state
    multiselect. `repo` / `issue` are exact-match scalars; `query` is a
    case-insensitive substring matched across every free-text field.
    `exclude_fixtures` (default off, so existing callers are unaffected)
    drops the synthetic test-suite records `TrajectoryRun.is_fixture`
    flags. Relative order is preserved.
    """
    backend_set = set(backends) if backends else None
    role_set = set(agent_roles) if agent_roles else None
    stage_set = set(stages) if stages else None
    needle = query.strip().lower() if query and query.strip() else None

    out: list[TrajectoryRun] = []
    for r in runs:
        if exclude_fixtures and r.is_fixture:
            continue
        if repo is not None and r.repo != repo:
            continue
        if issue is not None and r.issue != issue:
            continue
        if backend_set is not None and r.backend not in backend_set:
            continue
        if role_set is not None and r.agent_role not in role_set:
            continue
        if stage_set is not None and r.stage not in stage_set:
            continue
        if needle is not None and not _matches_query(r, needle):
            continue
        out.append(r)
    return out


def summarize(runs: Sequence[TrajectoryRun]) -> TrajectorySummary:
    """Headline counts for the (filtered) run set.

    `total_cost_usd` sums the authoritative run cost over runs that carry
    one -- a run with no `run_usage` (pre-usage record) or an unpriced cost
    (`None`) contributes nothing rather than a spurious 0, so the KPI reads
    the spend of the runs that actually recorded it.
    """
    return TrajectorySummary(
        total_runs=len(runs),
        distinct_issues=len({(r.repo, r.issue) for r in runs}),
        distinct_repos=len({r.repo for r in runs if r.repo}),
        total_tool_calls=sum(r.tool_calls for r in runs),
        truncated_runs=sum(1 for r in runs if r.truncated),
        total_cost_usd=sum(
            r.cost_usd for r in runs if r.cost_usd is not None
        ),
    )
