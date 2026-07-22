# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing resume."""
from __future__ import annotations

import inspect
from typing import Any

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_DevResumeOptions = _owner._DevResumeOptions
_DevResumePlan = _owner._DevResumePlan
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
Tuple = _owner.Tuple
config = _owner.config
dataclass = _owner.dataclass
filter_trusted = _owner.filter_trusted
_IMPLEMENTING_STAGE = _state._IMPLEMENTING_STAGE
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_RETRY_COUNT = _state._RETRY_COUNT


_DEV_RESUME_SIGNATURE = inspect.Signature((
    inspect.Parameter("gh", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("spec", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("issue", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("resume_args", inspect.Parameter.VAR_POSITIONAL),
    inspect.Parameter(
        "stage",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
    ),
    inspect.Parameter("option_fields", inspect.Parameter.VAR_KEYWORD),
))


@dataclass(frozen=True)
class _DevResumeRequest:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    resume_args: tuple
    option_fields: dict
    stage: Optional[str]


@dataclass(frozen=True)
class _DevResumeContext:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    followup_text: str
    options: _DevResumeOptions
    worktree: Path
    plan: _DevResumePlan
    stage: str

    @classmethod
    def build(
        cls, request: _DevResumeRequest,
    ) -> _DevResumeContext:
        if len(request.resume_args) != 2:
            raise TypeError("expected state and followup_text")
        state, followup_text = request.resume_args
        options = _DevResumeOptions.from_fields(request.option_fields)
        worktree = _owner._ensure_resume_worktree(request.spec, request.issue, state)
        plan = _owner._resolve_dev_session_for_resume(request.issue, state)
        # An explicit `stage` wins over the label read off `issue`: a caller
        # that just relabeled the issue (validating -> fixing on
        # CHANGES_REQUESTED) holds an `Issue` whose cached `labels` PyGithub
        # did not refresh after `set_labels`, so `gh.workflow_label(issue)`
        # would still report the pre-flip stage and misattribute the run.
        return cls(
            gh=request.gh,
            spec=request.spec,
            issue=request.issue,
            state=state,
            followup_text=followup_text,
            options=options,
            worktree=worktree,
            plan=plan,
            stage=(
                request.stage
                or request.gh.workflow_label(request.issue)
                or _IMPLEMENTING_STAGE
            ),
        )

    def execute(self) -> Tuple[Path, AgentResult, bool]:
        from orchestrator import workflow as _wf

        agent_result, paused = self._run_attempt(
            fresh=self.plan.fresh_spawn,
            session_id=self.plan.session.session_id,
        )
        if paused:
            return self.worktree, agent_result, True
        fresh_spawn = self.plan.fresh_spawn
        if self._needs_fresh_retry(agent_result):
            _wf.log.info(
                "issue=#%d dropping poisoned dev session %r after poisoned-session "
                "marker (stale or context overflow); retrying once as a fresh spawn",
                self.issue.number, self.plan.session.session_id,
            )
            _owner._drop_poisoned_dev_session(self.state)
            fresh_spawn = True
            agent_result, paused = self._run_attempt(
                fresh=True, session_id=None,
            )
            if paused:
                return self.worktree, agent_result, True
        _owner._persist_dev_session_after_run(
            self.state,
            agent_result,
            fresh_spawn=fresh_spawn,
            resume_count=self.plan.resume_count,
        )
        return self.worktree, agent_result, False

    def _run_attempt(
        self, *, fresh: bool, session_id: Optional[str],
    ) -> tuple[AgentResult, bool]:
        from orchestrator import workflow as _wf

        session = self.plan.session
        agent_result = _wf._run_agent_tracked(
            self.gh,
            self.issue.number,
            agent_role="developer",
            stage=self.stage,
            backend=session.backend,
            prompt=_owner._build_dev_spawn_prompt(
                self.spec,
                self.issue,
                self.followup_text,
                followup_has_tracked_repos=(
                    self.options.followup_has_tracked_repos
                ),
                fresh=fresh,
            ),
            cwd=self.worktree,
            agent_spec=session.spec,
            resume_session_id=session_id,
            extra_args=session.extra_args,
            review_round=self.state.get("review_round", 0),
            retry_count=self.state.get(_RETRY_COUNT),
        )
        _wf._accumulate_issue_usage(self.state, agent_result.usage)
        paused = (
            self.options.pause_guard
            and _wf._paused_during_agent_run(self.gh, self.issue)
        )
        return agent_result, paused

    def _needs_fresh_retry(self, agent_result: AgentResult) -> bool:
        return (
            self.plan.session.session_id is not None
            and not self.plan.fresh_spawn
            and _owner._is_poisoned_session_failure(
                self.plan.session.backend, agent_result,
            )
        )


def _ensure_resume_worktree(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Path:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if worktree.exists():
        return worktree
    return _wf._ensure_worktree(
        spec,
        issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )


def _resume_dev_with_text(
    *args: Any,
    **kwargs: Any,
) -> Tuple[Path, AgentResult, bool]:
    """Resume the dev's locked-backend session with the given prompt text.

    `stage` overrides the recorded stage for every audit / analytics /
    trajectory record this run emits. It defaults to the label read off
    `issue`, which is correct whenever the caller fetched the issue fresh this
    tick. The CHANGES_REQUESTED fix path must pass it explicitly (`fixing`):
    it relabels validating -> fixing and then resumes on the SAME `Issue`
    object, whose cached `labels` PyGithub does not refresh after
    `set_labels`, so the label read would still report `validating` and
    attribute the developer run to the reviewer's stage.

    The backend is locked to whatever wrote `dev_session_id` (or the legacy
    `codex_session_id`) for this issue -- resuming across backends would need
    an inter-backend session bridge that does not exist. Clears the
    `awaiting_human` flag because the caller is reacting to a fresh human
    signal (issue or PR comment) by spawning the agent.

    After `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent`
    parks on the current `dev_session_id`, the resume drops the session id
    and starts a fresh spawn instead. Sessions killed mid-stream (e.g. by a
    Claude rate limit) consistently return empty results on every subsequent
    resume; without this fallback every human "retry" comment burns another
    fresh-spawn retry slot on the same poisoned session.

    Proactive rotation: each resume increments a per-session `dev_resume_count`
    and, once it reaches `config.DEV_SESSION_MAX_RESUMES` (when that knob is
    > 0), the session is retired and the spawn goes fresh. `--resume` replays
    the entire accumulated transcript every time, so a session resumed many
    times creeps toward the model context window; rotating proactively rebuilds
    a small prompt from durable state (issue body + recent comments + the
    committed branch) and caps that creep before it overflows. Every fresh
    spawn -- whether triggered by rotation, the silent-park fallback, or
    poisoned-session recovery -- is prefixed with a re-grounding preamble
    (`_build_fresh_respawn_preamble`) because the prior session's in-memory
    reasoning is gone and only its committed work survives on the branch.

    A Claude resume that comes back with `No conversation found with session
    ID` (or a sibling marker), or with a `Prompt is too long` context-window
    overflow, is treated as the same poisoned-session condition but
    recognized immediately: the pinned session id is cleared and the call is
    retried once as a fresh spawn in the same worktree, so a Claude session
    whose transcript was GC'd or grew past the context window doesn't park
    (`agent_silent` for two ticks, or `awaiting_human` forever) before
    recovering.

    Returns `(worktree, result, paused)`. `paused` is the live-pause decision
    -- True only when `pause_guard` is set AND a hard-skip control label
    (`paused` / `backlog`) was applied to a freshly fetched issue while an agent
    run was in flight. `pause_guard` is opt-in (default False): every
    developer-resume caller -- implementing, validating, documenting, in_review,
    fixing, and resolving_conflict -- passes it True and honors the flag. The
    check runs after BOTH agent runs -- the initial resume/spawn AND the
    poisoned-session fresh retry -- because each has its own live-pause window:
    the first fires before the retry spawns a second agent, and the second
    before the retry's result is persisted. When it fires the helper stops
    before the session id is persisted and before `awaiting_human` is cleared,
    and the caller must honor the returned flag by stopping too -- the decision
    is propagated, not re-fetched, so there is no window where the caller reads
    the label differently than the helper did.
    """
    bound_fields = _DEV_RESUME_SIGNATURE.bind(*args, **kwargs)
    bound_fields.apply_defaults()
    request = _DevResumeRequest(
        gh=bound_fields.arguments["gh"],
        spec=bound_fields.arguments["spec"],
        issue=bound_fields.arguments["issue"],
        resume_args=bound_fields.arguments["resume_args"],
        option_fields=bound_fields.arguments["option_fields"],
        stage=bound_fields.arguments["stage"],
    )
    return _DevResumeContext.build(request).execute()


_resume_dev_with_text.__signature__ = _DEV_RESUME_SIGNATURE


def _resume_developer_on_human_reply(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
    *,
    pause_guard: bool = False,
) -> Optional[Tuple[Path, AgentResult, bool]]:
    """Resume the developer's agent session with new issue-level comments.

    Returns (worktree, agent_result, paused) on resume, or None if there are no
    new comments since the last park (caller should return without writing
    state). `paused` is forwarded from `_resume_dev_with_text` and is only ever
    True when `pause_guard` is set; both callers (implementing and validating)
    pass it True and honor the flag.

    Used by `implementing` and `validating` -- both deliberately watch only
    the issue's comment thread, not the PR's. The `in_review` handler watches
    PR comments too via `_resume_dev_with_text` directly.

    Bumps `last_action_comment_id` to the highest consumed comment id BEFORE
    spawning the agent. Without this, a successful resume during implementing
    or validating leaves `last_action_comment_id` at the prior park id, so
    the validating->in_review handoff treats the just-consumed human reply
    as fresh PR feedback and re-resumes the dev on input it has already
    handled. This pre-resume bump is also robust to mid-resume failures:
    if the agent crashes or times out, those comments are still recorded
    as consumed (the dev DID see them via the resume prompt), and the
    failure is surfaced via the timeout/dirty/question paths instead.

    Untrusted authors are dropped up front so nothing they post drives the
    resume: with `ALLOWED_ISSUE_AUTHORS` set an outsider reply posted while the
    issue is parked awaiting human must not reach the dev prompt NOR advance the
    consumed watermark. Only trusted comments are consumed, so an outsider reply
    trailing a trusted one is left unconsumed rather than persisted as the
    watermark; an all-untrusted batch is treated as "no new reply".
    """
    last_action_id = state.get(_LAST_ACTION_COMMENT_ID)
    new_comments = filter_trusted(gh.comments_after(issue, last_action_id))
    if not new_comments:
        return None
    consumed_max = max(comment.id for comment in new_comments)
    state.set(_LAST_ACTION_COMMENT_ID, consumed_max)
    from orchestrator import workflow as _wf

    followup = "\n\n".join(
        _wf._quote_comment_line(comment)
        for comment in new_comments if comment.body
    )
    followup = f"{followup}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    return _owner._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=pause_guard,
    )
