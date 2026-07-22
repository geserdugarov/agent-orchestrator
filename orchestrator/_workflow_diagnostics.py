# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow diagnostics."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

AgentResult = _owner.AgentResult
_STDERR_TAIL_BUDGET = _state._STDERR_TAIL_BUDGET


def _as_blockquote(text: str) -> str:
    """Render `text` as a Markdown blockquote (each line prefixed with `> `)."""
    prefixed = text.replace("\n", "\n> ")
    return f"> {prefixed}"


def _format_stderr_diagnostics(
    agent_result: AgentResult, label: str = "Agent",
) -> str:
    r"""Render a stderr/exit-code diagnostic block to append to a park comment.

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
    tail = _owner._redact_secrets(agent_result.stderr or "").rstrip()
    if not tail:
        return ""
    if len(tail) > _STDERR_TAIL_BUDGET:
        tail = tail[-_STDERR_TAIL_BUDGET:]
    quoted = _owner._as_blockquote(tail)
    return (
        f"\n\n_{label} stderr (last 1KB):_\n\n{quoted}\n\n"
        f"_{label} exit code:_ {agent_result.exit_code}"
    )


def _stderr_log_tail(agent_result: AgentResult, max_chars: int = 400) -> str:
    r"""Short stderr tail for log lines -- tighter than the park-comment cap
    so a single WARNING fits on one screen.

    Redact before trimming for the same reason as `_format_stderr_diagnostics`:
    a multi-line secret value ending in `\\n` would not match `str.replace`
    if `rstrip` ate the trailing newline first.
    """
    tail = _owner._redact_secrets(agent_result.stderr or "").rstrip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail
