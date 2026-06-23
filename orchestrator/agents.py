# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Spawn a local coding-agent CLI (codex or claude) as a subprocess.

Both backends emit JSONL events on stdout. We don't pin their event-shape
contracts; instead `parse_session_id` walks the parsed JSON looking for any
UUID-shaped value at common keys (session_id, conversation_id, ...). If a
format drifts, the unit tests on parse_session_id and the claude
last-message walker will fail loudly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import config

log = logging.getLogger(__name__)

# Registry of live orchestrator-spawned subprocess groups, keyed by the
# `Popen` object. Two producers register here: `_run_subprocess` (the agent
# CLI children) and `verify._run_verify_commands` (the operator-configured
# `VERIFY_COMMANDS` shells). Each call registers its child for the lifetime of
# the run and clears it in a `finally`. Both producers spawn with
# `start_new_session=True`, so every registered `pid` is a process-group
# leader and `terminate_all_running` can `killpg` the whole group. The
# orchestrator's shutdown path signals every registered group so a
# long-running agent (bounded only by `AGENT_TIMEOUT`, 1800s) or a slow verify
# command cannot keep its worker thread -- and therefore `main`'s drain --
# alive past systemd's stop deadline, nor survive a watchdog hard-exit and go
# on mutating the worktree after the orchestrator has stopped. The lock guards
# the set against the worker threads mutating it concurrently with a shutdown
# sweep.
_running_procs: set[subprocess.Popen] = set()
_running_procs_lock = threading.Lock()


def _register_proc(proc: subprocess.Popen) -> None:
    with _running_procs_lock:
        _running_procs.add(proc)


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _running_procs_lock:
        _running_procs.discard(proc)


def _process_group_alive(pgid: int) -> bool:
    """True if process group `pgid` still has a live member.

    Probes with signal 0: no signal is delivered, the kernel just runs the
    existence/permission check. `ProcessLookupError` means the group is empty
    (leader and every descendant have exited). Used after a leader's `wait()`
    returns to tell "the whole group is gone" apart from "the leader exited
    but a descendant ignored SIGTERM and is still running".
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def terminate_all_running(grace: float = 5.0) -> int:
    """SIGTERM every in-flight subprocess group, then SIGKILL stragglers.

    Sweeps both producers in `_running_procs`: the agent CLI children and the
    `VERIFY_COMMANDS` shells (`verify._run_verify_commands`). Returns the
    number of process groups signaled. Called from the orchestrator's shutdown
    path so a restart does not hang waiting for an agent that would otherwise
    run for up to `AGENT_TIMEOUT`, and so a long verify command cannot keep
    mutating the worktree after a watchdog hard-exit. SIGTERM is sent to the
    whole group (every producer spawns with `start_new_session=True`) so build
    grandchildren a child forked are reaped too. A single shared `grace`
    deadline bounds the total wait regardless of how many groups are in
    flight; any group still alive at the deadline is SIGKILLed.

    `proc.wait()` only observes the group *leader*: a descendant that ignored
    SIGTERM keeps running -- and keeps mutating the worktree -- after the
    leader exits, so leader-exit is not proof the group is gone. We SIGKILL
    unless the leader exited AND a `killpg(_, 0)` probe shows the group has no
    surviving member; a leader that hit the deadline is still alive, so its
    group is SIGKILLed without a probe.

    `ProcessLookupError` races are expected (a group may exit between the
    snapshot and the signal) and are swallowed.
    """
    with _running_procs_lock:
        procs = list(_running_procs)
    if not procs:
        return 0
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    for proc in procs:
        remaining = max(0.0, deadline - time.monotonic())
        leader_exited = True
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            leader_exited = False
        if leader_exited and not _process_group_alive(proc.pid):
            continue
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(procs)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PRIORITY_KEYS = ("session_id", "conversation_id", "thread_id", "session", "id")

# Strip GitHub credentials from the agent's environment. Issue/comment text is
# untrusted and the agent runs with sandbox bypass, so a prompt injection that
# inherits these would let the agent push directly or call the API as us.
# The orchestrator owns all GitHub writes; the agent must never see them.
#
# Exact-name list, kept narrow to GitHub-specific aliases. Production
# secret-shaped variables (anything matching `_AGENT_SECRET_SUFFIXES` /
# `_AGENT_SECRET_BARE_NAMES`) are stripped separately by `_filter_agent_env`
# below; the provider auth keys codex/claude actually need to talk to their
# model are preserved by `_AGENT_PROVIDER_AUTH_ALLOWLIST`.
_FORBIDDEN_AGENT_ENV = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
    "GH_HOST",
})

# Write-credential locators that aren't secret-shaped but let a subprocess
# use the operator's loaded auth to push or authenticate as them. None of
# these are values per se; they point at a live socket, an askpass binary,
# or a custom SSH command. Inheriting any of them lets the agent or a
# verify command:
#   * `SSH_AUTH_SOCK`   -- forward through the operator's ssh-agent and
#                          push to any host whose key is loaded.
#   * `SSH_ASKPASS`     -- pop up the operator's GUI/cli pass prompt.
#   * `GIT_ASKPASS`     -- the orchestrator's push path sets its OWN tempfile
#                          askpass; the operator's value would otherwise let
#                          a subprocess execute that binary with our env.
#   * `GIT_SSH_COMMAND` -- arbitrary SSH wrapper a subprocess could invoke
#                          (or worse, the operator's already-configured one
#                          that lets `git fetch ssh://...` succeed silently).
# The orchestrator's own push path (`worktrees._push_branch`) constructs
# `GIT_ASKPASS` in the env it hands to subprocess.run, so stripping the
# operator's copy here does not break it.
_AGENT_WRITE_CREDENTIAL_LOCATORS = frozenset({
    "SSH_AUTH_SOCK",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "GIT_SSH_COMMAND",
})

# Production-secret-shaped variables that should NOT be inherited by agent /
# verify subprocesses, even though they are not GitHub-specific. Two
# overlapping concerns covered here:
#
# 1. Direct secret values in env (`STRIPE_API_KEY`, `DATABASE_PASSWORD`,
#    `DEPLOY_TOKEN`, …) -- a sandbox-bypassed agent or operator-configured
#    verify shell would otherwise read them straight out of os.environ.
#
# 2. Credential-file LOCATORS (`ORCHESTRATOR_TOKEN_FILE`,
#    `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_SHARED_CREDENTIALS_FILE`,
#    `*_TOKEN_FILE`, `*_CREDENTIALS`, …) -- the value is a filesystem path,
#    not a secret, but an agent running as the same OS user can simply
#    open the file. Stripping the locator does not protect against the
#    agent guessing the default path (`~/.config/<repo>/token`,
#    `~/.aws/credentials`), but it removes the trivial "follow the
#    env-var pointer" exfiltration path and forces the agent to use a
#    well-known guess that the operator can audit independently. The
#    `ORCHESTRATOR_TOKEN_FILE` strip is particularly important: a
#    multi-repo deployment frequently points it at a non-default path,
#    and that path IS the orchestrator's own write credential.
#
# Suffix matching plus a small bare-name set; the predicate is case-
# insensitive so `database_password` gets the same treatment as
# `DATABASE_PASSWORD`. Allowlisting for the agent's own provider auth
# happens in `_AGENT_PROVIDER_AUTH_ALLOWLIST` below.
_AGENT_SECRET_SUFFIXES = (
    "_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PAT", "_CREDENTIAL",
    # Credential-file locators -- the env-var value is a path that the
    # subprocess can read as the same user. `_CREDENTIALS` (plural) and
    # `_CREDENTIALS_FILE` cover GCP / AWS shapes; the `_FILE`-suffixed
    # variants cover the long tail (`*_TOKEN_FILE`, `*_KEY_FILE`, …).
    "_TOKEN_FILE", "_KEY_FILE", "_SECRET_FILE", "_PASSWORD_FILE",
    "_CREDENTIAL_FILE", "_CREDENTIALS", "_CREDENTIALS_FILE",
)
_AGENT_SECRET_BARE_NAMES = frozenset({
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PAT", "CREDENTIAL",
    "TOKEN_FILE", "CREDENTIALS", "CREDENTIALS_FILE",
})

# Provider-auth keys the agent needs to talk to its OWN model. The shape-based
# filter would otherwise strip these (they all end in `_KEY` / `_TOKEN`), so we
# allowlist by exact name. The scope is intentionally narrow to direct-API
# usage of the two supported backends; advanced deployments (Bedrock, Vertex,
# a self-hosted proxy with a custom env var) need to extend this set
# explicitly rather than have it loosened via a shape match.
_AGENT_PROVIDER_AUTH_ALLOWLIST = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
})


def _is_secret_shaped(name: str) -> bool:
    """True if `name` looks like a production-secret env var.

    Suffix-based detection plus a small bare-name set, matching the shapes
    `_redact_secrets` already treats as secrets. Case-insensitive so
    operators who set `database_password` (lower-cased) get the same
    protection as `DATABASE_PASSWORD`.
    """
    upper = name.upper()
    if upper in _AGENT_SECRET_BARE_NAMES:
        return True
    return any(upper.endswith(suffix) for suffix in _AGENT_SECRET_SUFFIXES)


def _filter_agent_env(
    env: dict[str, str], *, allow_provider_auth: bool = True,
) -> dict[str, str]:
    """Return `env` with GitHub creds + secret-shaped vars stripped.

    Drops keys in `_FORBIDDEN_AGENT_ENV` (the GitHub-specific exact-match
    list), in `_AGENT_WRITE_CREDENTIAL_LOCATORS` (SSH-agent / askpass /
    `GIT_SSH_COMMAND` -- write-credential pointers that aren't
    secret-shaped but let a subprocess use the operator's loaded auth),
    and any key matching `_is_secret_shaped`.

    `allow_provider_auth` controls the narrow exception for the agent's
    own provider auth keys (`_AGENT_PROVIDER_AUTH_ALLOWLIST`):

    * ``True`` (default, agent subprocesses): the allowlist runs --
      `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. ride through so codex
      and claude can reach their own model. Without this the agent CLI
      fails at startup.
    * ``False`` (verify-command subprocesses): the allowlist is bypassed
      and the provider keys are stripped along with everything else.
      Verify commands run untrusted code the agent just produced, so a
      hostile dependency that could read `$ANTHROPIC_API_KEY` would
      gain billable access to the operator's account. The agent CLI is
      not invoked from a verify command in normal use; an operator who
      legitimately needs to drive an agent from a verify command should
      load the key from disk inside a wrapper script (e.g.
      `VERIFY_COMMANDS=./scripts/run-verify.sh` where the script reads
      `~/.config/<provider>/key` and exports it before running tests)
      rather than embedding the literal value in `VERIFY_COMMANDS` --
      the verify failure park comment publishes the offending command
      string verbatim, so an `ANTHROPIC_API_KEY=sk-… pytest` entry
      would leak the secret to GitHub on the first failure.
    """
    filtered: dict[str, str] = {}
    for k, v in env.items():
        if k in _FORBIDDEN_AGENT_ENV:
            continue
        if k in _AGENT_WRITE_CREDENTIAL_LOCATORS:
            continue
        if _is_secret_shaped(k):
            if allow_provider_auth and k in _AGENT_PROVIDER_AUTH_ALLOWLIST:
                filtered[k] = v
            continue
        filtered[k] = v
    return filtered


@dataclass
class AgentResult:
    session_id: Optional[str]
    last_message: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str


# Transitional alias for one release so external imports (debugging scripts,
# downstream tests) keep working while call sites migrate to AgentResult.
CodexResult = AgentResult


def _walk_for_uuid(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        return obj if _UUID_RE.match(obj) else None
    if isinstance(obj, dict):
        for key in _PRIORITY_KEYS:
            if key in obj:
                found = _walk_for_uuid(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = _walk_for_uuid(value)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _walk_for_uuid(item)
            if found:
                return found
    return None


def parse_session_id(jsonl_output: str) -> Optional[str]:
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = _walk_for_uuid(obj)
        if sid:
            return sid
    return None


def _agent_env(extra_env: Optional[dict[str, str]]) -> dict[str, str]:
    env = _filter_agent_env(dict(os.environ))
    # Stamp agent commits with the orchestrator's identity. Env vars take
    # precedence over user.name/user.email from any config scope, so the
    # host's git config is untouched and no per-worktree config is needed.
    env["GIT_AUTHOR_NAME"] = config.AGENT_GIT_NAME
    env["GIT_AUTHOR_EMAIL"] = config.AGENT_GIT_EMAIL
    env["GIT_COMMITTER_NAME"] = config.AGENT_GIT_NAME
    env["GIT_COMMITTER_EMAIL"] = config.AGENT_GIT_EMAIL
    if extra_env:
        env.update(extra_env)
    return env


def _run_subprocess(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[str, str, int, bool]:
    # Spawn the agent in its own process group (start_new_session=True =>
    # setsid). On timeout we send SIGTERM to the whole group, not just the
    # direct child, so that grandchildren the agent forked (Maven, gradle,
    # JVM test runners, ...) are also reaped. Without this, a 30-min build
    # the agent kicked off keeps running for hours after the agent itself
    # was killed -- we hit exactly that with a hudi-spark scalatest sweep.
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    # Register before the first blocking call so a shutdown that fires while
    # this worker is parked in `communicate` can still reach the child group.
    _register_proc(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return stdout or "", stderr or "", proc.returncode, False
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            return stdout or "", stderr or "", -1, True
    finally:
        _unregister_proc(proc)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole process group, then SIGKILL after a grace window.

    ProcessLookupError races are expected (the leader may have exited between
    the Python-side timeout firing and our killpg call) -- swallow them.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_codex(
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
    extra_args: tuple[str, ...] = (),
) -> AgentResult:
    timeout = timeout or config.AGENT_TIMEOUT
    # The -o file lives outside the worktree (per-spawn tempfile) so the
    # target repo's `git status` never sees it as untracked. Putting it
    # inside cwd worked when the orchestrator managed its own repo (whose
    # .gitignore covers `.codex-*`), but broke `_worktree_dirty_files` on
    # any target repo without that rule -- the orchestrator would park
    # awaiting_human on its own scratch on every codex review pass.
    fd, last_msg_path_str = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
    os.close(fd)
    last_msg_path = Path(last_msg_path_str)
    # codex applies `-C` AFTER it has already chdir'd into the subprocess cwd,
    # so a relative path resolves twice (once by Popen, once by codex) and
    # codex hits "No such file or directory (os error 2)". Pass an absolute
    # path so the second resolution is a no-op. WORKTREES_DIR=../wt-...
    # in .env is the common shape that triggers this.
    cwd_abs = Path(cwd).resolve()
    try:
        # `codex exec resume` does not accept -C; we rely on subprocess cwd for it.
        # Configured `extra_args` (e.g. `-m gpt-5.5 -c '...'`) are codex global
        # options, so they go BEFORE the `exec` subcommand. The safety/output
        # flags (`--dangerously-...`, `--json`, `-o`) and the prompt itself
        # stay where they are -- operator-provided args must not be able to
        # silently displace them.
        common = [
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-o", str(last_msg_path),
        ]
        if resume_session_id:
            cmd = [
                config.CODEX_BIN, *extra_args, "exec", "resume",
                *common, resume_session_id, prompt,
            ]
        else:
            cmd = [
                config.CODEX_BIN, *extra_args, "exec", "-C", str(cwd_abs),
                *common, prompt,
            ]

        env = _agent_env(extra_env)
        log.info(
            "codex spawn: cwd=%s resume=%s timeout=%ss",
            cwd, bool(resume_session_id), timeout,
        )

        stdout, stderr, exit_code, timed_out = _run_subprocess(cmd, cwd, env, timeout)

        sid = resume_session_id or parse_session_id(stdout)
        last_msg = ""
        if last_msg_path.exists():
            try:
                last_msg = last_msg_path.read_text(errors="replace")
            except OSError:
                last_msg = ""

        return AgentResult(
            session_id=sid,
            last_message=last_msg,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        try:
            last_msg_path.unlink()
        except FileNotFoundError:
            pass


def _claude_last_message(jsonl_output: str) -> str:
    """Pull the final assistant text out of claude's stream-json output.

    Prefers the terminal `{"type":"result", "result": "..."}` event, which is
    the documented final-message channel. Falls back to the last `assistant`
    or `message` event's text content for forward-compat with schema drift.
    Returns "" on total absence; the question/timeout paths in workflow.py
    already accept an empty last_message.
    """
    last_result: Optional[str] = None
    last_assistant_text: Optional[str] = None
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ev_type = obj.get("type")
        if ev_type == "result":
            res = obj.get("result")
            if isinstance(res, str):
                last_result = res
        elif ev_type in ("assistant", "message"):
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            content = msg.get("content")
            if isinstance(content, list):
                texts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            texts.append(text)
                if texts:
                    last_assistant_text = "".join(texts)
            elif isinstance(content, str):
                last_assistant_text = content
    if last_result is not None:
        return last_result
    return last_assistant_text or ""


def _run_claude(
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
    extra_args: tuple[str, ...] = (),
) -> AgentResult:
    timeout = timeout or config.AGENT_TIMEOUT

    # Configured `extra_args` (e.g. `--model claude-opus-4-7 --effort high`)
    # go right after the binary, before our own flags and the prompt. The
    # safety/output flags (`-p`, `--dangerously-skip-permissions`,
    # `--output-format stream-json`, `--include-partial-messages`,
    # `--verbose`) and the prompt itself stay where they are so operator
    # args cannot silently override them.
    cmd = [
        config.CLAUDE_BIN,
        *extra_args,
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd.append(prompt)

    env = _agent_env(extra_env)
    log.info(
        "claude spawn: cwd=%s resume=%s timeout=%ss",
        cwd, bool(resume_session_id), timeout,
    )

    stdout, stderr, exit_code, timed_out = _run_subprocess(cmd, cwd, env, timeout)

    sid = resume_session_id or parse_session_id(stdout)
    last_msg = _claude_last_message(stdout)

    return AgentResult(
        session_id=sid,
        last_message=last_msg,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
    )


def run_agent(
    backend: str,
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
    extra_args: tuple[str, ...] = (),
) -> AgentResult:
    """Dispatch to the per-backend runner. Config validates `backend` at
    import time, but we re-check here so a misuse from non-config call sites
    fails loudly instead of silently no-opping.

    `extra_args` are forwarded verbatim to the backend CLI (e.g. `-m
    gpt-5.5` for codex, `--model claude-opus-4-7` for claude). Callers
    typically pull these from the role-specific config entries
    (`DEV_AGENT_ARGS`, `REVIEW_AGENT_ARGS`, `DECOMPOSE_AGENT_ARGS`) so a
    role like "implement with codex at xhigh reasoning" stays declarative
    in env. They are injected for both fresh spawns and resumes; the
    backend's own session store carries forward model/effort selection
    across resumes, but explicit args keep the contract identical.
    """
    if backend == "codex":
        runner = _run_codex
    elif backend == "claude":
        runner = _run_claude
    else:
        raise ValueError(
            f"unknown agent backend {backend!r}; expected 'codex' or 'claude'"
        )
    return runner(
        prompt,
        cwd,
        resume_session_id=resume_session_id,
        extra_env=extra_env,
        timeout=timeout,
        extra_args=extra_args,
    )
