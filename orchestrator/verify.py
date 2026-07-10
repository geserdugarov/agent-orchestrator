# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Local-verify runner and worktree state helpers.

Owns the bounded-timeout, process-group-killable runner that executes
the operator-configured `VERIFY_COMMANDS` against a per-issue worktree,
the `VerifyResult` shape its callers read, and the two small worktree-
state probes the runner needs to detect a dirty tree or a HEAD-moving
verify command:

* `_head_sha` -- HEAD commit SHA of a worktree, or `''` on failure.
* `_worktree_dirty_files` -- paths git considers modified or untracked.
* `_truncate_verify_output` -- redact-then-tail to `_VERIFY_OUTPUT_BUDGET`
  so a chatty test runner cannot overflow the park comment and cannot
  leak a partial secret through a mid-cut value.
* `VerifyResult` -- frozen dataclass with the per-status fields populated
  only for the case they describe.
* `_run_verify_commands` -- sequential bounded runner with per-command
  dirty-tree and HEAD-change checks; returns the first failure wins.

The hardening semantics are preserved verbatim from the previous
`worktrees.py` home: redact-before-truncate, `start_new_session=True`
plus `killpg` on timeout, `_filter_agent_env(..., allow_provider_auth=
False)` to strip GitHub tokens / production secret shapes / agent
provider keys / write-credential locators from the verify shell's
child environment, and the per-command dirty / HEAD-movement probes
that block an unreviewed verify-created commit from sneaking past
`_squash_and_force_push`. Each running command is also registered in
`agents._running_procs` so the orchestrator's shutdown sweep
(`agents.terminate_all_running`) tears down a slow verify group on
SIGTERM/SIGINT instead of leaving it to mutate the worktree after a
watchdog hard-exit.

`worktrees.py` re-exports every name below under its original name so
existing imports (`from orchestrator.worktrees import VerifyResult`)
and `patch.object(worktrees, "_foo", ...)` test patches keep working
without touching the new module. The leading underscore convention is
preserved because these helpers remain module-internal contracts -- the
public surface is the stage handlers in `orchestrator/stages/` driven
by `workflow.py`.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orchestrator.agents import _communicate_bounded, _filter_agent_env, _registered
from orchestrator.git_plumbing import _git, _git_hardened
from orchestrator.workflow_messages import _redact_secrets

log = logging.getLogger(__name__)


def _head_sha(worktree: Path) -> str:
    """HEAD commit SHA of the worktree, or '' if it cannot be read.

    Used by the validating handler to detect whether a dev-fix codex run
    produced a new commit. _has_new_commits compares against origin/<base>,
    which is already true throughout validating, so we need an absolute SHA
    snapshot instead.
    """
    head_result = _git("rev-parse", "HEAD", cwd=worktree)
    if head_result.returncode != 0:
        return ""
    return (head_result.stdout or "").strip()


def _worktree_dirty_files(worktree: Path) -> list[str]:
    """Paths git considers modified or untracked in the worktree.

    Used to refuse opening a PR when codex committed only part of its work and
    left other modifications behind -- the push would publish an incomplete
    branch. The orchestrator's own scratch (codex's `-o` file) lives outside
    the worktree (a per-spawn tempfile in `_run_codex`), so it never surfaces
    here regardless of the target repo's .gitignore.

    Hardened unconditionally: `git status --porcelain` refreshes the index,
    which spawns a configured `core.fsmonitor` helper -- and the agent can
    plant one in the worktree's `.git/config` or in `~/.gitconfig` (same OS
    user), so a plain probe would execute it with the orchestrator's process
    environment (ambient secrets) attached. Every call site is an
    agent-writable worktree, so there is no trusted caller that would want
    the unhardened form. Detaching global/system config also drops a global
    `core.excludesFile` from the untracked filter; the repo's own tracked
    `.gitignore` still applies, which is the intended trust boundary.
    """
    status_result = _git_hardened("status", "--porcelain", cwd=worktree)
    if status_result.returncode != 0:
        return []
    paths: list[str] = []
    for line in (status_result.stdout or "").splitlines():
        if len(line) < 4:
            continue
        # porcelain v1: "XY <path>" with optional " -> dest" for renames.
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        if path:
            paths.append(path)
    return paths


# Trim long verify command output to a budget compatible with GitHub's
# issue body limit -- a chatty test runner can otherwise overflow the
# park comment. Matches the stderr-tail budget used by
# `_format_stderr_diagnostics` so both surfaces enforce the same cap.
_VERIFY_OUTPUT_BUDGET = 4096


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of running the configured `VERIFY_COMMANDS`.

    `status` is one of:

    * ``"ok"``           -- every command exited 0 and the worktree was clean.
    * ``"failed"``       -- a command exited non-zero.
    * ``"timeout"``      -- a command hit the per-command wall-clock cap.
    * ``"dirty"``        -- every command exited 0 but the worktree carried
                            uncommitted changes afterwards; treated as a
                            verify failure because handing off a dirty tree
                            to in_review would advertise the PR as ready for
                            human merge with state the dev never committed.
    * ``"head_changed"`` -- a command moved `HEAD` (it ran `git commit` or
                            `git reset` etc.) while leaving the tree clean.
                            Treated as a verify failure because the squash-
                            on-approval + force-push that follows would
                            otherwise publish an unreviewed verify-created
                            commit. `head_before` / `head_after` record the
                            SHAs so the operator can identify which commit
                            the verify produced.

    The non-ok fields (`command`, `exit_code`, `output`, `dirty_files`,
    `head_before` / `head_after`) are populated only for the case they
    describe and are otherwise None / empty so the formatter does not
    have to know the variant.

    `output` is already redacted (via `_redact_secrets`) AND truncated to
    `_VERIFY_OUTPUT_BUDGET` bytes -- callers can post it verbatim. The
    redact pass runs before truncation so a secret straddling the cut
    cannot leak a partial value (see `_truncate_verify_output`).
    """

    status: str
    command: Optional[str] = None
    exit_code: Optional[int] = None
    output: str = ""
    dirty_files: tuple[str, ...] = ()
    head_before: Optional[str] = None
    head_after: Optional[str] = None


def _combine_output(stdout: str, stderr: str) -> str:
    """Merge a command's captured stdout and stderr into one block.

    stderr is appended after stdout (newline-separated when stdout did not
    end in one) so a failing build with all its diagnostics on stderr
    surfaces in a single block in the park comment.
    """
    combined = stdout or ""
    if stderr:
        if combined and not combined.endswith("\n"):
            combined += "\n"
        combined += stderr
    return combined


def _kill_verify_group(proc: subprocess.Popen) -> None:
    """SIGKILL a timed-out verify command's whole process group.

    `start_new_session=True` made `proc.pid` a group leader, so one `killpg`
    tears down the shell AND every descendant (`make -j` workers, pytest-xdist
    forkers, backgrounded `&` subshells) together -- a plain `proc.kill()`
    reaps only the shell and lets a survivor keep mutating the worktree after
    the orchestrator has already posted `verify_timeout` and parked the issue.
    `os.getpgid(proc.pid)` reads that group id; `ProcessLookupError` /
    `PermissionError` cover the race where the shell exited between the
    timeout firing and this call (nothing left to kill).
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _drain_verify_output(proc: subprocess.Popen) -> tuple[str, str]:
    """Read whatever a killed verify shell buffered, killing harder if it hangs.

    A bounded first drain covers the normal case. If it times out -- a
    descendant that escaped the group via its own `setsid` is still holding
    the pipe fd open -- `proc.kill()` reaps the leader and a second bounded
    drain runs. Returns `("", "")` if both drains time out.
    """
    drained = _communicate_bounded(proc, 5)
    if drained is None:
        proc.kill()
        drained = _communicate_bounded(proc, 5)
    return drained if drained is not None else ("", "")


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
    head_before = _head_sha(worktree)
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
    child_env = _filter_agent_env(dict(os.environ), allow_provider_auth=False)
    for command in commands:
        # `start_new_session=True` puts the shell in its own process
        # group (and session) so a timeout-kill can tear down EVERY
        # descendant in one `killpg` call. Without this, the
        # `subprocess.run(..., shell=True, timeout=...)` shape only
        # SIGKILLs the shell; a `make -j` worker, a `pytest-xdist`
        # forker, or a backgrounded `&` subprocess survives the shell
        # and can keep mutating the worktree AFTER the orchestrator has
        # already posted `verify_timeout` and parked the issue. That
        # silently violates the bounded-timeout gate the operator
        # configured and can race the orchestrator's own next-tick
        # reads.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=child_env,
        )
        # Register the group so the orchestrator's shutdown sweep
        # (`agents.terminate_all_running`) can SIGTERM/SIGKILL it. Without
        # this a slow verify command is invisible to the sweep and survives
        # the shutdown watchdog's `os._exit`, going on to mutate the worktree
        # after the orchestrator has stopped -- the same bounded-lifetime
        # guarantee the agent subprocesses already get. `start_new_session=
        # True` above makes `proc.pid` the group leader the sweep targets;
        # `_registered` clears the registry so a completed command does not
        # leak into it.
        with _registered(proc):
            drained = _communicate_bounded(proc, timeout)
            if drained is None:
                _kill_verify_group(proc)
                partial = _combine_output(*_drain_verify_output(proc))
                return VerifyResult(
                    status="timeout",
                    command=command,
                    exit_code=None,
                    output=_truncate_verify_output(partial),
                )
            combined = _combine_output(*drained)
            if proc.returncode != 0:
                return VerifyResult(
                    status="failed",
                    command=command,
                    exit_code=proc.returncode,
                    output=_truncate_verify_output(combined),
                )
            # Check dirtiness PER COMMAND so a dirty failure can be
            # attributed to the actual command that produced the
            # untracked/modified files and surface that command's stdout/
            # stderr in the park comment. A single end-of-loop check would
            # always blame `commands[-1]` even when an earlier command was
            # the cause, and would have already lost its captured output
            # by the time we got here.
            dirty = _worktree_dirty_files(worktree)
            if dirty:
                return VerifyResult(
                    status="dirty",
                    command=command,
                    exit_code=proc.returncode,
                    output=_truncate_verify_output(combined),
                    dirty_files=tuple(dirty),
                )
            # HEAD-movement check. A verify command that `git commit`s its
            # own auto-fix leaves `git status` clean and exits 0 -- looking
            # identical to a passing gate -- yet the squash-on-approval +
            # force-push that follows would publish that unreviewed commit.
            # Fail the gate so the operator decides whether the auto-commit
            # belongs in the PR (re-spawn the reviewer) or should be
            # reverted before re-trying.
            head_after = _head_sha(worktree)
            if head_after != head_before:
                return VerifyResult(
                    status="head_changed",
                    command=command,
                    exit_code=proc.returncode,
                    output=_truncate_verify_output(combined),
                    head_before=head_before,
                    head_after=head_after,
                )
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
    redacted = _redact_secrets(text)
    if len(redacted) <= _VERIFY_OUTPUT_BUDGET:
        return redacted
    return redacted[-_VERIFY_OUTPUT_BUDGET:]
