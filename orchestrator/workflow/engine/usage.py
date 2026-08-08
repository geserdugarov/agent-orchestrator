# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The accounting a tracked agent run is bookended by.

`_run_agent_tracked` is the single spawn site every role goes through --
decomposer, developer, reviewer, documenter, fixer, conflict resolver,
question responder -- and the rest of this module is one part of the bookend
it wraps that spawn in: the frozen request the caller describes the run with,
the audit pair around it, the analytics record its exit earns, and the
`skill_triggered` events that record's return value drives. They sit together
because they share one `request`, so a field added for the audit event is
already the field the record carries and the skill event repeats.

The spawn itself is named on `orchestrator/agents/runner.py`, the owner that
defines it. That call is the seam the stage tests replace to drive a handler
without a CLI, so a mock has to land on the runner owner; one left on the
`orchestrator.workflow` facade beside it would let a real CLI run.

Everything after the spawn is fail-open. The record and the trajectory write
behind it ride guards inside `recording.record_agent_exit`, and the skill
emission carries its own here, because none of it is worth a run whose
`agent_spawn` / `agent_exit` events already fired. An exception out of the
spawn is the deliberate exception: it propagates, leaving a spawn with no
matching exit for the per-issue `tick()` catch to log.

The per-issue meter closes the same loop from the other end. The
`UsageMetrics` `record_agent_exit` attaches to the returned result is exactly
the object `_accumulate_issue_usage` folds into the running counters on the
handler's pinned state, and `_format_issue_usage_verdict` reads those counters
back into the single receipt line a terminal posts. The fold deliberately does
not happen inside `_run_agent_tracked`: the tracked run writes no pinned state,
so the handler that owns the write stays its only writer.

`_now_iso` sits here because the stamps it writes mark the same events. Every
pinned-state timestamp a stage sets -- `last_agent_action_at`,
`last_review_at`, `decomposed_at`, the terminal `merged_at` -- records when a
run or its verdict landed, and one UTC, second-resolution ISO shape is what
lets two ticks' stamps compare as plain strings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from github.Issue import Issue

from orchestrator._workflow_state import log
from orchestrator.agents import AgentResult, runner as _agent_runner
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.observability.analytics import recording
from orchestrator.observability.usage.metrics import UsageMetrics
from orchestrator.workflow.engine import comments as _comments


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class _AgentRunRequest:
    """Agent invocation plus the audit/analytics context that follows it."""

    agent_role: str
    stage: str
    backend: str
    prompt: str
    cwd: Path
    agent_spec: Optional[str] = None
    resume_session_id: Optional[str] = None
    timeout: Optional[int] = None
    extra_args: tuple[str, ...] = ()
    review_round: Optional[int] = None
    retry_count: Optional[int] = None


def _agent_run_kwargs(request: _AgentRunRequest) -> dict[str, Any]:
    """Forward only optional runner kwargs that the caller supplied."""
    kwargs: dict[str, Any] = {"extra_args": request.extra_args}
    if request.resume_session_id is not None:
        kwargs["resume_session_id"] = request.resume_session_id
    if request.timeout is not None:
        kwargs["timeout"] = request.timeout
    return kwargs


def _record_tracked_agent_exit(
    gh: GitHubClient,
    issue_number: int,
    request: _AgentRunRequest,
    agent_result: AgentResult,
    duration_s: float,
):
    gh.emit_event(
        "agent_exit",
        issue_number=issue_number,
        stage=request.stage,
        agent=request.backend,
        agent_role=request.agent_role,
        session_id=agent_result.session_id,
        duration_s=duration_s,
        exit_code=agent_result.exit_code,
        timed_out=agent_result.timed_out,
        review_round=request.review_round,
        retry_count=request.retry_count,
    )
    return recording.record_agent_exit(
        repo=getattr(gh, "_repo_slug", None) or "",
        issue=issue_number,
        stage=request.stage,
        agent_role=request.agent_role,
        backend=request.backend,
        agent_spec=request.agent_spec,
        resume_session_id=request.resume_session_id,
        result=agent_result,
        duration_s=duration_s,
        review_round=request.review_round,
        retry_count=request.retry_count,
        fallback_model=_configured_model(request.backend, request.extra_args),
        prompt=request.prompt,
        cwd=request.cwd,
    )


def _emit_triggered_skills(
    gh: GitHubClient,
    issue_number: int,
    request: _AgentRunRequest,
    triggered_skills,
) -> None:
    try:
        for skill in triggered_skills or ():
            gh.emit_event(
                "skill_triggered",
                issue_number=issue_number,
                stage=request.stage,
                agent=request.backend,
                agent_role=request.agent_role,
                review_round=request.review_round,
                retry_count=request.retry_count,
                skill=skill,
            )
    except Exception:
        log.exception(
            "issue=#%d: skill_triggered audit emission failed; continuing",
            issue_number,
        )


def _run_agent_tracked(
    gh: GitHubClient,
    issue_number: int,
    request: Optional[_AgentRunRequest] = None,
    **request_fields: Any,
) -> AgentResult:
    """Run an agent, bookending the spawn with `agent_spawn` / `agent_exit`
    audit events and appending a per-invocation analytics record on exit.

    Thin wrapper around `run_agent` -- the spawn behaviour is unchanged.
    Optional context (`review_round`, `retry_count`, resume session id) is
    forwarded so downstream consumers can correlate spawns with retry
    budgets and reviewer rounds. The exit record carries
    `exit_code`/`timed_out`/`duration_s` from the AgentResult so an
    operator tailing the JSONL sink sees timeouts and crashes without
    needing the orchestrator log too. An exception out of `run_agent`
    propagates -- the audit log will show a spawn without a matching
    exit, which is intentional (the per-issue `tick()` catch above logs
    the traceback).

    After the audit `agent_exit` is emitted, an analytics record is
    appended to `ANALYTICS_LOG_PATH` via the recording owner's
    `append_record` (a no-op when the sink is disabled). The record carries
    the same contextual fields (`repo`, `issue`, `stage`, `agent_role`, `backend`,
    `agent_spec`, `resume_session_id` / `session_id`, `review_round`,
    `retry_count`, `duration_s`, `exit_code`, `timed_out`) plus parsed
    token counts, model list, `cost_usd`, and `cost_source` extracted
    from `result.stdout` by `observability/usage/metrics.py`'s
    `parse_agent_usage`. The configured model is pulled out of
    `extra_args` (via `_configured_model`) and passed as the parser's
    `fallback_model` so a codex run whose stdout omits the model name
    still records the configured model and an estimated cost when the
    SKU is in the price table. Prompts, raw stdout/stderr, secrets, and
    worktree contents are intentionally NOT stored in this `agent_exit`
    record -- the analytics sink is a foundation for usage / cost
    aggregation, not a debugging mirror, and `result.stdout` may contain
    user-issue text. A parser failure or a sink IO error is swallowed so
    an analytics misconfiguration cannot stop the per-issue tick.

    The returned `AgentResult` additionally carries the parsed run usage on its
    `usage` field -- `record_agent_exit` attaches the `UsageMetrics` it parsed
    from the same stdout, independent of whether the sink is enabled -- so
    callers can read token / cost metrics off the result without re-parsing.
    It is `None` when the usage parse failed (fail-open); this is best-effort
    observability plumbing and does not touch the pinned state.

    The `prompt` is forwarded to `record_agent_exit` so it can land as the
    redacted `user_input` of the separate, opt-in trajectory record -- and
    ONLY when `TRAJECTORY_LOG_PATH` is enabled. With the trajectory sink off
    (the default) the prompt is never stored and the `agent_exit` record
    shape is unchanged. That trajectory parse / redact / write rides its own
    fail-open guard inside `record_agent_exit`, so it never disturbs the
    baseline record or the `skill_triggered` events below.

    The worktree `cwd` is also forwarded so `record_agent_exit` can discover
    a codex run's offered skills out-of-band from the filesystem -- codex's
    stream carries no offered-skills catalog the way claude's `system`/`init`
    frame does, so this backfills `skills_available` for codex records.

    When `TRACK_SKILL_TRIGGERS` is on, `record_agent_exit` returns the
    distinct skills the run triggered and one `skill_triggered` audit event
    is emitted per skill (carrying `agent`, `agent_role`, `review_round`,
    `retry_count`, and `skill`), reusing that parsed list rather than
    re-reading stdout. The switch off (the default) yields no list and thus
    no events, so the gating is inherited from the analytics layer; the
    emission is wrapped in its own fail-open guard so an opt-in bug can never
    cost a run whose baseline `agent_spawn` / `agent_exit` events already
    fired.
    """
    if request is not None and request_fields:
        raise TypeError("pass either request or keyword request fields, not both")
    run_request = request or _AgentRunRequest(**request_fields)
    start = time.monotonic()
    gh.emit_event(
        "agent_spawn",
        issue_number=issue_number,
        stage=run_request.stage,
        agent=run_request.backend,
        agent_role=run_request.agent_role,
        session_id=run_request.resume_session_id,
        review_round=run_request.review_round,
        retry_count=run_request.retry_count,
    )
    # Forward only the kwargs the original call sites set so the
    # wrapper's run_agent invocation matches the pre-tracking signature
    # call-for-call (test fakes assert on `call.kwargs`).
    agent_result = _agent_runner.run_agent(
        run_request.backend,
        run_request.prompt,
        run_request.cwd,
        **_agent_run_kwargs(run_request),
    )
    duration_s = round(time.monotonic() - start, 3)
    triggered_skills = _record_tracked_agent_exit(
        gh, issue_number, run_request, agent_result, duration_s,
    )
    # One `skill_triggered` audit event per distinct triggered skill, reusing
    # the list `record_agent_exit` already parsed (no second pass over stdout).
    # Empty unless `TRACK_SKILL_TRIGGERS` is on, so the gating is inherited
    # from the analytics layer. This is opt-in observability, so it rides its
    # own fail-open guard exactly like the skill parse does -- a bug here must
    # never break a run whose baseline audit events have already fired.
    _emit_triggered_skills(gh, issue_number, run_request, triggered_skills)
    return agent_result


def _configured_model(
    backend: str, extra_args: tuple[str, ...]
) -> Optional[str]:
    """Pull the configured model name out of a backend's `extra_args`.

    codex selects the model with `-m <model>` (or `-m=<model>`); claude
    uses `--model <model>` (or `--model=<model>`). Whichever is present
    is forwarded to `observability/usage/metrics.py`'s
    `parse_agent_usage` as `fallback_model` so a codex run whose stdout
    carries usage frames but omits the model (resume frames, minimal
    completions, schema drift) still produces a populated `models` list
    and -- when the model is in the price table -- an estimated
    `cost_usd`. Returns `None` when neither flag is set so the parser
    keeps its own "unknown" handling.

    The split-form (`-m gpt-5`) and `=`-form (`--model=gpt-5`) are both
    accepted because `shlex.split` produces either shape depending on
    the operator's quoting; only one needs to win.
    """
    flag = "-m" if backend == "codex" else "--model"
    eq_prefix = f"{flag}="
    for arg_index, arg in enumerate(extra_args):
        if arg == flag and arg_index + 1 < len(extra_args):
            model_name = extra_args[arg_index + 1].strip()
            return model_name or None
        if arg.startswith(eq_prefix):
            model_name = arg[len(eq_prefix):].strip()
            return model_name or None
    return None


def _accumulate_issue_usage(
    state: PinnedState, usage: Optional[UsageMetrics]
) -> None:
    """Fold one agent run's parsed usage into the per-issue running totals.

    Called by the developer (implementing) and reviewer (validating) run
    sites right after `_run_agent_tracked` returns, mutating the SAME
    `PinnedState` the handler persists later -- never a second writer. The
    runner deliberately does not write pinned state itself, so an
    `interrupted` run whose handler returns without `write_pinned_state`
    (the shutdown-sweep contract) simply never persists these counters: a
    slight, accepted undercount on killed runs, with the analytics sink
    still holding ground truth.

    Keys folded (all new to the pinned-state schema):
      * ``issue_agent_runs``     -- +1 per real agent exit.
      * ``issue_total_tokens``   -- input + output + cache-read + cache-write.
        codex's ``cached_tokens`` is intentionally excluded: it is the
        portion of ``input_tokens`` already served from cache, so summing it
        would double-count part of the input.
      * ``issue_total_cost_usd`` -- sum of each run's ``cost_usd``; ``None``
        costs (``no-usage`` / ``unknown-price``) contribute nothing.
      * ``issue_cost_sources``   -- sorted distinct ``cost_source`` tags seen.
        The minimal aggregate a terminal verdict needs to mark ``(est.)``
        (any ``estimated``) or an unpriced ``unknown`` (any ``unknown-price``)
        without re-reading the analytics sink.

    A ``None`` usage -- the fail-open case where the parse itself failed --
    is a no-op: with no parsed metrics there is nothing to fold and the run
    is not counted.
    """
    if usage is None:
        return

    agent_runs = int(state.get("issue_agent_runs") or 0)
    state.set("issue_agent_runs", agent_runs + 1)

    tokens = sum((
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    ))
    state.set(
        "issue_total_tokens",
        int(state.get("issue_total_tokens") or 0) + tokens,
    )

    if usage.cost_usd is not None:
        state.set(
            "issue_total_cost_usd",
            float(state.get("issue_total_cost_usd") or 0) + usage.cost_usd,
        )

    prior_sources = state.get("issue_cost_sources")
    seen = set(prior_sources) if isinstance(prior_sources, list) else set()
    seen.add(usage.cost_source)
    state.set("issue_cost_sources", sorted(seen))


def _format_issue_usage_verdict(state: PinnedState) -> Optional[str]:
    """Render the cumulative per-issue usage verdict for a terminal surface.

    Reads the counters `_accumulate_issue_usage` folds onto pinned state and
    returns a single visible line:

        :receipt: this issue: 3 agent runs · 45,200 tokens · $0.87

    The cost slot follows `issue_cost_sources`: `(est.)` is appended when any
    run's cost was `estimated` from the price table, and the whole figure
    collapses to `unknown` when any `unknown-price` run leaves the priced
    total incomplete (that dominates -- an unknown total cannot also be an
    estimate). A `no-usage` run contributes nothing and marks neither.

    Returns None when no agent run was ever counted (`issue_agent_runs` is
    0 / absent) so a terminal with nothing to report skips the line instead
    of posting a zero receipt.
    """
    runs = int(state.get("issue_agent_runs") or 0)
    if runs <= 0:
        return None
    tokens = int(state.get("issue_total_tokens") or 0)
    prior_sources = state.get("issue_cost_sources")
    sources = set(prior_sources) if isinstance(prior_sources, list) else set()
    if "unknown-price" in sources:
        cost = "unknown"
    else:
        cost = f"${float(state.get('issue_total_cost_usd') or 0):.2f}"
        if "estimated" in sources:
            cost = f"{cost} (est.)"
    return (
        f":receipt: this issue: {runs} agent runs · "
        f"{tokens:,} tokens · {cost}"
    )


def _post_issue_usage_verdict(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> None:
    """Post the terminal usage verdict as its own tracked issue comment.

    Thin wrapper over `_format_issue_usage_verdict` + `_post_issue_comment`
    for the PR merged / rejected finalizers, which otherwise post no comment
    of their own. Must run BEFORE the finalizer's `write_pinned_state` so the
    comment id lands in the same persisted state and a later drift/watermark
    tick recognizes it as orchestrator-authored. A no-op when there is
    nothing to report (no counted agent run).
    """
    verdict = _format_issue_usage_verdict(state)
    if verdict:
        _comments._post_issue_comment(gh, issue, state, verdict)
