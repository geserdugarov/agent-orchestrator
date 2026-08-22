# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Authenticated repository client composed from the owner mixins.

``GitHubClient`` is the one concrete client workflow and operator code
construct: it resolves the token, opens the PyGithub connection, and owns the
seams that cut across the domain owners -- the per-worker clone, the label read
cache, and the paired audit / analytics stage-enter hook.
"""
from __future__ import annotations

import logging
from typing import Optional

from github import Auth, Github, GithubException
from github.Issue import Issue
from github.Label import Label
from github.Repository import Repository

from orchestrator import config
from orchestrator.github.checks import GitHubChecksMixin
from orchestrator.github.labels import GitHubLabelMixin
from orchestrator.github.reviews import GitHubReviewMixin
from orchestrator.observability.analytics import recording

log = logging.getLogger("orchestrator.github")

# The one status that answers "this repository does not have that label".
# Anything else -- 403 on an exhausted rate limit, a 5xx -- says only that the
# question could not be asked, so the sweep has to ask it again.
_HTTP_NOT_FOUND = 404

# How many closed-issue sweeps a confirmed-absent label is taken at its word.
# Counted in sweeps rather than seconds because the cost being throttled is one
# request per sweep -- and counted off `_closed_sweeps`, which advances only
# when a sweep actually runs, so `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` stretches
# the window in wall-clock terms instead of eroding it. Long enough that a
# migrated repository is not re-asking for a label nobody has on every pass,
# short enough that a human re-adding one by hand is picked up without a
# restart -- the absence is throttled, never final.
_ABSENT_LABEL_RETRY_SWEEPS = 20


class GitHubClient(
    GitHubReviewMixin,
    GitHubLabelMixin,
    GitHubChecksMixin,
):
    """Authenticated repository client with a worker-safe clone seam.

    The review and label collaborators stand beside the check / pull-request
    chain rather than inside it: neither needs a check or PR method, and keeping
    them independent lets each own its surface without a leaf-to-leaf import.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_spec: Optional["config.RepoSpec"] = None,
        *,
        bot_login: Optional[str] = None,
    ) -> None:
        slug = repo_slug or config.REPO if repo_spec is None else repo_spec.slug
        if token is None:
            token = config._resolve_github_token(slug)
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is empty. Export it in the orchestrator's "
                "environment or write it to "
                f"~/.config/{slug}/token "
                "(override path with ORCHESTRATOR_TOKEN_FILE). "
                "Do NOT put it in REPO_ROOT/.env -- the implementer agent "
                "can read that file.",
            )
        self._gh = Github(auth=Auth.Token(token))
        self.repo: Repository = self._gh.get_repo(slug)
        self._repo_slug = slug
        self._token = token
        self._bot_login = (
            self._gh.get_user().login
            if bot_login is None
            else bot_login
        )
        self.recorded_events: list[dict] = []
        self._label_cache: dict[str, Label] = {}
        self._absent_after_sweep: dict[str, int] = {}
        self._pollable_calls = 0
        self._closed_sweeps = 0

    def _for_worker_thread(self) -> "GitHubClient":
        """Build a fresh requester/repository pair for one worker thread."""
        return GitHubClient(
            token=self._token,
            repo_slug=self._repo_slug,
            bot_login=self._bot_login,
        )

    def _cached_label(
        self,
        name: str,
        *,
        throttle_absent: bool = False,
        absent_names: Optional[list[str]] = None,
        strict: bool = False,
    ) -> Optional[Label]:
        """Resolve and cache a label, while leaving failures retryable.

        Every failure is retried eventually; the only question is how soon.
        Retrying on the very next call is the default, because a label the
        bootstrap could not create is one a human may add at any moment.
        ``throttle_absent`` widens that to `_ABSENT_LABEL_RETRY_SWEEPS`
        closed-issue sweeps for one case: a 404 on a pre-namespace spelling
        the sweep asks for beside the namespaced one. Most repositories will
        never have that label again, so asking every sweep is a request spent
        on a certain miss -- but a human or an older integration can still
        re-apply one, so the window expires rather than closing.

        The throttle is a 404 only. A 403 is what this client sees when the
        primary rate limit is exhausted, and standing down on it would strand
        exactly the closed legacy-labeled issues the second query exists to
        reach.

        ``absent_names`` is where a throttled miss is recorded for the caller
        to summarize; a caller that passes none takes the miss silently.

        ``strict`` is for the caller that cannot treat "could not ask" as an
        answer. A 404 is a real one -- the repository does not have that label
        -- and still returns None; every other status is a question nobody
        put, and swallowing it hands back the same None. The sweep reads that
        as one label skipped this pass; a caller deciding whether an issue it
        already created exists would read it as "no" and create a second one.
        So a non-404 is re-raised for that caller and swallowed for the others.
        """
        cached_label = self._label_cache.get(name)
        if cached_label is not None:
            return cached_label
        if self._absent_after_sweep.get(name, 0) > self._closed_sweeps:
            return None
        try:
            label_object = self.repo.get_label(name)
        except GithubException as error:
            if strict and error.status != _HTTP_NOT_FOUND:
                raise
            self._report_label_lookup_failure(
                name, error, throttle_absent, absent_names,
            )
            return None
        self._absent_after_sweep.pop(name, None)
        self._label_cache[name] = label_object
        return label_object

    def _report_label_lookup_failure(
        self,
        name: str,
        error: GithubException,
        throttle_absent: bool,
        absent_names: Optional[list[str]],
    ) -> None:
        """Report a label the sweep could not resolve, and when to re-ask."""
        if throttle_absent and error.status == _HTTP_NOT_FOUND:
            self._absent_after_sweep[name] = (
                self._closed_sweeps + _ABSENT_LABEL_RETRY_SWEEPS
            )
            # Handed to the caller's summary rather than reported here: this
            # answer is the expected one, and a line per spelling says nothing
            # a line per repository does not.
            if absent_names is not None:
                absent_names.append(name)
            return
        log.warning(
            "could not look up %r label for closed-issue sweep "
            "(HTTP %s); skipping. Externally-merged %s issues will "
            "not finalize to `done` until the label exists.",
            name,
            error.status,
            name,
        )

    def _report_absent_legacy_labels(self, absent_names: list[str]) -> None:
        """Summarize the legacy spellings one sweep confirmed absent.

        Reported per repository, and only for the spellings this sweep asked
        about: on a migrated host the alternative is a burst of near-identical
        lines naming no repository -- once per legacy spelling per repo on
        every fresh process, and again whenever the retry window expires --
        that reads like broken configuration while saying only that the rename
        landed.

        The names come from the sweep that collected them, so a sweep that
        raises partway through takes its own with it. Held on the client
        instead, they would outlive the pass that asked, and the next sweep
        would report a name it never re-asked -- a skip served from the retry
        window, restated as a fresh miss with the window starting over.
        """
        if not absent_names:
            return
        log.info(
            "repo=%s closed-issue sweep: %d legacy (pre-namespace) label "
            "spelling(s) absent; not asked again for %d sweeps: %s. Nothing "
            "is stranded unless issues still carry them.",
            self._repo_slug,
            len(absent_names),
            _ABSENT_LABEL_RETRY_SWEEPS,
            ", ".join(absent_names),
        )

    def _emit_stage_enter(self, issue: Issue, stage: str) -> None:
        """Record matching audit and analytics stage-enter events."""
        issue_number = getattr(issue, "number", 0) or 0
        self.emit_event(
            "stage_enter",
            issue_number=issue_number,
            stage=stage,
        )
        recording.record_stage_enter(
            repo=self._repo_slug,
            issue=issue_number,
            stage=stage,
        )
