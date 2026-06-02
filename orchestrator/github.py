# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Thin GitHub client built on PyGithub.

Per-issue state is stored in a single 'pinned' comment whose body matches
PINNED_STATE_RE. The orchestrator owns this comment and only edits it from
write_pinned_state.
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
from github.PullRequest import PullRequest
from github.Repository import Repository

from . import analytics, config

log = logging.getLogger(__name__)

PINNED_STATE_MARKER = "<!--orchestrator-state"
PINNED_STATE_RE = re.compile(r"<!--orchestrator-state\s+(\{.*?\})\s*-->", re.DOTALL)
PINNED_STATE_TEMPLATE = "<!--orchestrator-state {payload}-->"

# (name, hex color, description) for each workflow label. Order roughly
# tracks the happy-path lifecycle (implementing -> validating ->
# documenting -> in_review) but is otherwise only the order in which
# `ensure_workflow_labels` creates labels on a fresh repo; lifecycle
# routing itself is driven by the stage handlers, not by this tuple.
WORKFLOW_LABEL_SPECS: tuple[tuple[str, str, str], ...] = (
    ("decomposing", "fbca04", "Orchestrator is breaking this issue into sub-issues"),
    ("ready", "0e8a16", "Decomposed and ready for implementation"),
    ("blocked", "b60205", "Blocked on another issue"),
    ("umbrella", "ededed", "Parent of child issues with no implementation of its own"),
    ("implementing", "1d76db", "A coding agent is working on this"),
    ("validating", "8a2be2", "Automated review/tests are running"),
    ("documenting", "c2e0c6", "Documentation pass after reviewer approval (final-docs hop), before in_review"),
    ("in_review", "d93f0b", "PR is open, awaiting human review"),
    ("fixing", "fef2c0", "Addressing PR feedback before the next reviewer round"),
    ("resolving_conflict", "e99695", "Auto-resolving merge conflicts after a sibling PR landed first"),
    ("question", "d876e3", "Awaiting a clarifying answer from a human before the orchestrator can advance"),
    ("done", "cccccc", "Merged to main"),
    ("rejected", "5c0000", "Issue rejected / closed without merge"),
)
WORKFLOW_LABELS = frozenset(name for name, _, _ in WORKFLOW_LABEL_SPECS)

BASE_SYNC_HOLD_LABEL = "hold_base_sync"
BACKLOG_LABEL = "backlog"
CONTROL_LABEL_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        BASE_SYNC_HOLD_LABEL,
        "5319e7",
        "Pause automatic base sync, conflict resolution, and auto-merge",
    ),
    (
        BACKLOG_LABEL,
        "c5def5",
        "Skip orchestrator processing entirely until the label is removed",
    ),
)


def issue_has_label(issue: Issue, label_name: str) -> bool:
    wanted = (label_name or "").lower()
    return any(
        ((getattr(label, "name", "") or "").lower() == wanted)
        for label in (issue.labels or [])
    )


def _write_event_record(record: dict) -> None:
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        log.warning("could not write event log %s: %s", path, e)


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
    for k, v in extras.items():
        if v is not None:
            rec[k] = v
    return rec


@dataclass
class PinnedState:
    comment_id: Optional[int] = None
    data: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_spec: Optional["config.RepoSpec"] = None,
    ):
        # `repo_spec` wins when both are passed -- the multi-repo caller in
        # main.py threads a spec; legacy callers (and tests) still use the
        # `repo_slug` shortcut against the single-repo default.
        if repo_spec is not None:
            slug = repo_spec.slug
        else:
            slug = repo_slug or config.REPO
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
        # In-memory tail of recently-emitted stage-transition events. Capped
        # so a long-running process can't grow this list unbounded; the file
        # at `config.EVENT_LOG_PATH` (when configured) is the durable record.
        # FakeGitHubClient mirrors this attribute so workflow tests can read
        # captured events without touching disk.
        self.recorded_events: list[dict] = []

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
        target repo. The in-memory `recorded_events` tail starts empty per
        worker; the durable JSONL sink at `config.EVENT_LOG_PATH` is the
        cross-worker record and write_event_record's open/append is what
        carries event ordering across threads.
        """
        return GitHubClient(token=self._token, repo_slug=self._repo_slug)

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

        kwargs: dict[str, Any] = {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
        }
        if since is not None:
            kwargs["since"] = since
        for issue in self.repo.get_issues(**kwargs):
            if issue.pull_request is None and issue.number not in seen:
                seen.add(issue.number)
                yield issue

        # PyGithub's Repository.get_issues(labels=...) expects Label OBJECTS
        # and reads `label.name`; passing a raw string list raises a
        # TypeError before the sweep yields anything. Because that exception
        # propagates out of this generator on the second `for` -- past the
        # per-issue try/except in `tick()` -- it would silently break every
        # tick after open issues processed and leave externally-merged
        # in_review issues stuck closed-but-labeled forever. Look up
        # each Label once per call; treat a missing label as "nothing to
        # sweep" and skip rather than raising. Multi-label-OR is achieved
        # by issuing one query per label (the GitHub Issues API treats
        # `labels` as AND, not OR).
        for label_name in (
            "implementing", "documenting", "validating",
            "in_review", "fixing", "resolving_conflict", "question",
        ):
            try:
                label_obj = self.repo.get_label(label_name)
            except GithubException as e:
                log.warning(
                    "could not look up %r label for closed-issue sweep "
                    "(HTTP %s); skipping. Externally-merged %s issues will "
                    "not finalize to `done` until the label exists.",
                    label_name, e.status, label_name,
                )
                continue
            closed_kwargs: dict[str, Any] = {
                "state": "closed",
                "labels": [label_obj],
                "sort": "updated",
                "direction": "desc",
            }
            if since is not None:
                closed_kwargs["since"] = since
            for issue in self.repo.get_issues(**closed_kwargs):
                if issue.pull_request is None and issue.number not in seen:
                    seen.add(issue.number)
                    yield issue

    @staticmethod
    def workflow_label(issue: Issue) -> Optional[str]:
        for lbl in issue.labels:
            if lbl.name in WORKFLOW_LABELS:
                return lbl.name
        return None

    def set_workflow_label(self, issue: Issue, new_label: Optional[str]) -> None:
        keep = [l.name for l in issue.labels if l.name not in WORKFLOW_LABELS]
        if new_label:
            keep.append(new_label)
        issue.set_labels(*keep)
        if new_label:
            self._emit_stage_enter(issue, new_label)

    _RECORDED_EVENTS_CAP = 500

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
        if len(self.recorded_events) > self._RECORDED_EVENTS_CAP:
            self.recorded_events = self.recorded_events[-self._RECORDED_EVENTS_CAP:]
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
        full_body = f"{(body or '').rstrip()}\n\nParent: #{parent_number}"
        return self.repo.create_issue(title=title, body=full_body, labels=labels)

    def read_pinned_state(self, issue: Issue) -> PinnedState:
        for c in issue.get_comments():
            body = c.body or ""
            if PINNED_STATE_MARKER not in body:
                continue
            m = PINNED_STATE_RE.search(body)
            if m:
                try:
                    return PinnedState(comment_id=c.id, data=json.loads(m.group(1)))
                except json.JSONDecodeError:
                    log.warning("issue=#%s pinned state JSON unparseable", issue.number)
                    return PinnedState(comment_id=c.id, data={})
        return PinnedState()

    def write_pinned_state(self, issue: Issue, state: PinnedState) -> PinnedState:
        body = PINNED_STATE_TEMPLATE.format(
            payload=json.dumps(state.data, sort_keys=True)
        )
        if state.comment_id is None:
            created = issue.create_comment(body)
            state.comment_id = created.id
            return state
        for c in issue.get_comments():
            if c.id == state.comment_id:
                c.edit(body)
                return state
        # Pinned comment was deleted out from under us; recreate.
        created = issue.create_comment(body)
        state.comment_id = created.id
        return state

    def comments_after(
        self, issue: Issue, after_id: Optional[int]
    ) -> list[IssueComment]:
        result: list[IssueComment] = []
        for c in issue.get_comments():
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                result.append(c)
        return result

    def latest_comment_id(self, issue: Issue) -> Optional[int]:
        latest: Optional[int] = None
        for c in issue.get_comments():
            if latest is None or c.id > latest:
                latest = c.id
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
        head = f"{self.repo.owner.login}:{branch}"
        for pr in self.repo.get_pulls(state="open", head=head, base=base):
            return pr
        return None

    def get_pr(self, pr_number: int) -> PullRequest:
        return self.repo.get_pull(pr_number)

    @staticmethod
    def pr_state(pr: PullRequest) -> str:
        """Return one of 'merged', 'closed', 'open'."""
        if pr.merged:
            return "merged"
        if pr.state == "closed":
            return "closed"
        return "open"

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
        would be reported as 'success' and AUTO_MERGE could land the PR
        over the unread failing checks.
        """
        head_sha = pr.head.sha
        states: list[str] = []
        read_failed = False

        try:
            combined = self.repo.get_commit(head_sha).get_combined_status()
            cs = combined.state
            if cs and cs != "":
                # 'success' / 'pending' / 'failure'/'error', plus 'pending'
                # when there are statuses but none have completed yet.
                if combined.total_count or cs != "pending":
                    states.append("failure" if cs == "error" else cs)
        except GithubException as e:
            log.warning(
                "could not read combined status for %s (HTTP %s); ignoring",
                head_sha, e.status,
            )
            read_failed = True

        try:
            check_runs = list(self.repo.get_commit(head_sha).get_check_runs())
            if check_runs:
                conclusions = [cr.conclusion for cr in check_runs]
                if any(c is None for c in conclusions):
                    states.append("pending")
                elif any(
                    c in ("failure", "timed_out", "action_required", "cancelled")
                    for c in conclusions
                ):
                    states.append("failure")
                elif all(
                    c in ("success", "neutral", "skipped")
                    for c in conclusions
                ):
                    states.append("success")
                else:
                    # Unknown conclusion shape -- fail safe.
                    states.append("failure")
        except GithubException as e:
            # 403 here almost always means the fine-grained PAT is missing
            # 'Checks: read'. For Actions-only PRs (no commit statuses,
            # only check-runs), swallowing this silently leaves
            # `pr_combined_check_state` at 'none' and AUTO_MERGE parks
            # forever despite the PR actually being green; surface the
            # remediation prominently so an operator can fix the scope.
            if e.status == 403:
                log.error(
                    "could not read check-runs for %s (HTTP 403). The "
                    "orchestrator PAT needs 'Checks: read' to evaluate "
                    "GitHub Actions PRs. Without it, AUTO_MERGE may "
                    "report check_state='none' and park indefinitely on "
                    "Actions-only PRs. Add the permission and restart.",
                    head_sha,
                )
            else:
                log.warning(
                    "could not read check-runs for %s (HTTP %s); ignoring",
                    head_sha, e.status,
                )
            read_failed = True

        # Partial read: at least one surface returned a usable signal but
        # the other surface raised. Treat the unread side as 'pending' so
        # an unread failing/pending check on that side cannot be masked by
        # the readable side's 'success'. AUTO_MERGE then waits (or parks
        # via the failed_checks branch on a sustained partial read) instead
        # of merging on half the picture. When BOTH surfaces failed the
        # branch is skipped and we return 'none' below, which the workflow
        # treats as ambiguous and parks awaiting_human -- visible to the
        # operator instead of silently waiting forever.
        if states and read_failed:
            states.append("pending")

        if not states:
            return "none"
        if "failure" in states:
            return "failure"
        if "pending" in states:
            return "pending"
        return "success"

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
            if (getattr(review, "commit_id", "") or "") != head_sha:
                continue
            state = (review.state or "").upper()
            if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                continue
            login = review.user.login if review.user else ""
            if not login:
                continue
            sub_id = getattr(review, "id", 0) or 0
            prev = latest_per_user.get(login)
            if prev is None or sub_id > prev[0]:
                latest_per_user[login] = (sub_id, state)
        return [s for _, s in latest_per_user.values()]

    @classmethod
    def pr_has_changes_requested(
        cls, pr: PullRequest, *, head_sha: str
    ) -> bool:
        """True if any reviewer's latest review on `head_sha` is
        CHANGES_REQUESTED. A human veto on the current head must block
        the in_review ready-for-merge ping.
        """
        return any(
            s == "CHANGES_REQUESTED"
            for s in cls._latest_review_states_for_head(pr, head_sha=head_sha)
        )

    @classmethod
    def pr_is_approved(cls, pr: PullRequest, *, head_sha: str) -> bool:
        """True iff at least one APPROVED review exists for `head_sha` and no
        review on `head_sha` says CHANGES_REQUESTED.
        """
        states = cls._latest_review_states_for_head(pr, head_sha=head_sha)
        if not states:
            return False
        if any(s == "CHANGES_REQUESTED" for s in states):
            return False
        return any(s == "APPROVED" for s in states)

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
            return True
        except GithubException as e:
            if e.status == 404:
                return True
            log.warning(
                "could not delete remote branch %r (HTTP %s): %s",
                branch, e.status, e.data,
            )
            return False

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
            return True
        except GithubException as e:
            log.warning(
                "merge failed for PR #%s (HTTP %s): %s",
                pr.number, e.status, e.data,
            )
            return False

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
        for c in pr.get_issue_comments():
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        out.sort(key=lambda c: c.id)
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
        for c in pr.get_review_comments():
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        out.sort(key=lambda c: c.id)
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
        is silently ignored and may be auto-merged over, and a
        CHANGES_REQUESTED review with body but no inline comments only
        blocks merge via `pr_has_changes_requested` without ever reaching
        the dev agent.
        """
        out: list = []
        for review in pr.get_reviews():
            state = (review.state or "").upper()
            if state not in ("CHANGES_REQUESTED", "COMMENTED"):
                continue
            body = (review.body or "").strip()
            if not body:
                continue
            if after_id is None or review.id > after_id:
                out.append(review)
        out.sort(key=lambda r: r.id)
        return out

    def ensure_workflow_labels(self) -> None:
        """Create any missing workflow/control labels on the repo. Idempotent.

        Best-effort: a 403 (under-scoped PAT) logs a clear instruction and
        returns without raising, so the polling loop keeps running. The user
        can fix the PAT scopes without restarting.
        """
        try:
            existing = {l.name for l in self.repo.get_labels()}
        except GithubException as e:
            log.warning(
                "could not list labels (HTTP %s); skipping label bootstrap. "
                "Grant the PAT 'Issues: Read and write' to enable.",
                e.status,
            )
            return
        for name, color, description in (
            WORKFLOW_LABEL_SPECS + CONTROL_LABEL_SPECS
        ):
            if name in existing:
                continue
            try:
                self.repo.create_label(name=name, color=color, description=description)
                log.info("created label %r", name)
            except GithubException as e:
                log.error(
                    "could not create label %r (HTTP %s). "
                    "Fine-grained PAT needs 'Issues: Read and write'. "
                    "Skipping remaining label bootstrap; orchestrator will keep "
                    "running and may retry on the next restart.",
                    name, e.status,
                )
                return
