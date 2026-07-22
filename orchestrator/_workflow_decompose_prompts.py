# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow decompose prompts."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

Issue = _owner.Issue
config = _owner.config
_MAX_CHILDREN = _state._MAX_CHILDREN
_NO_BODY = _state._NO_BODY
_NO_PRIOR_COMMENTS = _state._NO_PRIOR_COMMENTS
_SECTION_SEP = _state._SECTION_SEP


def _build_decompose_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    comments_text: str,
    specs: list[config.RepoSpec],
) -> str:
    body = issue.body or _NO_BODY
    convo = comments_text or _NO_PRIOR_COMMENTS
    tracked = _owner._build_tracked_repos_context(spec, specs)
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
    rationale = _owner._single_manifest_text(
        manifest, "rationale", "(no rationale provided)",
    )
    lines = [f":mag: decomposer says this fits one context: {rationale}"]

    files = _owner._single_manifest_files(manifest)
    if files:
        rendered = "\n".join(f"- `{file_path}`" for file_path in files)
        lines.append(f"**Affected files:**\n{rendered}")

    notes = _owner._single_manifest_text(manifest, "notes")
    if notes:
        lines.append(f"**Implementation notes:**\n{notes}")

    return _SECTION_SEP.join(lines)
