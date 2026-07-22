# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow agent runs."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

AgentResult = _owner.AgentResult
Any = _owner.Any
GitHubClient = _owner.GitHubClient
Optional = _owner.Optional
Path = _owner.Path
analytics = _owner.analytics
dataclass = _owner.dataclass
datetime = _owner.datetime
time = _owner.time
timezone = _owner.timezone
log = _state.log


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
    return analytics.record_agent_exit(
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
        fallback_model=_owner._configured_model(request.backend, request.extra_args),
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
    appended to `analytics.ANALYTICS_LOG_PATH` via `analytics.append_record`
    (a no-op when the sink is disabled). The record carries the same
    contextual fields (`repo`, `issue`, `stage`, `agent_role`, `backend`,
    `agent_spec`, `resume_session_id` / `session_id`, `review_round`,
    `retry_count`, `duration_s`, `exit_code`, `timed_out`) plus parsed
    token counts, model list, `cost_usd`, and `cost_source` extracted
    from `result.stdout` by `usage.parse_agent_usage`. The configured
    model is pulled out of `extra_args` (via `_configured_model`) and
    passed as the parser's `fallback_model` so a codex run whose stdout
    omits the model name still records the configured model and an
    estimated cost when the SKU is in the price table. Prompts, raw
    stdout/stderr, secrets, and worktree contents are intentionally NOT
    stored in this `agent_exit` record -- the analytics sink is a foundation
    for usage / cost aggregation, not a debugging mirror, and `result.stdout`
    may contain user-issue text. A parser failure or a sink IO error is
    swallowed so an analytics misconfiguration cannot stop the per-issue tick.

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
    agent_result = _owner.run_agent(
        run_request.backend,
        run_request.prompt,
        run_request.cwd,
        **_owner._agent_run_kwargs(run_request),
    )
    duration_s = round(time.monotonic() - start, 3)
    triggered_skills = _owner._record_tracked_agent_exit(
        gh, issue_number, run_request, agent_result, duration_s,
    )
    # One `skill_triggered` audit event per distinct triggered skill, reusing
    # the list `record_agent_exit` already parsed (no second pass over stdout).
    # Empty unless `TRACK_SKILL_TRIGGERS` is on, so the gating is inherited
    # from the analytics layer. This is opt-in observability, so it rides its
    # own fail-open guard exactly like the skill parse does -- a bug here must
    # never break a run whose baseline audit events have already fired.
    _owner._emit_triggered_skills(gh, issue_number, run_request, triggered_skills)
    return agent_result


def _configured_model(
    backend: str, extra_args: tuple[str, ...]
) -> Optional[str]:
    """Pull the configured model name out of a backend's `extra_args`.

    codex selects the model with `-m <model>` (or `-m=<model>`); claude
    uses `--model <model>` (or `--model=<model>`). Whichever is present
    is forwarded to `usage.parse_agent_usage` as `fallback_model` so a
    codex run whose stdout carries usage frames but omits the model
    (resume frames, minimal completions, schema drift) still produces a
    populated `models` list and -- when the model is in the price table
    -- an estimated `cost_usd`. Returns `None` when neither flag is
    set so the parser keeps its own "unknown" handling.

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
