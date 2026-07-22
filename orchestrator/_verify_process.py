# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Verify process."""
from __future__ import annotations

from orchestrator import verify as _owner

VerifyResult = _owner.VerifyResult
Optional = _owner.Optional
Path = _owner.Path
os = _owner.os
signal = _owner.signal
subprocess = _owner.subprocess
suppress = _owner.suppress


def _combine_output(stdout: str, stderr: str) -> str:
    """Merge a command's captured stdout and stderr into one block.

    stderr is appended after stdout (newline-separated when stdout did not
    end in one) so a failing build with all its diagnostics on stderr
    surfaces in a single block in the park comment.
    """
    combined = stdout or ""
    if stderr:
        if combined and not combined.endswith("\n"):
            combined = f"{combined}\n"
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
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _drain_verify_output(proc: subprocess.Popen) -> tuple[str, str]:
    """Read whatever a killed verify shell buffered, killing harder if it hangs.

    A bounded first drain covers the normal case. If it times out -- a
    descendant that escaped the group via its own `setsid` is still holding
    the pipe fd open -- `proc.kill()` reaps the leader and a second bounded
    drain runs. Returns `("", "")` if both drains time out.
    """
    drained = _owner._communicate_bounded(proc, 5)
    if drained is None:
        proc.kill()
        drained = _owner._communicate_bounded(proc, 5)
    return ("", "") if drained is None else drained


def _spawn_verify_command(
    worktree: Path, command: str, child_env: dict[str, str],
) -> subprocess.Popen:
    """Start one verify shell in the process group used for bounded cleanup."""
    return subprocess.Popen(
        command,
        shell=True,
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=child_env,
    )


def _timeout_verify_result(
    proc: subprocess.Popen, command: str,
) -> VerifyResult:
    """Kill a timed-out verify group and retain its bounded partial output."""
    _owner._kill_verify_group(proc)
    partial_output = _owner._combine_output(*_owner._drain_verify_output(proc))
    return VerifyResult(
        status="timeout",
        command=command,
        exit_code=None,
        output=_owner._truncate_verify_output(partial_output),
    )


def _completed_verify_result(
    proc: subprocess.Popen,
    command: str,
    drained: tuple[str, str],
    worktree: Path,
    head_before: str,
) -> Optional[VerifyResult]:
    """Classify one completed command, returning None only when it passed."""
    combined_output = _owner._combine_output(*drained)
    if proc.returncode != 0:
        return VerifyResult(
            status="failed",
            command=command,
            exit_code=proc.returncode,
            output=_owner._truncate_verify_output(combined_output),
        )
    dirty_files = _owner._worktree_dirty_files(worktree)
    if dirty_files:
        return VerifyResult(
            status="dirty",
            command=command,
            exit_code=proc.returncode,
            output=_owner._truncate_verify_output(combined_output),
            dirty_files=tuple(dirty_files),
        )
    head_after = _owner._head_sha(worktree)
    if head_after == head_before:
        return None
    return VerifyResult(
        status="head_changed",
        command=command,
        exit_code=proc.returncode,
        output=_owner._truncate_verify_output(combined_output),
        head_before=head_before,
        head_after=head_after,
    )
