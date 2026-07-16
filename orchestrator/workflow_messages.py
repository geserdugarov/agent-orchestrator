# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared workflow text/parsing/comment helpers.

Stage handlers live under `orchestrator/stages/` (decomposition.py,
implementing.py, documenting.py, validating.py, in_review.py, fixing.py,
conflicts.py, question.py); they reach these helpers through the
compatibility facade in `workflow.py`, which re-exports each public
name below under its original identifier for backward compatibility
with direct test references and `patch.object(workflow, ...)` patches.

Covers:

* Orchestrator comment markers and post helpers (`_post_issue_comment`,
  `_post_pr_comment`).
* Agent stderr redaction/diagnostics surfaced in park comments.
* Implementer / reviewer / decomposer / conflict / PR-comment followup
  prompt builders. The drift / user-content-change prompt builder
  (`_build_user_content_change_prompt`) lives in `workflow_drift.py`,
  not here.
* Manifest, review verdict, and drift-ACK parsers.
* The `/orchestrator continue` operator-command parser + classifier
  (`_parse_orchestrator_continue`, `_is_bare_orchestrator_continue`,
  `_continue_command_action`, `_refuse_parked_continue`), shared by the
  `fixing`, `implementing`, and `documenting` stages.
* Recent-comment formatting for prompts.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import is_trusted_author
from orchestrator.config import RepoSpec
from orchestrator.github import GitHubClient, PinnedState


# Cap on `orchestrator_comment_ids`. The watermark always advances, so older
# ids are no longer in any `comments_after` window -- the cap exists only to
# bound list growth on long-lived issues, not for correctness.
_ORCH_COMMENT_ID_CAP = 500

# Hidden HTML-comment marker embedded in the body of every issue / PR
# comment the orchestrator posts. Used by `_compute_user_content_hash` to
# identify orchestrator-authored comments WITHOUT relying on
# `orchestrator_comment_ids`, which is capped at `_ORCH_COMMENT_ID_CAP`
# and therefore evicts old ids on long-lived issues. Once an old id falls
# off the cap, an id-only filter would start including that bot comment
# in the hash and trigger false drift every tick; the body marker
# survives indefinitely on the GitHub side and is invisible in rendered
# Markdown. Kept distinct from `PINNED_STATE_MARKER` so the pinned-state
# filter (which uses `<!--orchestrator-state ... -->`) and the
# orchestrator-comment filter are independent identifiers.
_ORCH_COMMENT_MARKER = "<!--orchestrator-comment-->"

# Appended to every prompt that can lead to a commit. Agent sessions are
# one-shot headless processes: the CLI exits the moment the model ends its
# turn, so a backgrounded build/test (a long suite, `cargo miri`, a dev
# server) dies with the session and its result is never observed -- the
# issue just parks on "waiting for X to finish" forever. Models default to
# the interactive habit of backgrounding slow jobs and "checking later",
# so the execution model has to be spelled out.
_FOREGROUND_ONLY_NOTE = (
    "IMPORTANT: your session terminates the moment you finish responding -- "
    "nothing keeps running between turns, and a later resume starts a fresh "
    "process. NEVER start a background job (build, test run, Miri, server) "
    "and end your turn intending to check it later: the job dies with your "
    "session and its result will never be seen. Run all builds and tests in "
    "the foreground and wait for them to complete before you commit or reply."
)


# Commit-subject instruction shared by every commit-producing prompt. It
# tells the agent to learn the convention from the repo's OWN recent history
# rather than from a hardcoded prefix list: the orchestrator runs against
# arbitrary configured repos, so a fixed Conventional-Commits set would be
# wrong for a project that uses its own prefixes (e.g. `event:`, `career:`).
_COMMIT_STYLE_NOTE = (
    "Before committing, run `git log --oneline -20` to see how recent commit "
    "subjects are formatted, and write your subject in the SAME "
    "repository-local style. Mirror whatever subject/prefix convention that "
    "history uses rather than assuming a fixed set of types -- it may be a "
    "`<type>: <subject>` form, or a project-specific prefix such as `event:` "
    "or `career:`; the repo's own recent history is the source of truth. Keep "
    "the subject a single short, imperative line.\n\n"
    "The commit message MUST be the subject line only -- no extended "
    "description / body and no `Co-Authored-By:` (or other) trailer. Use "
    "`git commit -m \"<subject>\"` with a single `-m`."
)


# How many other tracked repos to list inline before collapsing the rest into
# a single `… and N more` line. Caps the steady-state prompt cost so a host
# driving dozens of repos cannot blow the prompt with one line per repo.
_TRACKED_REPOS_CAP = 20


def _build_tracked_repos_context(
    current: RepoSpec, specs: list[RepoSpec]
) -> str:
    """Render the 'other tracked repos' awareness block, or '' when there is
    nothing useful to say.

    Returns '' when `EXPOSE_TRACKED_REPOS` is off or there is at most one
    tracked repo -- so the default single-repo deployment sees zero added
    tokens and zero behavior change. For a multi-repo deployment it lists each
    *other* repo (the `current` one is excluded from the list) on one line with
    its slug, durable `target_root` checkout, and base branch, capped at
    `_TRACKED_REPOS_CAP` with an `… and N more` overflow line.

    The framing is deliberately stage-neutral: it says only that the sibling
    checkouts are read-only references and says nothing about whether the agent
    may write in its own working directory -- that grant (or withholding) is
    owned by the surrounding stage prompt, not by this list. No secrets are
    disclosed: only operator-configured slugs, base branches, and paths the
    agent could already read; never tokens or remote URLs.
    """
    if not config.EXPOSE_TRACKED_REPOS or len(specs) <= 1:
        return ""
    others = [repo_spec for repo_spec in specs if repo_spec.slug != current.slug]
    if not others:
        return ""
    lines = [
        f"- {repo_spec.slug} — source at {repo_spec.target_root} "
        f"(base `{repo_spec.base_branch}`)"
        for repo_spec in others[:_TRACKED_REPOS_CAP]
    ]
    overflow = len(others) - _TRACKED_REPOS_CAP
    if overflow > 0:
        lines.append(f"- … and {overflow} more")
    listing = "\n".join(lines)
    return (
        "This orchestrator also tracks the repositories below. Their source is "
        "checked out locally for cross-repo reference only -- treat every path "
        "listed here as read-only and do NOT modify, commit, or push in any of "
        "them. (Whether you may write in your own working directory is governed "
        "by the rest of this prompt, not by this list.) Your task is on "
        f"`{current.slug}`.\n\n{listing}"
    )


def _orchestrator_ids(state: PinnedState) -> set[int]:
    """Set of comment ids the orchestrator itself posted on this issue/PR.
    Used to filter the orchestrator's own messages out of "new feedback"
    scans without falling back to author-login matching -- a PAT shared
    with a human reviewer's GitHub account would otherwise have its real
    review comments swallowed as bot noise (and the PR pinged ready for
    human merge over them).
    """
    raw = state.get("orchestrator_comment_ids") or []
    return {int(comment_id) for comment_id in raw}


def _track_orchestrator_comment(state: PinnedState, comment_id: int) -> None:
    raw = state.get("orchestrator_comment_ids")
    ids = list(raw) if isinstance(raw, list) else []
    ids.append(int(comment_id))
    if len(ids) > _ORCH_COMMENT_ID_CAP:
        ids = ids[-_ORCH_COMMENT_ID_CAP:]
    state.set("orchestrator_comment_ids", ids)


def _with_orch_marker(body: str) -> str:
    """Append the hidden orchestrator-comment marker to `body` (idempotent).

    Every orchestrator-posted comment carries this marker so the
    user-content hash can identify bot comments even after their id has
    been evicted from the bounded `orchestrator_comment_ids` cap. The
    marker is an HTML comment, invisible in rendered Markdown.
    """
    if _ORCH_COMMENT_MARKER in body:
        return body
    return f"{body}\n\n{_ORCH_COMMENT_MARKER}"


def _post_issue_comment(
    gh: GitHubClient, issue: Issue, state: PinnedState, body: str,
):
    """Post an issue comment AND record its id in pinned state so future
    `_handle_in_review` ticks recognize it as orchestrator-authored even when
    the PAT login is shared with a human reviewer. Caller is still responsible
    for `gh.write_pinned_state` -- this only mutates the in-memory state.

    The body is augmented with `_ORCH_COMMENT_MARKER` so the user-content
    hash can identify bot comments by marker (id-cap-resistant) in
    addition to by id (works for tracked-and-not-yet-evicted comments).
    """
    issue_comment = gh.comment(issue, _with_orch_marker(body))
    cid = getattr(issue_comment, "id", None)
    if cid is not None:
        _track_orchestrator_comment(state, int(cid))
    return issue_comment


def _post_pr_comment(
    gh: GitHubClient, pr_number: int, state: PinnedState, body: str,
):
    """PR-conversation comment counterpart to `_post_issue_comment`. Both
    surfaces share the IssueComment id namespace, so a single id list covers
    them. Inline review comments and PR review summaries live in different id
    spaces but the orchestrator never posts to those, so they need no entry.

    The body is augmented with `_ORCH_COMMENT_MARKER` for the same reason
    as `_post_issue_comment`: the user-content hash needs to identify
    bot comments even after their id has been evicted from the bounded
    `orchestrator_comment_ids` cap. PR-conversation comments do not feed
    into `_compute_user_content_hash` directly (the hash reads
    `issue.get_comments()`, not the PR's), but marker symmetry across
    surfaces keeps the filter rules uniform and avoids accidental
    inconsistency when a future tweak does start reading PR comments.
    """
    pr_comment = gh.pr_comment(pr_number, _with_orch_marker(body))
    cid = getattr(pr_comment, "id", None)
    if cid is not None:
        _track_orchestrator_comment(state, int(cid))
    return pr_comment


# Cap the stderr tail surfaced in park comments. A multi-MB Cloudflare
# anti-bot interstitial (the original motivation for surfacing stderr at
# all -- see #36) would otherwise bloat the issue body past GitHub's limit.
_STDERR_TAIL_BUDGET = 1024

# Defense-in-depth redaction of secret-shaped env values before any stderr
# is surfaced to GitHub or the orchestrator log. `agents._filter_agent_env`
# already strips both GitHub-token aliases AND the broader secret-shape
# family (`*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_PAT`,
# `*_CREDENTIAL`, plus credential-file locators) from the agent's and the
# verify command's environment, so a well-behaved subprocess cannot
# read those values to begin with. The redactor below catches the
# remaining narrow leaks:
#   * provider auth the agent IS allowed to see (the
#     `_AGENT_PROVIDER_AUTH_ALLOWLIST` -- ANTHROPIC_API_KEY, OPENAI_API_KEY,
#     …) -- a noisy backend or buggy test that echoes its own provider
#     key to stderr would otherwise republish it verbatim in the park
#     comment we post to the issue;
#   * vars the orchestrator process itself holds but the subprocess was
#     supposed to never see -- if a git/gh subprocess that DID get the
#     PAT (the orchestrator's own pushes) leaks it on stderr we still
#     redact before posting;
#   * the file-backed GITHUB_TOKEN -- when resolved from
#     ORCHESTRATOR_TOKEN_FILE (or the default ~/.config/<repo>/token) it
#     never appears in os.environ, so the env loop alone misses it and
#     we redact the cached value explicitly below.
# Match by suffix to keep the long tail of provider/secret names
# (`HF_TOKEN`, `GEMINI_API_KEY`, `DATABASE_PASSWORD`, …) covered without
# enumerating every variant, plus a small bare-name set (some build
# systems set unprefixed `TOKEN` / `PASSWORD`).
_SECRET_KEY_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PAT", "_CREDENTIAL")
_SECRET_KEY_NAMES = frozenset({
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT",
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PAT", "CREDENTIAL",
})
# Short values produce too many false-positive replacements (a 4-char dev
# key masks incidental substrings like "true"/"main") for too little
# protection. Real provider keys are well above this floor.
_REDACT_MIN_VALUE_LEN = 8


def _is_secret_environment_value(key: str, env_value: str) -> bool:
    """Whether an environment entry is shaped like a usable secret."""
    if not env_value or len(env_value) < _REDACT_MIN_VALUE_LEN:
        return False
    upper_key = key.upper()
    return upper_key in _SECRET_KEY_NAMES or any(
        upper_key.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES
    )


def _redact_environment_secrets(text: str) -> str:
    """Replace every secret-shaped process environment value."""
    redacted = text
    for key, env_value in os.environ.items():
        if _is_secret_environment_value(key, env_value):
            redacted = redacted.replace(env_value, "***")
    return redacted


def _redact_configured_github_token(text: str) -> str:
    """Redact the PAT even when it came from a token file, not the env."""
    token = config.GITHUB_TOKEN
    if token and len(token) >= _REDACT_MIN_VALUE_LEN:
        return text.replace(token, "***")
    return text


def _redact_secrets(text: str) -> str:
    """Replace values of secret-shaped env vars in `text` with `***`.

    Called before any stderr is surfaced to GitHub or the log so a
    prompt-injected agent that echoes its own provider key cannot exfiltrate
    it via a park comment. Snapshot of os.environ at call time, so a key
    that was unset between subprocess spawn and the post is no longer
    redacted -- acceptable since it also no longer leaks anything reachable
    from the agent.
    """
    if not text:
        return text
    # GITHUB_TOKEN may have been resolved from ORCHESTRATOR_TOKEN_FILE (or
    # the default ~/.config/<repo>/token path) rather than the process env,
    # in which case the environment scan never sees it. The explicit token
    # pass also covers git/gh stderr that quotes a file-backed credential.
    return _redact_configured_github_token(
        _redact_environment_secrets(text)
    )


def _format_stderr_diagnostics(
    agent_result: AgentResult, label: str = "Agent",
) -> str:
    """Render a stderr/exit-code diagnostic block to append to a park comment.

    Returns "" when the agent produced no stderr -- callers can concatenate
    unconditionally without a trailing dead section. Otherwise returns a
    block beginning with two newlines so it slots cleanly after an existing
    `_Last … message:_` body.

    Redaction happens on the raw stderr before any trimming: a multi-line
    secret env value (e.g. an SSH/PEM key whose env-var value ends in `\\n`)
    echoed at the end of stderr would otherwise have its trailing newline
    stripped first, so `str.replace` would no longer find the env value
    verbatim and the secret would leak.
    """
    tail = _redact_secrets(agent_result.stderr or "").rstrip()
    if not tail:
        return ""
    if len(tail) > _STDERR_TAIL_BUDGET:
        tail = tail[-_STDERR_TAIL_BUDGET:]
    quoted = "> " + tail.replace("\n", "\n> ")
    return (
        f"\n\n_{label} stderr (last 1KB):_\n\n{quoted}\n\n"
        f"_{label} exit code:_ {agent_result.exit_code}"
    )


def _stderr_log_tail(agent_result: AgentResult, max_chars: int = 400) -> str:
    """Short stderr tail for log lines -- tighter than the park-comment cap
    so a single WARNING fits on one screen.

    Redact before trimming for the same reason as `_format_stderr_diagnostics`:
    a multi-line secret value ending in `\\n` would not match `str.replace`
    if `rstrip` ate the trailing newline first.
    """
    tail = _redact_secrets(agent_result.stderr or "").rstrip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


# The reviewer prompt asks for the marker alone on its own line, but real
# codex output isn't always that disciplined: prefixes like "Final verdict:"
# or trailing punctuation appear in practice. Match anywhere and take the
# last occurrence, so a stray reference earlier in the text loses to the
# concluding one.
_VERDICT_RE = re.compile(
    r"VERDICT:\s*(APPROVED|CHANGES_REQUESTED)\b",
    re.IGNORECASE,
)


def _parse_review_verdict(last_message: str) -> Tuple[str, str]:
    """Find the last 'VERDICT: APPROVED|CHANGES_REQUESTED' marker.

    Returns (verdict, body_above_marker). verdict is one of "approved",
    "changes_requested", or "unknown" (no marker found). body_above_marker is
    the slice of last_message before the marker, used as PR-comment text for
    the changes-requested case.
    """
    if not last_message:
        return "unknown", ""
    matches = list(_VERDICT_RE.finditer(last_message))
    if not matches:
        return "unknown", last_message
    last = matches[-1]
    word = last.group(1).upper()
    verdict = "approved" if word == "APPROVED" else "changes_requested"
    body = last_message[: last.start()].rstrip()
    return verdict, body


# Marker the documentation session emits to explicitly assert that the
# branch diff needs no documentation update. Stricter than `_VERDICT_RE`:
# the marker MUST occupy its own line AND be the final non-whitespace
# content of the message. Otherwise prose like
#   "I cannot conclude DOCS: NO_CHANGE because README is stale."
# or a marker line followed by an unresolved question
#   "DOCS: NO_CHANGE\nBut I have a question about the API."
# would be parsed as success, defeating the issue requirement that
# ambiguous no-commit text must not be accepted. The leading
# `(?:^|\n)` anchors the marker at the start of a line; the trailing
# `\s*\Z` requires only whitespace through end of string (so trailing
# punctuation like `DOCS: NO_CHANGE.` or any follow-up content fails to
# match). `re.MULTILINE` is deliberately NOT set -- with it `$` would
# match at every line break and a non-final marker would still slip
# through.
_DOC_VERDICT_RE = re.compile(
    r"(?:^|\n)[ \t]*DOCS:[ \t]*NO_CHANGE[ \t]*\r?\n?\s*\Z",
    re.IGNORECASE,
)


def _parse_documentation_verdict(last_message: str) -> Tuple[str, str]:
    """Find a final 'DOCS: NO_CHANGE' marker in a documentation-stage message.

    Returns (verdict, body_above_marker):
      * `("no_change", body)` -- the agent emitted the explicit marker
        AS THE FINAL LINE (alone on its line, with only optional
        whitespace through end of string), confirming the branch diff
        requires no documentation update. `body` is the slice above
        the marker, suitable for surfacing the agent's one-line
        justification on the issue.
      * `("unknown", last_message)` -- no valid final marker present.
        The caller MUST park rather than treat this as success;
        deliberately rejected variants include:
          - ambiguous prose like "no changes needed";
          - inline references such as
              "I cannot conclude DOCS: NO_CHANGE because ...";
          - non-final markers followed by further content, e.g.
              "DOCS: NO_CHANGE\nBut I have a question.";
          - markers with trailing punctuation like "DOCS: NO_CHANGE.".

    The `"updated"` outcome (docs were modified) is signalled by a fresh
    commit on the branch (any subject; the prompt no longer mandates a
    `docs:` prefix) and is detected at the stage handler level rather than
    here -- this parser only resolves the no-commit branch.
    """
    if not last_message:
        return "unknown", ""
    match = _DOC_VERDICT_RE.search(last_message)
    if match is None:
        return "unknown", last_message
    body = last_message[: match.start()].rstrip()
    return "no_change", body


# Marker the dev session emits to explicitly acknowledge that the existing
# work satisfies a user-content-drift edit. Matched on its own line,
# anywhere in the message; the last occurrence wins (mirrors how
# `_VERDICT_RE` accepts the reviewer's final marker even when earlier
# references appear in the body). Without this marker the no-commit
# response is treated as a clarification question via `_on_question`, NOT
# silently swallowed as an ack -- a misleading "existing work satisfies"
# comment on a real question would leave the issue stuck without
# `awaiting_human` set.
_DRIFT_ACK_RE = re.compile(r"^\s*ACK:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _drift_ack_reason(last_message: str) -> Optional[str]:
    """Return the dev's ACK justification if `last_message` carries the
    explicit `ACK: ...` marker, or None when no marker is present.

    Takes the LAST match (matches `_parse_review_verdict`'s convention) so
    a stray reference earlier in the message loses to the concluding line.
    """
    if not last_message:
        return None
    matches = list(_DRIFT_ACK_RE.finditer(last_message))
    if not matches:
        return None
    return matches[-1].group(1).strip() or None


# Park reasons a `/orchestrator continue` operator command may retry: a dev
# session that went silent (`agent_silent` -- a poisoned resume that produced
# no output, or a session/usage-limit stop parked under this reason) or timed
# out (`agent_timeout`). Both are session-limit / session-failure conditions
# whose recovery is "retry the dev flow on a fresh or replayed session", not
# "wait for a human answer". Every other awaiting-human shape -- a real agent
# question or a dirty worktree (both `park_reason=None`), a stuck push, a
# missing PR -- needs the human's actual guidance, so the command is refused
# there. Shared by the `fixing`, `implementing`, and `documenting` stages.
_CONTINUE_PARK_REASONS = frozenset({"agent_silent", "agent_timeout"})

# `/orchestrator continue` operator command, matched as an EXACT LINE
# (anchored to line boundaries, mirrors `_ADD_REVIEW_ROUNDS_RE` in
# `stages.validating`) so prose that mentions the command in backticks cannot
# fire it. `search` detects the command line inside a larger comment -- so a
# comment that carries the command AND real guidance still counts as the
# command -- while `_is_bare_orchestrator_continue` (whole stripped body == the
# line) tells a content-free nudge apart from a comment that also carries
# guidance.
_ORCHESTRATOR_CONTINUE_RE = re.compile(
    r"^[ \t]*/orchestrator[ \t]+continue[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_orchestrator_continue(comments: list) -> list:
    """Return the comments whose body contains an exact-line
    `/orchestrator continue` operator command."""
    return [
        comment
        for comment in comments
        if _ORCHESTRATOR_CONTINUE_RE.search(comment.body or "")
    ]


def _is_bare_orchestrator_continue(comment) -> bool:
    """True when the comment's ENTIRE body is the command line and nothing
    else -- a content-free nudge whose consumption drops no guidance."""
    return (
        _ORCHESTRATOR_CONTINUE_RE.fullmatch((comment.body or "").strip())
        is not None
    )


def _continue_command_action(new_comments: list, park_reason) -> str:
    """Classify an operator `/orchestrator continue` on a parked dev-session
    stage (`implementing` / `documenting` / `validating` / `resolving_conflict`)
    whose park carries no preserved feedback batch to replay -- the counterpart
    to `fixing`'s richer `_handle_continue_command`, which can reconstruct an
    in_review batch.

    `new_comments` is the fresh trusted issue-thread comments since the last
    consumed watermark. Returns:

      * ``"retry"``       -- a retryable session-failure park
        (`agent_silent` / `agent_timeout`) whose fresh comments are ALL bare
        continues: retry the parked dev flow intentionally, without feeding
        the bare command to the dev as guidance.
      * ``"refuse"``      -- a park that needs real guidance (a genuine agent
        question, a dirty worktree, a diverged branch, ...) whose fresh
        comments are ALL bare continues: the command carries no answer, so
        refuse and stay parked.
      * ``"passthrough"`` -- no continue command is present, or the command
        arrived alongside genuine guidance: the caller's normal
        resume / drift path handles the comments (and feeds that guidance to
        the dev).
    """
    if not _parse_orchestrator_continue(new_comments):
        return "passthrough"
    if not all(
        _is_bare_orchestrator_continue(comment) for comment in new_comments
    ):
        return "passthrough"
    if park_reason in _CONTINUE_PARK_REASONS:
        return "retry"
    return "refuse"


# Neutral resume prompt for an intentional `/orchestrator continue` retry of a
# session-failure park, shared by the dev-parking stages (`implementing` /
# `documenting`'s docs rerun aside, `validating`, `resolving_conflict`).
# Deliberately NOT the bare command text: the parked dev session already carries
# its task in the replayed transcript, or is re-grounded from the issue body +
# recent comments when `_resume_dev_with_text` rotates it to a fresh spawn, so
# the followup only has to say "carry on".
_CONTINUE_RETRY_PROMPT = (
    "Resuming after a session/usage limit or a silent session failure. "
    "Re-read the issue requirements and the conversation in your transcript, "
    "then CONTINUE the work already in progress and COMMIT any remaining "
    "changes in your current worktree. Do NOT push -- the orchestrator pushes "
    "and re-runs the reviewer."
)


# Refusal posted when a content-free `/orchestrator continue` lands on a
# dev-parking stage park that needs a real human answer (a genuine agent
# question or a worktree the dev could not finish). Mirrors the non-retryable
# refusal `fixing._handle_continue_command` posts.
_CONTINUE_NEEDS_GUIDANCE_MSG = (
    f"{config.HITL_MENTIONS} `/orchestrator continue` needs your actual "
    "guidance here: this park is waiting on a real answer (an agent question, "
    "or a worktree it could not finish), not a generic continue. Reply with "
    "the specific change to make, or relabel the issue, to proceed."
)


def _refuse_parked_continue(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Consume a content-free `/orchestrator continue` and post a refusal on a
    park that needs real human guidance, leaving the issue parked.

    Shared by the dev-parking stages (`implementing`, `documenting`,
    `validating`, `resolving_conflict`): a bare continue on a non-retryable
    park carries no answer, so post a single note and advance the
    issue watermark past BOTH the command and the refusal (so neither re-fires
    next tick and the refusal is not re-posted every poll). `awaiting_human`
    stays set. Mutates in-memory state only; the caller writes pinned state.
    """
    _post_issue_comment(gh, issue, state, _CONTINUE_NEEDS_GUIDANCE_MSG)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)


# Captures the JSON payload between a fenced ```orchestrator-manifest block.
# We deliberately match everything up to the next ``` rather than trying to
# bound braces in the regex itself: nested objects in the JSON body would
# trip a `\{.*?\}` non-greedy match without rescuing well, while a fence
# delimiter is a single token that the agent prompt forces it to emit.
_MANIFEST_RE = re.compile(
    r"```orchestrator-manifest\s*\n(.*?)\n```",
    re.DOTALL,
)
# Hard cap on children per parent. A buggy decomposer that emits 100 children
# would otherwise create 100 GitHub issues before anyone notices. Configurable
# later if needed; not surfaced as an env var initially.
_MAX_CHILDREN = 10


def _extract_manifest_payload(
    last_message: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Extract the one final fenced manifest payload from an agent reply."""
    if not last_message:
        return None, None
    # The prompt requires exactly one final fenced block. Accepting the first
    # match would let a quoted sample manifest override the agent's answer.
    matches = list(_MANIFEST_RE.finditer(last_message))
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            f"expected exactly one orchestrator-manifest block, "
            f"found {len(matches)}"
        )
    manifest_match = matches[0]
    if last_message[manifest_match.end():].strip():
        return None, (
            "orchestrator-manifest must be the final block; "
            "found content after the closing fence"
        )
    return manifest_match.group(1), None


def _decode_manifest(
    payload: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Decode a manifest payload and require a JSON object."""
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error.msg}"
    if not isinstance(manifest, dict):
        return None, "manifest is not a JSON object"
    return manifest, None


def _split_manifest_children(
    manifest: dict,
) -> Tuple[Optional[list], Optional[str]]:
    """Return the bounded, non-empty children list for a split decision."""
    children = manifest.get("children")
    if not isinstance(children, list) or not children:
        return None, "split decision requires non-empty children list"
    if len(children) > _MAX_CHILDREN:
        return None, f"too many children ({len(children)} > {_MAX_CHILDREN})"
    return children, None


def _manifest_umbrella_error(manifest: dict) -> Optional[str]:
    """Validate the optional umbrella flag without truthy coercion."""
    umbrella = manifest.get("umbrella")
    if umbrella is not None and not isinstance(umbrella, bool):
        return "umbrella must be a boolean"
    return None


def _is_nonempty_text(text_value: object) -> bool:
    return isinstance(text_value, str) and bool(text_value)


def _manifest_child_text_error(
    child: object, child_index: int,
) -> Optional[str]:
    """Validate one child object and its required text fields."""
    if not isinstance(child, dict):
        return f"child {child_index} is not an object"
    if not _is_nonempty_text(child.get("title")):
        return f"child {child_index} missing title or body"
    if not _is_nonempty_text(child.get("body")):
        return f"child {child_index} missing title or body"
    return None


def _manifest_child_dependencies(
    child: dict, child_index: int,
) -> Tuple[Optional[list], Optional[str]]:
    """Normalize null dependencies and reject every other non-list shape."""
    dependencies = child.get("depends_on")
    if dependencies is None:
        return [], None
    if not isinstance(dependencies, list):
        return None, f"child {child_index} depends_on must be a list"
    return dependencies, None


def _is_valid_dependency(
    dependency_index: object,
    *,
    child_index: int,
    child_count: int,
) -> bool:
    """Validate type, bounds, and the no-self-edge invariant."""
    if isinstance(dependency_index, bool):
        return False
    if not isinstance(dependency_index, int):
        return False
    if dependency_index < 0 or dependency_index >= child_count:
        return False
    return dependency_index != child_index


def _manifest_child_error(
    child: object, child_index: int, child_count: int,
) -> Optional[str]:
    """Return the first structural error for one split child."""
    text_error = _manifest_child_text_error(child, child_index)
    if text_error is not None:
        return text_error
    dependencies, dependency_error = _manifest_child_dependencies(
        child, child_index,
    )
    if dependency_error is not None:
        return dependency_error
    for dependency_index in dependencies or []:
        if not _is_valid_dependency(
            dependency_index,
            child_index=child_index,
            child_count=child_count,
        ):
            return (
                f"child {child_index} has invalid dependency "
                f"{dependency_index!r}"
            )
    return None


def _manifest_children_error(children: list) -> Optional[str]:
    """Validate every child and then the dependency graph as a whole."""
    for child_index, child in enumerate(children):
        child_error = _manifest_child_error(
            child, child_index, len(children),
        )
        if child_error is not None:
            return child_error
    if _has_dep_cycle(children):
        return "dependency graph has a cycle"
    return None


def _split_manifest_error(manifest: dict) -> Optional[str]:
    """Return the first split-only manifest validation error."""
    children, children_error = _split_manifest_children(manifest)
    if children_error is not None:
        return children_error
    umbrella_error = _manifest_umbrella_error(manifest)
    if umbrella_error is not None:
        return umbrella_error
    return _manifest_children_error(children or [])


def _manifest_validation_error(manifest: dict) -> Optional[str]:
    """Validate the decision and its split-only payload when applicable."""
    decision = manifest.get("decision")
    if decision not in ("single", "split"):
        return "decision must be 'single' or 'split'"
    if decision == "single":
        return None
    return _split_manifest_error(manifest)


def _parse_manifest(
    last_message: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Parse a fenced `orchestrator-manifest` block.

    Returns `(manifest, error_reason)`:
      * `(dict, None)` -- a valid manifest. `decision` is `"single"` or
        `"split"`; for `"split"`, `children` is non-empty and each entry has
        `title`/`body` and a structurally-valid `depends_on` index list. On
        `"single"` only `decision` is validated -- the optional context
        fields (`rationale`, `affected_files`, `notes`) pass through
        unvalidated and are sanitized where rendered.
      * `(None, error)` -- a fence was present but the payload was invalid.
        `error` is a short human-readable reason (used in the HITL park
        message).
      * `(None, None)` -- no fenced block at all. The caller treats this as
        "agent ended without a manifest" and parks as a question.
    """
    payload, payload_error = _extract_manifest_payload(last_message)
    if payload is None:
        return None, payload_error
    manifest, decode_error = _decode_manifest(payload)
    if manifest is None:
        return None, decode_error
    validation_error = _manifest_validation_error(manifest)
    if validation_error is not None:
        return None, validation_error
    return manifest, None


def _dep_cycle_visit(
    child_index: int, children: list[dict], color: list[int],
) -> bool:
    """DFS one node of the children dep graph; True on a back-edge to a node
    still on the stack.

    `color` is mutated in place (0=unvisited, 1=on-stack, 2=finished) and
    shared across the whole walk, so a node finished on one root is never
    re-descended from another.
    """
    color[child_index] = 1
    for dependency_index in (children[child_index].get("depends_on") or []):
        if color[dependency_index] == 1:
            return True
        if color[dependency_index] == 0 and _dep_cycle_visit(
            dependency_index, children, color,
        ):
            return True
    color[child_index] = 2
    return False


def _has_dep_cycle(children: list[dict]) -> bool:
    """DFS for back-edges in the children dep graph (white/gray/black)."""
    color = [0] * len(children)  # 0=unvisited, 1=on-stack, 2=finished
    return any(
        color[child_index] == 0
        and _dep_cycle_visit(child_index, children, color)
        for child_index in range(len(children))
    )


def _prompt_comment_chunk(issue_comment: object) -> Optional[str]:
    """Format one trusted, non-state issue comment for an agent prompt."""
    body = getattr(issue_comment, "body", None) or ""
    if "<!--orchestrator-state" in body:
        return None
    user = getattr(issue_comment, "user", None)
    if not is_trusted_author(user):
        return None
    login = user.login if user else "user"
    return f"@{login}: {body}"


def _recent_comments_text(issue: Issue, max_chars: int = 4000) -> str:
    """Conversation text fed to every agent prompt (implement, review,
    documentation, decompose, question, and the drift-resume prompt).

    An untrusted author's comment is dropped whole -- its body and any URLs
    it contains never reach the prompt -- so once `ALLOWED_ISSUE_AUTHORS`
    is set an outsider on a public repo cannot smuggle workflow-driving
    instructions into a coding agent through the issue thread. With no
    allowlist configured `is_trusted_author` trusts every author, so the
    default single-user deployment sees the full thread unchanged.
    """
    chunks: list[str] = []
    for issue_comment in issue.get_comments():
        chunk = _prompt_comment_chunk(issue_comment)
        if chunk is not None:
            chunks.append(chunk)
    text = "\n\n".join(chunks)
    return text[-max_chars:] if len(text) > max_chars else text


def _build_implement_prompt(
    spec: RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[RepoSpec],
) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    tracked = _build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are the implementer for GitHub issue #{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Implement the change in the current working directory (a fresh git worktree on a "
        "new branch). When done, COMMIT your changes with a clear message. Do NOT push - "
        "the orchestrator pushes and opens the PR.\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        f"{_FOREGROUND_ONLY_NOTE}\n\n"
        "If you cannot proceed because of missing information, leave the working tree "
        "uncommitted (no commits) and end your response with a clear question for the human."
    )


def _build_fresh_respawn_preamble(
    spec: RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[RepoSpec],
) -> str:
    """Re-grounding header prepended to a FRESH dev spawn that REPLACES a
    retired or poisoned session mid-issue (proactive rotation, silent-park
    fallback, or stale/overflow recovery).

    The previous session's in-memory reasoning is gone, but its committed work
    survives on the current branch, so the fresh agent is pointed at the branch
    as the source of truth and re-grounded in the issue requirements +
    conversation. Without this the rotation regresses into a context-starved
    spawn that could re-implement from scratch or ignore the original spec.
    The caller appends the stage-specific instruction (fix feedback, drift,
    conflict, ...) after this block.
    """
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    tracked = _build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are resuming work on GitHub issue #{issue.number}: {issue.title!r}. "
        "A previous agent session worked on this issue and its commits are "
        "already on the current branch (your working directory); that session's "
        "history is NOT available to you. Before doing anything, re-ground "
        "yourself: inspect what has already been done with `git log --oneline` "
        "and `git diff` against the base branch, and continue from there -- do "
        "NOT restart the implementation from scratch.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Your immediate task follows.\n"
        "----------------------------------------"
    )


def _build_review_prompt(
    spec: RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[RepoSpec],
    dev_backend: str = "agent",
) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    tracked = _build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are an automated code reviewer for GitHub issue #{issue.number}: {issue.title!r}. "
        f"A separate {dev_backend} session has implemented this issue and committed to the current "
        f"branch. The base branch is `{base_ref}`.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Inspect the change with:\n"
        f"  git log --oneline {base_ref}..HEAD\n"
        f"  git diff {base_ref}...HEAD\n\n"
        "Review the change against the issue requirements. Flag correctness bugs, missing "
        "tests, scope creep, obvious style issues, and anything that would block a human "
        "approver. Do NOT edit or commit anything -- you are a reviewer only.\n\n"
        "Your final message MUST end with exactly one of these markers, alone on its own line:\n"
        "  VERDICT: APPROVED\n"
        "  VERDICT: CHANGES_REQUESTED\n\n"
        "If CHANGES_REQUESTED, list the specific items above the verdict line as a numbered "
        "list so the implementer can address them one by one. If the change is acceptable as "
        "is, write VERDICT: APPROVED with a one-line justification above it."
    )


def _build_documentation_prompt(
    spec: RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[RepoSpec],
) -> str:
    """Prompt for the documentation pass that runs as the final-docs
    handoff between reviewer approval and `in_review`.

    Reuses the dev agent role -- the documentation pass commits to the same
    branch as the implementer, so it is operating as a developer and not a
    reviewer. No separate backend env var is introduced for this stage;
    the stage handler invokes the existing dev backend on the PR worktree.
    """
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    tracked = _build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are the documentation pass for GitHub issue #{issue.number}: "
        f"{issue.title!r}. A separate session has implemented this issue and "
        f"committed to the current branch. The base branch is `{base_ref}`.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Inspect the change with:\n"
        f"  git log --oneline {base_ref}..HEAD\n"
        f"  git diff {base_ref}...HEAD\n\n"
        "Compare the branch diff against `README.md` and the `docs/` tree. "
        "If any user-facing description or architectural note needs to be "
        "updated to match the code that landed in this branch, UPDATE the "
        "relevant files and COMMIT the change in the current worktree. Do "
        "NOT push -- the orchestrator pushes once this stage finishes. Do "
        "NOT inspect or modify the `plans/` tree or roadmap entries: those "
        "are working notes owned by humans and are out of scope for the "
        "final-docs pass.\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        "If the branch genuinely requires no documentation change, do NOT "
        "commit and end your final message with EXACTLY this marker, alone "
        "on its own line:\n\n"
        "  DOCS: NO_CHANGE\n\n"
        "Place a one-sentence justification on the line above the marker. "
        "The orchestrator will NOT accept ambiguous phrasing like "
        "'no changes needed' as success without the explicit marker; an "
        "agent message that neither commits nor emits the marker is parked "
        "for human review.\n\n"
        "If you genuinely cannot decide because of missing information, "
        "leave the worktree uncommitted, omit the marker, and end your "
        "final message with a question for the human; the orchestrator "
        "will park the issue for human review.\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )


def _build_fix_prompt(review_feedback: str) -> str:
    feedback = review_feedback.strip() or "(reviewer left no detail)"
    quoted = "> " + feedback.replace("\n", "\n> ")
    return (
        "An automated reviewer requested changes on your implementation. Address each item "
        "below, then COMMIT the fix in your current worktree. Do NOT push -- the orchestrator "
        "pushes and re-runs the review.\n\n"
        f"Review feedback:\n\n{quoted}\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        f"{_FOREGROUND_ONLY_NOTE}\n\n"
        "If you genuinely disagree with a point, end your final message with a question for "
        "the human and leave that item un-fixed; the orchestrator will park the issue for "
        "human review. Otherwise, fix all items (a single commit is fine)."
    )


def _build_decompose_prompt(
    spec: RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[RepoSpec],
) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    tracked = _build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are the decomposer for GitHub issue #{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Decide whether this issue can be implemented in ONE coding-agent "
        "context window. If yes, return decision='single'. If no, propose a "
        "list of smaller child issues each one-shottable on its own.\n\n"
        "Sizing rule of thumb: if the change touches more than ~5 files or "
        "needs more than one logical commit, propose splitting; otherwise "
        "keep it as a single child. Use `git ls-files`, `wc -l`, or other "
        "read-only commands to inspect the codebase. You MUST NOT commit, "
        "push, or modify any file -- you are read-only.\n\n"
        "If you genuinely need a clarification, end your message with a "
        "question for the human and DO NOT emit a manifest. Otherwise, end "
        "your final message with EXACTLY ONE fenced JSON block in this "
        "format (and nothing else after it):\n\n"
        "```orchestrator-manifest\n"
        "{\n"
        "  \"decision\": \"split\",\n"
        "  \"rationale\": \"<<= 2 sentences why>\",\n"
        "  \"umbrella\": false,\n"
        "  \"children\": [\n"
        "    {\"title\": \"...\", \"body\": \"...\", \"depends_on\": []}\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "The block must be valid JSON parseable by `json.loads`. The "
        "`decision` value must be exactly the string `\"single\"` or "
        "`\"split\"` (no other values, no union syntax). On `\"single\"`, "
        "omit the `children` field and instead hand off the context you "
        "already gathered so the implementer does not re-derive it: add "
        "`\"affected_files\"` (a list of repo-relative paths you found "
        "relevant) and `\"notes\"` (<= 3 sentences of concrete "
        "implementation guidance). Both are optional but strongly "
        "encouraged on `\"single\"`.\n\n"
        "Rules for the children list (omit entirely on 'single'):\n"
        f"- At most {_MAX_CHILDREN} children.\n"
        "- `depends_on` is a list of 0-based indexes into THIS children "
        "array (not GitHub issue numbers; the orchestrator allocates those).\n"
        "- Self-dependencies and cycles are rejected.\n"
        "- Each child must be small enough to implement in one context "
        "(do not propose a child that itself needs decomposition).\n\n"
        "The optional `umbrella` boolean (default false) signals that the "
        "parent issue itself has NO implementation work of its own and exists "
        "only to aggregate the children. Set it to true when every line of "
        "the parent's intent is covered by the children you are creating; "
        "leave it false when the parent still needs its own coding pass after "
        "the children land. An umbrella parent auto-resolves to `done` once "
        "every child resolves; a non-umbrella parent re-enters implementation."
    )


def _single_manifest_text(
    manifest: dict, field_name: str, fallback: str = "",
) -> str:
    """Return one stripped optional text field with a safe fallback."""
    raw_value = manifest.get(field_name)
    text = raw_value.strip() if isinstance(raw_value, str) else ""
    return text or fallback


def _single_manifest_files(manifest: dict) -> list[str]:
    """Return non-empty string paths from optional single-decision context."""
    raw_files = manifest.get("affected_files")
    if not isinstance(raw_files, list):
        return []
    return [
        file_path.strip()
        for file_path in raw_files
        if isinstance(file_path, str) and file_path.strip()
    ]


def _build_single_decision_comment(manifest: dict) -> str:
    """Compose the `single`-decision comment posted on the parent issue.

    Beyond the decomposer's rationale, this surfaces the context the
    decomposer already gathered while sizing the issue -- the affected
    files and any implementation notes -- so the develop agent that picks
    the issue up in `implementing` starts from that groundwork instead of
    re-deriving it. The implementer reads the issue thread via
    `_recent_comments_text` at spawn, so anything included here reaches it.
    A comment (not a body edit) is deliberate: rewriting the issue body
    would shift the user-content hash and trip `_detect_user_content_change`
    into re-decomposing the issue on the next tick.

    Every field beyond `decision` is best-effort. `_parse_manifest` only
    validates the decision string for the single branch, so `rationale` /
    `affected_files` / `notes` may be any JSON value or missing; coerce
    non-strings / non-lists to empty rather than parking a valid single
    decision after the agent already ran.
    """
    rationale = _single_manifest_text(
        manifest, "rationale", "(no rationale provided)",
    )
    lines = [f":mag: decomposer says this fits one context: {rationale}"]

    files = _single_manifest_files(manifest)
    if files:
        rendered = "\n".join(f"- `{file_path}`" for file_path in files)
        lines.append(f"**Affected files:**\n{rendered}")

    notes = _single_manifest_text(manifest, "notes")
    if notes:
        lines.append(f"**Implementation notes:**\n{notes}")

    return "\n\n".join(lines)


def _build_conflict_resolution_prompt(
    base_ref: str, files: list[str]
) -> str:
    shown = files[:20]
    files_md = "\n".join(f"- `{file_path}`" for file_path in shown)
    if len(files) > len(shown):
        files_md += f"\n- ... ({len(files) - len(shown)} more)"
    return (
        f"`git rebase {base_ref}` left {len(files)} conflicted "
        "file(s) in your worktree. Resolve each conflict and complete the "
        "rebase in your current worktree. Do NOT push -- the orchestrator "
        "pushes and re-runs the reviewer.\n\n"
        f"Conflicted paths:\n\n{files_md}\n\n"
        "Workflow: edit each file to a coherent resolution, `git add` it, "
        "then run `git rebase --continue`. Repeat until the rebase completes. "
        "If Git reports an empty commit because the change is already present, "
        "use `git rebase --skip`; use `git commit --allow-empty` only when "
        "an empty commit is intentional. Use `git rebase --abort` only as "
        "the escape hatch when you cannot make progress. "
        "Use `git status` to inspect the in-progress rebase.\n\n"
        "If you genuinely cannot resolve a conflict, end your final "
        "message with a question for the human and leave the worktree "
        "mid-rebase; the orchestrator will park the issue for human review.\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )


def _build_question_prompt(
    spec: RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[RepoSpec],
) -> str:
    """Compose the read-only prompt used by the `question` stage.

    The agent runs in the per-issue `issue-N` worktree with read-only
    expectations: it must answer the standing question (or ask a focused
    follow-up of its own) without touching code, committing, or pushing.
    The orchestrator parks on any commit / dirty-tree output, so the
    prompt is explicit about that contract.

    The tracked-repos awareness block is included for a multi-repo
    deployment; it lists the sibling checkouts as read-only references
    and does not soften this stage's own no-write contract (the block's
    framing defers write permission to the surrounding prompt, which
    grants none here).
    """
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    tracked = _build_tracked_repos_context(spec, specs)
    tracked_block = f"{tracked}\n\n" if tracked else ""
    return (
        f"You are answering a standing question on GitHub issue "
        f"#{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{tracked_block}"
        "Read the issue and the conversation above, inspect the codebase "
        "with read-only commands (`git ls-files`, `git log`, `cat`, "
        "`grep`, etc.), and write a focused answer to the open question. "
        "Cite file paths or commits when useful. You MUST NOT modify, "
        "create, delete, commit, or push any file -- this stage is "
        "purely informational.\n\n"
        "If you need more information from the human before you can "
        "answer, end your message with a single, focused follow-up "
        "question. Otherwise end with a clear answer that the human can "
        "act on (close the issue, relabel it to `implementing`, etc.)."
    )


def _build_question_followup_prompt(comments: list) -> str:
    """Compose the resume prompt the question stage sends back to its
    locked agent session after a human reply.

    Mirrors `_resume_developer_on_human_reply`'s shape -- a quote of the
    incoming comments -- but reiterates the read-only / no-commit
    contract so a multi-tick conversation cannot drift into the agent
    deciding to "just implement the fix".
    """
    body = "\n\n".join(
        f"@{comment.user.login if comment.user else 'user'}: "
        f"{comment.body or ''}"
        for comment in comments
    )
    quoted = "> " + body.replace("\n", "\n> ")
    return (
        "The human replied on the issue thread. Continue the discussion "
        "and answer their reply.\n\n"
        f"Human reply:\n\n{quoted}\n\n"
        "Reminder: this is still the read-only question stage. Do NOT "
        "modify, create, delete, commit, or push any file. End with a "
        "clear answer or a single, focused follow-up question."
    )


def _build_pr_comment_followup(comments: list) -> str:
    """Compose a dev-fix prompt from new PR-side comments.

    The dev session has not seen any PR comment before (those live on a
    different surface than the issue thread it was fed at spawn time), so a
    short preamble is needed to frame the request -- otherwise a comment like
    "rename foo to bar" reads as freeform chatter without context.
    """
    body = "\n\n".join(
        f"@{comment.user.login if comment.user else 'user'}: "
        f"{comment.body or ''}"
        for comment in comments
    )
    quoted = "> " + body.replace("\n", "\n> ")
    return (
        "New comments arrived on the open PR for this issue. Address each item, "
        "then COMMIT the fix in your current worktree. Do NOT push -- the "
        "orchestrator pushes and re-runs the reviewer.\n\n"
        f"PR comments:\n\n{quoted}\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        "If you genuinely disagree with a point, end your final message with a "
        "question for the human and leave that item un-fixed; the orchestrator "
        "will park the issue for human review.\n\n"
        "If the comments contain NO concrete, actionable change request -- e.g. "
        "a vague 'continue', 'ok', or 'ping' that names no specific defect -- "
        "and the branch already satisfies them, make NO commit and end your "
        "final message with a single line `ACK: <brief reason>`. The "
        "orchestrator will then return the PR to review-ready instead of "
        "parking it for a fix that is not warranted.\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )
