# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.workflow_messages` leaves."""
from __future__ import annotations

from orchestrator import config
import re

_ORCH_COMMENT_ID_CAP = 500

_ORCH_COMMENT_MARKER = "<!--orchestrator-comment-->"

_FOREGROUND_ONLY_NOTE = (
    "IMPORTANT: your session terminates the moment you finish responding -- "
    "nothing keeps running between turns, and a later resume starts a fresh "
    "process. NEVER start a background job (build, test run, Miri, server) "
    "and end your turn intending to check it later: the job dies with your "
    "session and its result will never be seen. Run all builds and tests in "
    "the foreground and wait for them to complete before you commit or reply."
)

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

_TRACKED_REPOS_CAP = 20

_STDERR_TAIL_BUDGET = 1024

_SECRET_KEY_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PAT", "_CREDENTIAL")

_SECRET_KEY_NAMES = frozenset((
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT",
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PAT", "CREDENTIAL",
))

_REDACT_MIN_VALUE_LEN = 8

_SECTION_SEP = "\n\n"

_VERDICT_UNKNOWN = "unknown"

_NO_BODY = "(no body)"

_NO_PRIOR_COMMENTS = "(no prior comments)"

_MAX_FILES_SHOWN = 20

_VERDICT_RE = re.compile(
    r"VERDICT:\s*(APPROVED|CHANGES_REQUESTED)\b",
    re.IGNORECASE,
)

_DOC_VERDICT_RE = re.compile(
    r"(?:^|\n)[ \t]*DOCS:[ \t]*NO_CHANGE[ \t]*\r?\n?\s*\Z",
    re.IGNORECASE,
)

_DRIFT_ACK_RE = re.compile(r"^\s*ACK:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

_CONTINUE_PARK_REASONS = frozenset(("agent_silent", "agent_timeout"))

_ORCHESTRATOR_CONTINUE_RE = re.compile(
    r"^[ \t]*/orchestrator[ \t]+continue[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

_CONTINUE_RETRY_PROMPT = (
    "Resuming after a session/usage limit or a silent session failure. "
    "Re-read the issue requirements and the conversation in your transcript, "
    "then CONTINUE the work already in progress and COMMIT any remaining "
    "changes in your current worktree. Do NOT push -- the orchestrator pushes "
    "and re-runs the reviewer."
)

_CONTINUE_NEEDS_GUIDANCE_MSG = (
    f"{config.HITL_MENTIONS} `/orchestrator continue` needs your actual "
    "guidance here: this park is waiting on a real answer (an agent question, "
    "or a worktree it could not finish), not a generic continue. Reply with "
    "the specific change to make, or relabel the issue, to proceed."
)

_MANIFEST_RE = re.compile(
    r"```orchestrator-manifest\s*\n(.*?)\n```",
    re.DOTALL,
)

_MAX_CHILDREN = 10
