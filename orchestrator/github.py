# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Thin GitHub client built on PyGithub.

Per-issue state is stored in a single 'pinned' comment whose body matches
PINNED_STATE_RE. The orchestrator owns this comment and only edits it from
write_pinned_state. `read_pinned_state` authenticates that ownership on two
axes: the comment must be authored by the account backing the token
(`_bot_login`) AND its whole body must be nothing but the state marker
(`PINNED_STATE_BODY_RE`). So neither a third party's forged marker comment nor
an ordinary bot-authored comment that merely embeds the marker in prose can
preempt durable workflow state.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from github import Auth, Github, GithubException
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.Label import Label
from github.PullRequest import PullRequest
from github.Repository import Repository

from orchestrator import analytics, config
from orchestrator.state_machine import (
    ControlLabel,
    WorkflowLabel,
    coerce_child_issue_label,
    coerce_workflow_label,
    guard_transition,
)

log = logging.getLogger(__name__)

PINNED_STATE_MARKER = "<!--orchestrator-state"
PINNED_STATE_RE = re.compile(r"<!--orchestrator-state\s+(\{.*?\})\s*-->", re.DOTALL)
# `read_pinned_state` uses this anchored form so a comment is trusted as state
# only when its ENTIRE body is the marker -- exactly what `write_pinned_state`
# emits. An ordinary orchestrator comment (posted via `_post_issue_comment`,
# whose text is attacker-influenced -- e.g. decomposer rationale) that merely
# embeds a `<!--orchestrator-state ...-->` substring in surrounding prose is
# NOT state-only, so it cannot be mistaken for authoritative state before the
# real state comment exists.
PINNED_STATE_BODY_RE = re.compile(
    r"\A\s*<!--orchestrator-state\s+(\{.*?\})\s*-->\s*\Z", re.DOTALL
)
PINNED_STATE_TEMPLATE = "<!--orchestrator-state {payload}-->"

# GitHub REST status codes the client special-cases: a 403 on check-runs is a
# token-scope problem to surface; a 404 means the resource is already gone.
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404

# Values of the shared check-state model emitted and compared by the
# `_normalize_*` / `_fold_check_states` helpers.
_CHECK_STATE_FAILURE = "failure"
_CHECK_STATE_PENDING = "pending"
# GitHub review and issue/PR states the client keys behavior on.
_REVIEW_CHANGES_REQUESTED = "CHANGES_REQUESTED"
_ISSUE_STATE_OPEN = "open"

# (name, hex color, description) for each workflow label. Order roughly
# tracks the happy-path lifecycle (implementing -> validating ->
# documenting -> in_review) but is otherwise only the order in which
# `ensure_workflow_labels` creates labels on a fresh repo; lifecycle
# routing itself is driven by the stage handlers, not by this tuple.
WORKFLOW_LABEL_SPECS: tuple[tuple[WorkflowLabel, str, str], ...] = (
    (WorkflowLabel.DECOMPOSING, "fbca04", "Orchestrator is breaking this issue into sub-issues"),
    (WorkflowLabel.READY, "0e8a16", "Decomposed and ready for implementation"),
    (WorkflowLabel.BLOCKED, "b60205", "Blocked on another issue"),
    (WorkflowLabel.UMBRELLA, "ededed", "Parent of child issues with no implementation of its own"),
    (WorkflowLabel.IMPLEMENTING, "1d76db", "A coding agent is working on this"),
    (WorkflowLabel.VALIDATING, "8a2be2", "Reviewer agent is checking the diff; verify gate runs on approval"),
    (
        WorkflowLabel.DOCUMENTING,
        "c2e0c6",
        "Documentation pass after reviewer approval (final-docs hop), before in_review",
    ),
    (WorkflowLabel.IN_REVIEW, "d93f0b", "PR is open, awaiting human review"),
    (
        WorkflowLabel.FIXING,
        "fef2c0",
        "Dev fix-loop addressing reviewer changes or in_review PR feedback before re-validation",
    ),
    (
        WorkflowLabel.RESOLVING_CONFLICT,
        "e99695",
        "Resolving an actual rebase conflict (clean rebases route straight to validating)",
    ),
    (WorkflowLabel.QUESTION, "d876e3", "Awaiting a clarifying answer from a human before the orchestrator can advance"),
    (WorkflowLabel.DONE, "cccccc", "Merged to main"),
    (WorkflowLabel.REJECTED, "5c0000", "Issue rejected / closed without merge"),
)
# Source of truth is the enum; assert the spec table stays exhaustive so a
# new `WorkflowLabel` member cannot ship without a color/description (and a
# bootstrap label) here.
assert {spec[0] for spec in WORKFLOW_LABEL_SPECS} == set(WorkflowLabel)
WORKFLOW_LABELS = frozenset(WorkflowLabel)

BACKLOG_LABEL = ControlLabel.BACKLOG
PAUSED_LABEL = ControlLabel.PAUSED
# Applied by `sweep_community_contribution_prs` to any open PR whose author
# is not in `ALLOWED_ISSUE_AUTHORS`. The orchestrator only labels and pings
# HITL once per PR; it never drives the PR's lifecycle, so the label is a
# pure "needs a human" signal rather than a workflow stage.
COMMUNITY_CONTRIBUTION_LABEL = ControlLabel.COMMUNITY_CONTRIBUTION
# Unlike the hard-skip labels, `quick_run` does not pause processing: it stays
# attached and modifies the normal workflow, so it is registered here for
# bootstrap but deliberately absent from `HARD_SKIP_CONTROL_LABELS`.
QUICK_RUN_LABEL = ControlLabel.QUICK_RUN
CONTROL_LABEL_SPECS: tuple[tuple[ControlLabel, str, str], ...] = (
    (
        BACKLOG_LABEL,
        "c5def5",
        "Skip orchestrator processing entirely until the label is removed",
    ),
    (
        PAUSED_LABEL,
        "d4c5f9",
        "Pause an in-flight issue: skip orchestrator processing entirely until the label is removed",
    ),
    (
        COMMUNITY_CONTRIBUTION_LABEL,
        "7057ff",
        "PR opened by an author outside ALLOWED_ISSUE_AUTHORS; human review requested",
    ),
    (
        QUICK_RUN_LABEL,
        "0e8a16",
        "Modify the normal workflow to run in an accelerated quick-run mode; processing continues",
    ),
)


def issue_has_label(issue: Issue, label_name: str) -> bool:
    wanted = (label_name or "").lower()
    return any(
        ((getattr(label, "name", "") or "").lower() == wanted)
        for label in (issue.labels or [])
    )


# Control labels that make the orchestrator ignore an issue for the tick: no
# handler runs, no worktree is rebased, no per-repo/global slot is consumed,
# no stage evaluation is recorded. `backlog` and `paused` share this "hard
# skip" contract (they differ only in operator intent -- a fresh "not yet"
# hold vs. an in-flight pause), so every skip point checks them together.
HARD_SKIP_CONTROL_LABELS: tuple[ControlLabel, ...] = (BACKLOG_LABEL, PAUSED_LABEL)


def hard_skip_control_label(issue: Issue) -> Optional[str]:
    """Return the first hard-skip control label on `issue`, or None.

    The returned member (a `ControlLabel`, hence a plain string) feeds the
    "has %r; skipping" log line so operators see which label parked the
    issue.
    """
    for label in HARD_SKIP_CONTROL_LABELS:
        if issue_has_label(issue, label):
            return label
    return None


def _iter_new_non_pr_issues(
    issues: Iterable[Issue], seen: set[int]
) -> Iterable[Issue]:
    """Yield each non-PR issue in `issues` not already in `seen`, recording it.

    The open poll and every per-label closed-sweep query in
    `list_pollable_issues` share this filter: the Issues API returns pull
    requests too, so PRs are skipped, and `seen` is shared across the queries
    so an issue matching more than one of them surfaces exactly once. `seen`
    is mutated in place, which is what carries that dedup state (and the
    open-before-closed ordering) across the successive `yield from` calls.
    """
    for issue in issues:
        if issue.pull_request is None and issue.number not in seen:
            seen.add(issue.number)
            yield issue


def _issue_query_options(
    *,
    issue_state: str,
    since: Optional[datetime],
    label: Optional[Label] = None,
) -> dict[str, Any]:
    """Build the common open/closed issue query options."""
    options: dict[str, Any] = {
        "state": issue_state,
        "sort": "updated",
        "direction": "desc",
    }
    if label is not None:
        options["labels"] = [label]
    if since is not None:
        options["since"] = since
    return options


def _append_event_line(path, event_record: dict) -> None:
    """Create the parent directory and append one JSONL event line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{json.dumps(event_record, sort_keys=True)}\n")


def _write_event_record(event_record: dict) -> None:
    """Append one JSONL line to `config.EVENT_LOG_PATH` if configured.

    Shared by the real client and the test fake so a temp-file-backed
    assertion against the fake exercises the same write path the
    production sink uses. No-op when EVENT_LOG_PATH is unset, preserving
    the legacy "no event file is touched" behavior.
    """
    path = config.EVENT_LOG_PATH
    if path is None:
        return
    try:
        _append_event_line(path, event_record)
    except OSError as error:
        log.warning("could not write event log %s: %s", path, error)


def build_event_record(
    *, repo: str, issue_number: int, event: str,
    stage: Optional[str] = None,
    **extras: Any,
) -> dict:
    """Build a structured event record. UTC timestamp, second precision.

    `stage` is omitted when None so audit-only events that have no natural
    stage (rare; today every emitter passes one) do not carry a `null`
    field. Extra fields whose value is None are likewise dropped so callers
    can pass optional context (`session_id`, `review_round`, `retry_count`,
    ...) unconditionally without polluting records that don't carry them.
    """
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": repo,
        "issue": int(issue_number),
        "event": event,
    }
    if stage is not None:
        rec["stage"] = stage
    for field_name, field_value in extras.items():
        if field_value is not None:
            rec[field_name] = field_value
    return rec


@dataclass
class PinnedState:
    comment_id: Optional[int] = None
    data: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, state_value: Any) -> None:
        self.data[key] = state_value


def _pinned_state_from_comment(
    issue_comment: IssueComment,
    *,
    trusted_login: Optional[str],
    issue_number: int,
) -> Optional[PinnedState]:
    """Parse one authenticated, state-only pinned comment candidate."""
    body = issue_comment.body or ""
    if PINNED_STATE_MARKER not in body:
        return None
    author_login = getattr(
        getattr(issue_comment, "user", None), "login", None,
    )
    if trusted_login is not None and author_login != trusted_login:
        return None
    state_match = PINNED_STATE_BODY_RE.match(body)
    if state_match is None:
        return None
    try:
        parsed_state = json.loads(state_match.group(1))
    except json.JSONDecodeError:
        log.warning("issue=#%s pinned state JSON unparseable", issue_number)
        parsed_state = {}
    return PinnedState(comment_id=issue_comment.id, data=parsed_state)


@dataclass(frozen=True)
class _CheckSurfaceRead:
    """Normalized state and read outcome for one GitHub checks surface."""

    state: Optional[str] = None
    read_failed: bool = False


_FAILED_CHECK_RUN_CONCLUSIONS = frozenset(
    (_CHECK_STATE_FAILURE, "timed_out", "action_required", "cancelled")
)
_SUCCESSFUL_CHECK_RUN_CONCLUSIONS = frozenset(
    ("success", "neutral", "skipped")
)


def _normalize_combined_status(combined_status: Any) -> Optional[str]:
    """Convert a legacy combined status into the shared check-state model."""
    status = combined_status.state
    if not status or (status == _CHECK_STATE_PENDING and not combined_status.total_count):
        return None
    return _CHECK_STATE_FAILURE if status == "error" else status


def _normalize_check_runs(check_runs: Iterable[Any]) -> Optional[str]:
    """Convert check-run conclusions into the shared check-state model."""
    conclusions = {check_run.conclusion for check_run in check_runs}
    if not conclusions:
        return None
    if None in conclusions:
        return _CHECK_STATE_PENDING
    if conclusions & _FAILED_CHECK_RUN_CONCLUSIONS:
        return _CHECK_STATE_FAILURE
    if conclusions <= _SUCCESSFUL_CHECK_RUN_CONCLUSIONS:
        return "success"
    return _CHECK_STATE_FAILURE


def _fold_check_states(
    states: Iterable[Optional[str]],
    *,
    read_failed: bool,
) -> str:
    """Fold normalized surfaces using failure-before-pending priority."""
    observed_states = [state for state in states if state]
    if observed_states and read_failed:
        observed_states.append(_CHECK_STATE_PENDING)
    if not observed_states:
        return "none"
    if _CHECK_STATE_FAILURE in observed_states:
        return _CHECK_STATE_FAILURE
    if _CHECK_STATE_PENDING in observed_states:
        return _CHECK_STATE_PENDING
    return "success"


def _review_state_for_head(
    review: Any, head_sha: str,
) -> Optional[tuple[str, tuple[int, str]]]:
    """Return a reviewer-keyed state record when a review applies to HEAD."""
    if (getattr(review, "commit_id", "") or "") != head_sha:
        return None
    review_state = (review.state or "").upper()
    if review_state not in ("APPROVED", _REVIEW_CHANGES_REQUESTED, "DISMISSED"):
        return None
    reviewer_login = review.user.login if review.user else ""
    if not reviewer_login:
        return None
    review_id = getattr(review, "id", 0) or 0
    return reviewer_login, (review_id, review_state)


def _record_latest_review(
    latest_per_user: dict[str, tuple[int, str]],
    candidate: tuple[str, tuple[int, str]],
) -> None:
    """Retain the highest-id review record for one reviewer."""
    reviewer_login, review_record = candidate
    previous = latest_per_user.get(reviewer_login)
    if previous is None or review_record[0] > previous[0]:
        latest_per_user[reviewer_login] = review_record


def _is_actionable_review_summary(
    review: Any, after_id: Optional[int],
) -> bool:
    """Whether a review summary carries unread feedback for the developer."""
    review_state = (review.state or "").upper()
    if review_state not in (_REVIEW_CHANGES_REQUESTED, "COMMENTED"):
        return False
    if not (review.body or "").strip():
        return False
    return after_id is None or review.id > after_id


# Cap on the in-memory `recorded_events` tail so a long-running process
# cannot grow it unbounded; the JSONL sink keeps the full history.
_RECORDED_EVENTS_CAP = 500


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_spec: Optional["config.RepoSpec"] = None,
        *,
        bot_login: Optional[str] = None,
    ):
        # `repo_spec` wins when both are passed -- the multi-repo caller in
        # main.py threads a spec; legacy callers (and tests) still use the
        # `repo_slug` shortcut against the single-repo default.
        if repo_spec is None:
            slug = repo_slug or config.REPO
        else:
            slug = repo_spec.slug
        # Resolve per-slug at construction time rather than reusing the
        # cached `config.GITHUB_TOKEN` (which was looked up once for
        # `config.REPO`), so a multi-repo deployment with one token file
        # per slug under `~/.config/<owner>/<repo>/token` actually picks
        # up the right token for each spec. Legacy single-repo callers
        # see identical behavior because `_resolve_github_token(REPO)`
        # returns the same value.
        if token is None:
            token = config._resolve_github_token(slug)
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is empty. Export it in the orchestrator's "
                "environment or write it to "
                f"~/.config/{slug}/token "
                "(override path with ORCHESTRATOR_TOKEN_FILE). "
                "Do NOT put it in REPO_ROOT/.env -- the implementer agent "
                "can read that file."
            )
        self._gh = Github(auth=Auth.Token(token))
        self.repo: Repository = self._gh.get_repo(slug)
        self._repo_slug = slug
        # Retained so `_for_worker_thread` can build a fresh client without
        # re-reading the on-disk token file (which would mask a token rotation
        # mid-tick anyway -- a tick is short, the token does not change under
        # it). Treated as an internal detail; callers should not poke at it.
        self._token = token
        # Login of the account backing this token. `read_pinned_state` trusts
        # only marker comments authored by this login, so a third party who can
        # comment on the issue cannot preempt durable workflow state with a
        # forged `<!--orchestrator-state ...-->` comment (CWE-345). Legacy
        # pinned comments were authored by this same account, so author
        # matching keeps honoring them with no migration. Resolved once here;
        # `_for_worker_thread` threads the value into per-worker clones so the
        # parallel path adds no `GET /user` call per worker.
        self._bot_login = (
            self._gh.get_user().login if bot_login is None else bot_login
        )
        # In-memory tail of recently-emitted stage-transition events. Capped
        # so a long-running process can't grow this list unbounded; the file
        # at `config.EVENT_LOG_PATH` (when configured) is the durable record.
        # FakeGitHubClient mirrors this attribute so workflow tests can read
        # captured events without touching disk.
        self.recorded_events: list[dict] = []
        # Per-name cache of resolved workflow Label objects (see
        # `_cached_label`). Workflow labels are immutable after
        # `ensure_workflow_labels`, so the closed-issue sweep can stop
        # re-fetching them every tick.
        self._label_cache: dict[str, Label] = {}
        # Count of `list_pollable_issues` calls on this client. Because that
        # method is invoked exactly once per repo per tick, this doubles as a
        # tick counter and drives the closed-issue-sweep cadence throttle
        # (`config.CLOSED_ISSUE_SWEEP_EVERY_N_TICKS`).
        self._pollable_calls = 0

    def _for_worker_thread(self) -> "GitHubClient":
        """Build a fresh GitHubClient for a single worker thread.

        PyGithub's `Requester` holds mutable per-request state (the URL,
        headers and body being assembled for the next call, the active
        connection, the last-seen rate-limit headers) and the library does
        not document its objects as thread-safe. Sharing one GitHubClient
        across `workflow.tick`'s parallel-path worker threads can interleave
        two concurrent calls' request setup and corrupt the operations the
        orchestrator issues against GitHub (the wrong issue's labels
        updated, comment bodies cross-pollinated, rate-limit accounting
        trampled). A fresh `Github` + `Requester` + `Repository` per worker
        isolates each thread to its own requester so any in-flight HTTP
        call is the sole consumer of that requester's state.

        Token + slug are reused so the new instance has identical auth and
        target repo. `bot_login` is threaded through so the clone authenticates
        pinned state against the same account without re-issuing `GET /user`
        per worker. The in-memory `recorded_events` tail starts empty per
        worker; the durable JSONL sink at `config.EVENT_LOG_PATH` is the
        cross-worker record and write_event_record's open/append is what
        carries event ordering across threads.
        """
        return GitHubClient(
            token=self._token,
            repo_slug=self._repo_slug,
            bot_login=self._bot_login,
        )

    def _cached_label(self, name: str) -> Optional[Label]:
        """Resolve a workflow Label object, caching successes for this client.

        The closed-issue sweep in `list_pollable_issues` needs Label OBJECTS
        because PyGithub's `get_issues(labels=...)` reads `label.name`.
        Workflow labels are created once by `ensure_workflow_labels` and are
        never mutated by the orchestrator, so re-fetching each one every tick
        is pure waste -- on a multi-repo deployment those
        `GET /repos/.../labels/<name>` calls are a large fraction of the
        per-tick request volume that exhausts the GitHub primary rate limit.
        Cache the resolved object so each label is fetched at most once per
        repo client.

        Failures are NOT cached: a missing label (under-scoped PAT, or one not
        yet created) keeps being retried every tick exactly as before, so
        fixing the PAT or creating the label takes effect without a restart.
        Returns None on a lookup failure so the caller can skip that label's
        sweep instead of raising out of the generator.
        """
        cached = self._label_cache.get(name)
        if cached is not None:
            return cached
        try:
            label_obj = self.repo.get_label(name)
        except GithubException as error:
            log.warning(
                "could not look up %r label for closed-issue sweep "
                "(HTTP %s); skipping. Externally-merged %s issues will "
                "not finalize to `done` until the label exists.",
                name, error.status, name,
            )
            return None
        self._label_cache[name] = label_obj
        return label_obj

    def list_pollable_issues(self, since: Optional[datetime] = None) -> Iterable[Issue]:
        """Open issues plus closed issues still labeled with any non-terminal
        workflow label.

        The closed-issue sweep is what makes the manual-merge path work:
        when a human merges a PR with a `Resolves #N` footer, GitHub
        closes the linked issue automatically. Without this sweep the
        next tick would not see issue #N at all and the dispatcher could
        never finalize the workflow label to `done`. Once flipped the
        issue no longer carries either sweep label, so the cost stays
        bounded in steady state.

        `fixing` and `resolving_conflict` are included alongside
        `in_review` because an external merge can land while the
        orchestrator is mid-fix or mid-resolution too: `Resolves #N`
        closes the issue, the PR moves to merged, and the matching
        handler's terminal branch finalizes the label -- but only if
        the closed issue actually surfaces here.

        `implementing`, `documenting`, and `validating` join the sweep
        for the same reason: a human who merges a PR early closes the
        issue, and the per-stage handler's `_finalize_if_pr_merged`
        check (added for these labels alongside the legacy in_review /
        fixing / resolving_conflict terminals) flips the label to
        `done`. Without the sweep that finalize would never fire on a
        closed issue stuck at an early stage, and a parent umbrella
        would aggregate on the stale label forever.

        `question` joins the sweep so a human closing an open Q&A thread
        is recognized as a terminal signal: `_handle_question` finalizes
        the issue to `done` and cleans up the per-issue worktree/branch
        instead of letting an answered-but-then-closed question keep its
        worktree on disk indefinitely.
        """
        seen: set[int] = set()
        self._pollable_calls += 1
        yield from _iter_new_non_pr_issues(
            self.repo.get_issues(
                **_issue_query_options(issue_state=_ISSUE_STATE_OPEN, since=since)
            ),
            seen,
        )

        # The closed-issue recovery sweep below issues one GET per non-terminal
        # label, per repo. That fixed cost -- paid every tick regardless of how
        # much real work exists -- dominates request volume on multi-repo
        # deployments and exhausts GitHub's primary rate limit (see
        # `config.CLOSED_ISSUE_SWEEP_EVERY_N_TICKS`). Run it on the first call
        # (so startup recovery is never delayed) and then once every N ticks;
        # the open-issue poll above always runs. `<= 1` keeps it every tick.
        every = config.CLOSED_ISSUE_SWEEP_EVERY_N_TICKS
        if every > 1 and (self._pollable_calls - 1) % every != 0:
            return

        # PyGithub's Repository.get_issues(labels=...) expects Label OBJECTS
        # and reads `label.name`; passing a raw string list raises a
        # TypeError before the sweep yields anything. Because that exception
        # propagates out of this generator when the sweep query is iterated
        # -- past the per-issue try/except in `tick()` -- it would silently
        # break every tick after open issues processed and leave externally-
        # merged in_review issues stuck closed-but-labeled forever. Resolve each
        # Label via the per-client cache (`_cached_label`); treat a missing
        # label as "nothing to sweep" and skip rather than raising.
        # Multi-label-OR is achieved by issuing one query per label (the
        # GitHub Issues API treats `labels` as AND, not OR).
        for label_name in (
            WorkflowLabel.IMPLEMENTING, WorkflowLabel.DOCUMENTING,
            WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING, WorkflowLabel.RESOLVING_CONFLICT,
            WorkflowLabel.QUESTION,
        ):
            label_obj = self._cached_label(label_name)
            if label_obj is None:
                continue
            yield from _iter_new_non_pr_issues(
                self.repo.get_issues(
                    **_issue_query_options(
                        issue_state="closed",
                        since=since,
                        label=label_obj,
                    )
                ),
                seen,
            )

    @staticmethod
    def workflow_label(issue: Issue) -> Optional[WorkflowLabel]:
        for lbl in issue.labels:
            if lbl.name in WORKFLOW_LABELS:
                return WorkflowLabel(lbl.name)
        return None

    def set_workflow_label(self, issue: Issue, new_label: Optional[str]) -> None:
        # Typo guard (always strict): a label not in `WorkflowLabel` is
        # always a bug -- raise rather than apply a literal label that the
        # next tick would silently treat as unlabeled-pickup. Accepts a
        # `WorkflowLabel` or its string value.
        new = coerce_workflow_label(new_label) if new_label else None
        if new is not None:
            # Transition guard (mode-controlled, default `warn`): reject an
            # illegal `current -> new` relabel. Read the current label off
            # the live issue first; a same-label re-set is always allowed.
            guard_transition(
                self.workflow_label(issue), new, config.WORKFLOW_TRANSITION_GUARD,
            )
        keep = [
            issue_label.name
            for issue_label in issue.labels
            if issue_label.name not in WORKFLOW_LABELS
        ]
        if new is not None:
            keep.append(new)
        issue.set_labels(*keep)
        if new is not None:
            self._emit_stage_enter(issue, new)

    def emit_event(
        self,
        event: str,
        *,
        issue_number: int,
        stage: Optional[str] = None,
        **extras: Any,
    ) -> None:
        """Record a structured event and -- when EVENT_LOG_PATH is set --
        append it to the JSONL sink.

        Generalizes `_emit_stage_enter` so workflow handlers can emit
        per-stage audit events (`agent_spawn`, `agent_exit`,
        `review_verdict`, `park_awaiting_human`) through a single
        chokepoint without per-handler file IO. The in-memory tail is
        capped so a long-running process can't grow it unbounded; the
        file is the durable record.
        """
        record = build_event_record(
            repo=self._repo_slug,
            issue_number=issue_number,
            event=event,
            stage=stage,
            **extras,
        )
        self.recorded_events.append(record)
        if len(self.recorded_events) > _RECORDED_EVENTS_CAP:
            self.recorded_events = self.recorded_events[-_RECORDED_EVENTS_CAP:]
        _write_event_record(record)

    def _emit_stage_enter(self, issue: Issue, stage: str) -> None:
        """Record a `stage_enter` event for `issue` transitioning to `stage`.

        Centralized hook called from `set_workflow_label` so every callsite
        emits identically without per-handler bookkeeping. The audit event
        lands on `EVENT_LOG_PATH` via `emit_event`; an analytics-compatible
        copy lands on `ANALYTICS_LOG_PATH` so non-agent stages contribute
        timing context to the same sink `_run_agent_tracked` writes to.
        Both sinks are independently opt-in/out via their respective
        config knobs; pinned GitHub state stays authoritative regardless.
        """
        issue_number = getattr(issue, "number", 0) or 0
        self.emit_event(
            "stage_enter",
            issue_number=issue_number,
            stage=stage,
        )
        analytics.record_stage_enter(
            repo=self._repo_slug,
            issue=issue_number,
            stage=stage,
        )

    def comment(self, issue: Issue, body: str) -> IssueComment:
        return issue.create_comment(body)

    def get_issue(self, number: int) -> Issue:
        return self.repo.get_issue(number)

    def create_child_issue(
        self,
        *,
        title: str,
        body: str,
        parent_number: int,
        labels: list[str],
    ) -> Issue:
        """Create a sub-issue in the same repo, with a `Parent: #<n>` link.

        Deliberately does NOT use a `Resolves #<parent>` keyword: GitHub
        would auto-close the parent the moment the child PR merges (when
        the parent has only this one open child reference), bypassing
        `_handle_blocked`'s aggregation across siblings. A plain
        `Parent: #<n>` line keeps the parent open until every child
        resolves and `_handle_blocked` flips the parent to `ready`.
        """
        # Typo guard for this direct label write path (bypasses
        # `set_workflow_label`): each label is an orchestrator-authored
        # workflow label -- or the `quick_run` modifier propagated from a
        # split parent -- so coerce each and let a typo fail loudly instead
        # of creating a child with an invisible literal label.
        validated = [coerce_child_issue_label(lbl) for lbl in labels]
        parent_body = (body or "").rstrip()
        full_body = f"{parent_body}\n\nParent: #{parent_number}"
        return self.repo.create_issue(title=title, body=full_body, labels=validated)

    def read_pinned_state(self, issue: Issue) -> PinnedState:
        # Durable workflow state must be authenticated to the orchestrator's
        # own state comment on two axes, because the FIRST marker comment by
        # document order would otherwise preempt the real pinned state and
        # steer agent session fields, branch/PR selection, and terminal branch
        # cleanup (CWE-345):
        #   1. Author -- trust only comments posted by `_bot_login`, so a third
        #      party who posts (or edits an older comment to carry) the marker
        #      cannot win.
        #   2. State-only body -- trust only a comment whose ENTIRE body is the
        #      marker (`PINNED_STATE_BODY_RE`), what `write_pinned_state` emits.
        #      An ordinary bot-authored comment (`_post_issue_comment` posts
        #      decomposer/agent text that is attacker-influenced, and does so
        #      BEFORE the real state comment exists on a manually-labeled issue)
        #      that merely embeds a `<!--orchestrator-state ...-->` substring in
        #      prose is not state-only, so it cannot be mistaken for state.
        # `_bot_login` is absent only on clients built via `__new__` in tests --
        # there the author axis degrades open, but the state-only body axis
        # still holds.
        trusted_login = getattr(self, "_bot_login", None)
        for issue_comment in issue.get_comments():
            pinned_state = _pinned_state_from_comment(
                issue_comment,
                trusted_login=trusted_login,
                issue_number=issue.number,
            )
            if pinned_state is not None:
                return pinned_state
        return PinnedState()

    def write_pinned_state(self, issue: Issue, state: PinnedState) -> PinnedState:
        body = PINNED_STATE_TEMPLATE.format(
            payload=json.dumps(state.data, sort_keys=True)
        )
        if state.comment_id is None:
            created = issue.create_comment(body)
            state.comment_id = created.id
            return state
        for issue_comment in issue.get_comments():
            if issue_comment.id == state.comment_id:
                issue_comment.edit(body)
                return state
        # Pinned comment was deleted out from under us; recreate.
        created = issue.create_comment(body)
        state.comment_id = created.id
        return state

    def comments_after(
        self, issue: Issue, after_id: Optional[int]
    ) -> list[IssueComment]:
        issue_comments: list[IssueComment] = []
        for issue_comment in issue.get_comments():
            if PINNED_STATE_MARKER in (issue_comment.body or ""):
                continue
            if after_id is None or issue_comment.id > after_id:
                issue_comments.append(issue_comment)
        return issue_comments

    def latest_comment_id(self, issue: Issue) -> Optional[int]:
        latest: Optional[int] = None
        for issue_comment in issue.get_comments():
            if latest is None or issue_comment.id > latest:
                latest = issue_comment.id
        return latest

    def open_pr(
        self, *, branch: str, base: str, title: str, body: str
    ) -> PullRequest:
        return self.repo.create_pull(title=title, body=body, head=branch, base=base)

    def pr_comment(self, pr_number: int, body: str) -> IssueComment:
        return self.repo.get_pull(pr_number).create_issue_comment(body)

    def find_open_pr(self, *, branch: str, base: str) -> Optional[PullRequest]:
        """Return an open PR with the given head branch, or None.

        Used to recover after a crash between create_pull and relabeling:
        a duplicate create_pull would 422 and trap the issue in implementing.
        """
        owner_login = self.repo.owner.login
        head = f"{owner_login}:{branch}"
        return next(
            iter(self.repo.get_pulls(
                state=_ISSUE_STATE_OPEN, head=head, base=base,
            )),
            None,
        )

    def iter_open_prs(self) -> Iterable[PullRequest]:
        """Yield every open PR on the repo, regardless of head branch.

        Used by the community-contribution sweep, which does not know branch
        names up front and only needs author + labels off each PR. Errors
        from PyGithub propagate; the caller (the sweep) catches them so a
        single bad enumeration cannot break the polling tick.
        """
        for pr in self.repo.get_pulls(state=_ISSUE_STATE_OPEN):
            yield pr

    @staticmethod
    def pr_has_label(pr: PullRequest, label_name: str) -> bool:
        wanted = (label_name or "").lower()
        return any(
            ((getattr(label, "name", "") or "").lower() == wanted)
            for label in (pr.labels or [])
        )

    def add_pr_label(self, pr: PullRequest, label_name: str) -> None:
        """Add a single label to an open PR. Idempotent at the GitHub layer."""
        pr.add_to_labels(label_name)

    def get_pr(self, pr_number: int) -> PullRequest:
        return self.repo.get_pull(pr_number)

    @staticmethod
    def pr_state(pr: PullRequest) -> str:
        """Return one of 'merged', 'closed', 'open'."""
        if pr.merged:
            return "merged"
        if pr.state == "closed":
            return "closed"
        return _ISSUE_STATE_OPEN

    @staticmethod
    def pr_is_mergeable(pr: PullRequest) -> Optional[bool]:
        """`pr.mergeable` is computed lazily by GitHub. None means "not yet",
        not "no" -- callers should wait a tick rather than treating it as a
        hard failure. We refresh once if the cached value is None.
        """
        if pr.mergeable is None:
            try:
                pr.update()
            except GithubException:
                return None
        return pr.mergeable

    def pr_combined_check_state(self, pr: PullRequest) -> str:
        """Return one of 'success', 'pending', 'failure', 'none'.

        Combines the legacy combined-status API (commit statuses) with the
        check-runs API (GitHub Actions, third-party Apps). Either source is
        sufficient to mark the head 'success'; either failing is failure;
        a pending in either source pends the whole. 'none' means there are
        no checks configured at all (ambiguous -- caller refuses to merge).

        Fails closed on a partial read: when one surface returned a usable
        signal but the other surface raised, the unread surface is treated
        as 'pending' so the result downgrades from 'success' to 'pending'.
        Without that, a single green commit-status context plus failing or
        pending GitHub Actions check-runs that the PAT cannot read (403 on
        check-runs from a missing 'Checks: read' scope, or a transient 5xx)
        would be reported as 'success' and a caller could trust the head
        as green over the unread failing checks.
        """
        head_sha = pr.head.sha
        combined_surface = self._read_combined_status(head_sha)
        check_run_surface = self._read_check_runs(head_sha)
        return _fold_check_states(
            (combined_surface.state, check_run_surface.state),
            read_failed=(
                combined_surface.read_failed or check_run_surface.read_failed
            ),
        )

    def _read_combined_status(self, head_sha: str) -> _CheckSurfaceRead:
        """Read and normalize the legacy commit-status surface."""
        try:
            combined = self.repo.get_commit(head_sha).get_combined_status()
        except GithubException as error:
            log.warning(
                "could not read combined status for %s (HTTP %s); ignoring",
                head_sha, error.status,
            )
            return _CheckSurfaceRead(read_failed=True)
        return _CheckSurfaceRead(state=_normalize_combined_status(combined))

    def _read_check_runs(self, head_sha: str) -> _CheckSurfaceRead:
        """Read and normalize the check-runs surface."""
        try:
            return _CheckSurfaceRead(state=_normalize_check_runs(
                self.repo.get_commit(head_sha).get_check_runs(),
            ))
        except GithubException as error:
            # 403 here almost always means the fine-grained PAT is missing
            # 'Checks: read'. For Actions-only PRs (no commit statuses,
            # only check-runs), swallowing this silently leaves
            # `pr_combined_check_state` at 'none' despite the PR actually
            # being green; surface the remediation prominently so an
            # operator can fix the scope.
            if error.status == _HTTP_FORBIDDEN:
                log.error(
                    "could not read check-runs for %s (HTTP 403). The "
                    "orchestrator PAT needs 'Checks: read' to evaluate "
                    "GitHub Actions PRs. Without it, check_state is "
                    "reported as 'none' on Actions-only PRs. Add the "
                    "permission and restart.",
                    head_sha,
                )
            else:
                log.warning(
                    "could not read check-runs for %s (HTTP %s); ignoring",
                    head_sha, error.status,
                )
            return _CheckSurfaceRead(read_failed=True)

    @staticmethod
    def _latest_review_states_for_head(
        pr: PullRequest, *, head_sha: str
    ) -> list[str]:
        """Latest review state per reviewer, restricted to `head_sha`.

        Approvals on older commits are treated as stale -- a commit pushed
        after a human approval must not advertise the PR as ready unless
        the human re-reviews the new head.
        """
        if not head_sha:
            return []
        latest_per_user: dict[str, tuple[int, str]] = {}
        for review in pr.get_reviews():
            candidate = _review_state_for_head(review, head_sha)
            if candidate is not None:
                _record_latest_review(latest_per_user, candidate)
        return [
            review_state
            for _, review_state in latest_per_user.values()
        ]

    @classmethod
    def pr_has_changes_requested(
        cls, pr: PullRequest, *, head_sha: str
    ) -> bool:
        """True if any reviewer's latest review on `head_sha` is
        CHANGES_REQUESTED. A human veto on the current head must block
        the in_review ready-for-merge ping.
        """
        return any(
            review_state == _REVIEW_CHANGES_REQUESTED
            for review_state in cls._latest_review_states_for_head(
                pr, head_sha=head_sha,
            )
        )

    @classmethod
    def pr_is_approved(cls, pr: PullRequest, *, head_sha: str) -> bool:
        """True iff at least one APPROVED review exists for `head_sha` and no
        review on `head_sha` says CHANGES_REQUESTED.
        """
        states = cls._latest_review_states_for_head(pr, head_sha=head_sha)
        if not states:
            return False
        if any(review_state == _REVIEW_CHANGES_REQUESTED for review_state in states):
            return False
        return any(review_state == "APPROVED" for review_state in states)

    def delete_remote_branch(self, branch: str) -> bool:
        """Delete the remote `<branch>` ref from the repo.

        Idempotent: a 404 (ref already gone) is treated as success because
        the repo's "Automatically delete head branches" setting may have
        removed the branch as part of the merge call. Other failures are
        logged and swallowed so a tidy-up step never raises out of the
        merge handler.
        """
        try:
            self.repo.get_git_ref(f"heads/{branch}").delete()
        except GithubException as error:
            if error.status == _HTTP_NOT_FOUND:
                return True
            log.warning(
                "could not delete remote branch %r (HTTP %s): %s",
                branch, error.status, error.data,
            )
            return False
        return True

    def merge_pr(
        self, pr: PullRequest, *, sha: str, method: str = "squash"
    ) -> bool:
        """SHA-pinned merge so a commit landing between our checks and the
        merge call cannot slip through unreviewed. PyGithub returns 409 if the
        head moved; we treat 405 (not mergeable) / 409 (sha mismatch) /
        422 (conflicts) as 'wait a tick' rather than retrying blind.
        """
        try:
            pr.merge(sha=sha, merge_method=method)
        except GithubException as error:
            log.warning(
                "merge failed for PR #%s (HTTP %s): %s",
                pr.number, error.status, error.data,
            )
            return False
        return True

    def pr_conversation_comments_after(
        self, pr: PullRequest, after_id: Optional[int]
    ) -> list[IssueComment]:
        """PR conversation comments (the `/issues/N/comments` resource) newer
        than `after_id`. These share the IssueComment id space with
        `issue.get_comments()`, so callers may use a single watermark across
        both. Inline review comments live in a separate id space and need
        `pr_inline_comments_after`.
        """
        out: list[IssueComment] = []
        for pr_comment in pr.get_issue_comments():
            if PINNED_STATE_MARKER in (pr_comment.body or ""):
                continue
            if after_id is None or pr_comment.id > after_id:
                out.append(pr_comment)
        out.sort(key=lambda comment: comment.id)
        return out

    def pr_inline_comments_after(
        self, pr: PullRequest, after_id: Optional[int]
    ) -> list:
        """Inline PR review comments (`/pulls/N/comments`) newer than
        `after_id`. These are PullRequestComment objects with their own id
        space, distinct from IssueComment ids -- mixing the two namespaces
        under one watermark drops or replays comments, so this method takes
        a separate watermark from the issue-comment side.
        """
        out: list = []
        for review_comment in pr.get_review_comments():
            if PINNED_STATE_MARKER in (review_comment.body or ""):
                continue
            if after_id is None or review_comment.id > after_id:
                out.append(review_comment)
        out.sort(key=lambda comment: comment.id)
        return out

    def pr_reviews_after(
        self, pr: PullRequest, after_id: Optional[int]
    ) -> list:
        """PR review summaries (`pr.get_reviews()`) newer than `after_id`,
        filtered to states whose body is actionable feedback for the dev:
        CHANGES_REQUESTED and COMMENTED. APPROVED is excluded -- the human
        approved, the body is informational. DISMISSED / PENDING never count.
        Empty bodies are dropped because there is nothing to forward.

        These objects live in the PullRequestReview id namespace, distinct
        from the IssueComment and PullRequestComment id spaces -- the
        in_review handler tracks `pr_last_review_summary_id` separately.

        Without this surface, a 'Comment' review with a request in the body
        is silently ignored and the PR may be pinged ready for human
        merge over it, and a CHANGES_REQUESTED review with body but no
        inline comments only blocks the ready-ping via
        `pr_has_changes_requested` without ever reaching the dev agent.
        """
        out = [
            candidate_review
            for candidate_review in pr.get_reviews()
            if _is_actionable_review_summary(candidate_review, after_id)
        ]
        out.sort(key=lambda review_summary: review_summary.id)
        return out

    def ensure_workflow_labels(self) -> None:
        """Create any missing workflow/control labels on the repo. Idempotent.

        Best-effort: a 403 (under-scoped PAT) logs a clear instruction and
        returns without raising, so the polling loop keeps running. The user
        can fix the PAT scopes without restarting.
        """
        try:
            existing = {
                repo_label.name for repo_label in self.repo.get_labels()
            }
        except GithubException as error:
            log.warning(
                "could not list labels (HTTP %s); skipping label bootstrap. "
                "Grant the PAT 'Issues: Read and write' to enable.",
                error.status,
            )
            return
        for name, color, description in (
            WORKFLOW_LABEL_SPECS + CONTROL_LABEL_SPECS
        ):
            if name in existing:
                continue
            try:
                self.repo.create_label(name=name, color=color, description=description)
            except GithubException as error:
                log.error(
                    "could not create label %r (HTTP %s). "
                    "Fine-grained PAT needs 'Issues: Read and write'. "
                    "Skipping remaining label bootstrap; orchestrator will keep "
                    "running and may retry on the next restart.",
                    name, error.status,
                )
                return
            log.info("created label %r", name)
