# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Verify runner."""
from __future__ import annotations

from orchestrator import _verify_state as _state
from orchestrator import verify as _owner

VerifyResult = _owner.VerifyResult
Optional = _owner.Optional
Path = _owner.Path
os = _owner.os
_VERIFY_OUTPUT_BUDGET = _state._VERIFY_OUTPUT_BUDGET


def _run_verify_command(
    worktree: Path,
    command: str,
    timeout: int,
    child_env: dict[str, str],
    head_before: str,
) -> Optional[VerifyResult]:
    """Run and classify one command while registering its process group."""
    proc = _owner._spawn_verify_command(worktree, command, child_env)
    with _owner._registered(proc):
        drained = _owner._communicate_bounded(proc, timeout)
        if drained is None:
            return _owner._timeout_verify_result(proc, command)
        return _owner._completed_verify_result(
            proc, command, drained, worktree, head_before,
        )


def _run_verify_commands(
    worktree: Path,
    commands: tuple[str, ...],
    timeout: int,
) -> VerifyResult:
    """Run each command sequentially in `worktree` with a bounded timeout.

    Empty `commands` (the default) short-circuits to ``status="ok"`` so the
    legacy "no verification" behaviour is a single boolean check at the
    call site. Commands are spawned via the shell so quoting / pipes /
    `&&` work the way an operator would type them; stdout and stderr are
    merged so a failing build with all its diagnostics on stderr surfaces
    in one block in the park comment. The shell runs with a child
    environment stripped of GitHub credentials, production-secret-shaped
    variables, AND the agent's own provider-auth keys (`_filter_agent_env`
    with `allow_provider_auth=False`) -- stricter than the agent-subprocess
    strip, because a verify command is operator-configured shell that
    executes the agent-produced code and a hostile dependency reading
    `$ANTHROPIC_API_KEY` would gain billable access to the operator's
    model account.

    The first non-zero exit, timeout, post-run dirty tree, or HEAD
    advance wins -- later commands are not run, since the gate is
    "everything passed" and the operator only needs the first failure
    to triage. Dirtiness and HEAD-movement are checked AFTER EACH
    command so a failure can be attributed to the actual command that
    caused it, with that command's captured stdout/stderr preserved in
    `output` for the park comment. The HEAD check guards against a
    verify command that `git commit`s its own fixups: without it, a
    clean tree + zero exit would look like `ok`, and the squash-on-
    approval + force-push that follows would publish an unreviewed
    verify-created commit.
    """
    if not commands:
        return VerifyResult(status="ok")
    # Snapshot HEAD so we can refuse any verify command that moves it.
    # An empty snapshot (an uninitialized repo or a `git rev-parse`
    # failure) means we cannot prove HEAD stability, so a later
    # commit-by-the-verify-command would look identical to the
    # missing baseline -- treat the unknown baseline as "" and accept
    # only an unchanged "" afterwards (which means no HEAD ever
    # existed). Anything else is a fail-closed park.
    head_before = _owner._head_sha(worktree)
    # Strip GitHub credentials, production-secret-shaped variables,
    # write-credential locators (SSH-agent / askpass), AND the agent's
    # own provider-auth keys from the child environment. Verify commands
    # run operator-configured shell against code the agent just produced;
    # without this, a prompt-injected `pytest` plugin (or a hostile
    # dependency the agent pulled in) could read `$GITHUB_TOKEN` /
    # `$STRIPE_API_KEY` / `$ANTHROPIC_API_KEY` / `$SSH_AUTH_SOCK` / ...
    # straight out of the orchestrator's environment and exfiltrate or
    # push as the operator. `allow_provider_auth=False` is stricter than
    # the agent subprocess case: the agent CLI needs its provider key to
    # reach its model, but the verify shell does not. An operator who
    # legitimately needs a secret in a verify command must load it from
    # disk inside a wrapper script (`VERIFY_COMMANDS=./run-verify.sh`);
    # inline `KEY=value pytest ...` is unsafe because the failure park
    # comment publishes `verify.command` verbatim on the issue.
    child_env = _owner._filter_agent_env(dict(os.environ), allow_provider_auth=False)
    for command in commands:
        failure = _owner._run_verify_command(
            worktree, command, timeout, child_env, head_before,
        )
        if failure is not None:
            return failure
    return VerifyResult(status="ok")


def _truncate_verify_output(text: str) -> str:
    """Redact secrets, then keep the tail within `_VERIFY_OUTPUT_BUDGET`.

    Redaction MUST happen before the truncation. `_redact_secrets` does a
    full-string `str.replace(value, "***")` against each candidate env
    value; if the truncation cut sliced a secret in half first, the
    surviving partial would no longer match the replace and would leak
    verbatim in the park comment. Redacting first collapses any matched
    secret to `***` before its bytes can straddle the cut.

    The tail typically carries the actual failure (stack trace, assertion
    diff, linter summary); the head is build noise. Identical convention
    to `_format_stderr_diagnostics`.
    """
    if not text:
        return ""
    redacted = _owner._redact_secrets(text)
    if len(redacted) <= _VERIFY_OUTPUT_BUDGET:
        return redacted
    return redacted[-_VERIFY_OUTPUT_BUDGET:]
