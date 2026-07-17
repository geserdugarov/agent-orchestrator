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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional, TypedDict, Unpack

from orchestrator import config
from orchestrator.usage import UsageMetrics

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


@contextmanager
def _registered(proc: subprocess.Popen) -> Iterator[subprocess.Popen]:
    """Keep `proc` in `_running_procs` for the duration of the block.

    The shutdown sweep (`terminate_all_running`) can only reach a group that
    is in the registry, so every producer registers before its first blocking
    wait and clears the entry when the run ends -- on a normal return, an
    early return, or an exception -- so a completed process never leaks into
    the registry and keeps getting SIGTERMed on the next shutdown.
    """
    _register_proc(proc)
    try:
        yield proc
    finally:
        _unregister_proc(proc)


def _communicate_bounded(
    proc: subprocess.Popen, timeout: float,
) -> Optional[tuple[str, str]]:
    """`proc.communicate()` with a wall-clock cap; None if it times out.

    Returns `(stdout, stderr)` with each stream coerced to `""` when the
    child left it empty. A `None` return means the drain itself blocked past
    `timeout` -- the caller decides whether to kill harder and retry or fall
    back to empty output. Only `TimeoutExpired` is swallowed; any other error
    propagates.
    """
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return stdout or "", stderr or ""


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


def _sigkill_unless_group_gone(proc: subprocess.Popen, timeout: float) -> None:
    """Wait up to `timeout` for `proc`'s leader, then SIGKILL a live group.

    `proc.wait()` only observes the group *leader*: a descendant that ignored
    SIGTERM keeps running -- and keeps mutating the worktree -- after the
    leader exits, so leader-exit is not proof the group is gone. We SIGKILL
    the group unless the leader exited AND a `killpg(_, 0)` probe shows it has
    no surviving member; a leader still alive at the deadline means a live
    group, so it is SIGKILLed without a probe. Without this, a build
    grandchild the agent forked (Maven, gradle, a JVM test runner) could go on
    writing the worktree after the timeout was recorded -- exactly the
    post-timeout commit that stranded a clean implementing branch behind
    `awaiting_human`.

    The `ProcessLookupError` on the SIGKILL is an expected race (the group
    exited between the probe and the signal) and is swallowed.
    """
    leader_exited = True
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        leader_exited = False
    if leader_exited and not _process_group_alive(proc.pid):
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
    flight; `_sigkill_unless_group_gone` SIGKILLs any group still alive at the
    deadline (see it for the leader-vs-group safety model).

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
        remaining = max(0, deadline - time.monotonic())
        _sigkill_unless_group_gone(proc, remaining)
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
_FORBIDDEN_AGENT_ENV = frozenset((
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
    "GH_HOST",
))

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
_AGENT_WRITE_CREDENTIAL_LOCATORS = frozenset((
    "SSH_AUTH_SOCK",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "GIT_SSH_COMMAND",
))

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
_AGENT_SECRET_BARE_NAMES = frozenset((
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PAT", "CREDENTIAL",
    "TOKEN_FILE", "CREDENTIALS", "CREDENTIALS_FILE",
))

# Provider-auth keys the agent needs to talk to its OWN model. The shape-based
# filter would otherwise strip these (they all end in `_KEY` / `_TOKEN`), so we
# allowlist by exact name. The scope is intentionally narrow to direct-API
# usage of the two supported backends; advanced deployments (Bedrock, Vertex,
# a self-hosted proxy with a custom env var) need to extend this set
# explicitly rather than have it loosened via a shape match.
_AGENT_PROVIDER_AUTH_ALLOWLIST = frozenset((
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
))


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


def _agent_env_key_allowed(
    env_key: str, *, allow_provider_auth: bool,
) -> bool:
    if env_key in _FORBIDDEN_AGENT_ENV:
        return False
    if env_key in _AGENT_WRITE_CREDENTIAL_LOCATORS:
        return False
    if not _is_secret_shaped(env_key):
        return True
    return (
        allow_provider_auth
        and env_key in _AGENT_PROVIDER_AUTH_ALLOWLIST
    )


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
    filtered_env: dict[str, str] = {}
    for env_key, env_value in env.items():
        if _agent_env_key_allowed(
            env_key, allow_provider_auth=allow_provider_auth,
        ):
            filtered_env[env_key] = env_value
    return filtered_env


# Negative `Popen.returncode` values (the kernel reports `-N` for a child
# killed by signal N) that mean the run was cut short rather than completing
# on its own. The orchestrator's shutdown sweep (`terminate_all_running`)
# SIGTERMs then SIGKILLs every in-flight group, so a worker parked in
# `communicate` observes exactly these codes when a restart kills its agent
# mid-run; an external SIGKILL (OOM killer, operator `kill`) lands here too.
# Classifying them as `interrupted` keeps stage handlers from treating a
# half-finished run as a normal non-zero failure and from publishing a partial
# last message as the agent's considered final answer.
_INTERRUPTED_RETURNCODES = frozenset((-signal.SIGTERM, -signal.SIGKILL))


@dataclass
class AgentResult:
    session_id: Optional[str]
    last_message: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    # Shutdown-killed / signal-terminated mid-run (distinct from `timed_out`).
    # Defaulted so existing constructions stay valid without the field.
    interrupted: bool = False
    # Parsed run usage, populated by `analytics.record_agent_exit` from
    # `stdout` during a tracked run. Defaulted so existing constructions stay
    # valid without the field; stays `None` for any result that did not flow
    # through `_run_agent_tracked` or whose usage parse failed (fail-open), so
    # callers must treat it as best-effort.
    usage: Optional[UsageMetrics] = None


# Transitional alias for one release so external imports (debugging scripts,
# downstream tests) keep working while call sites migrate to AgentResult.
CodexResult = AgentResult


@dataclass(frozen=True)
class AgentRunOptions:
    """Optional controls shared by fresh agent runs and session resumes."""

    resume_session_id: Optional[str] = None
    extra_env: Optional[dict[str, str]] = None
    timeout: Optional[int] = None
    extra_args: tuple[str, ...] = ()

    @property
    def timeout_seconds(self) -> int:
        return self.timeout or config.AGENT_TIMEOUT


class _AgentRunOptionFields(TypedDict, total=False):
    resume_session_id: Optional[str]
    extra_env: Optional[dict[str, str]]
    timeout: Optional[int]
    extra_args: tuple[str, ...]


class _SubprocessResult(NamedTuple):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    interrupted: bool


def _resolve_agent_run_options(
    options: Optional[AgentRunOptions],
    option_fields: _AgentRunOptionFields,
) -> AgentRunOptions:
    if options is not None and option_fields:
        raise TypeError("pass either options or keyword option fields, not both")
    if options is not None:
        return options
    return AgentRunOptions(**option_fields)


def _first_nested_uuid(payload_nodes: Iterator[Any]) -> Optional[str]:
    for payload_node in payload_nodes:
        found = _walk_for_uuid(payload_node)
        if found is not None:
            return found
    return None


def _walk_mapping_for_uuid(payload_node: dict[Any, Any]) -> Optional[str]:
    priority_values = (
        payload_node[key]
        for key in _PRIORITY_KEYS
        if key in payload_node
    )
    priority_match = _first_nested_uuid(priority_values)
    if priority_match is not None:
        return priority_match
    return _first_nested_uuid(iter(payload_node.values()))


def _walk_for_uuid(payload_node: Any) -> Optional[str]:
    if isinstance(payload_node, str):
        return payload_node if _UUID_RE.match(payload_node) else None
    if isinstance(payload_node, dict):
        return _walk_mapping_for_uuid(payload_node)
    if isinstance(payload_node, list):
        return _first_nested_uuid(iter(payload_node))
    return None


def parse_session_id(jsonl_output: str) -> Optional[str]:
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event_payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = _walk_for_uuid(event_payload)
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
) -> _SubprocessResult:
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
    with _registered(proc):
        drained = _communicate_bounded(proc, timeout)
        if drained is None:
            # Our own timeout: classified as `timed_out`, not `interrupted`,
            # even though `_terminate_process_group` signals the group below.
            # A bounded second drain reads whatever the child buffered before
            # the kill; `("", "")` if that drain itself wedges.
            _terminate_process_group(proc)
            drained = _communicate_bounded(proc, 10)
            stdout, stderr = ("", "") if drained is None else drained
            return _SubprocessResult(stdout, stderr, -1, True, False)
        # A child killed by SIGTERM/SIGKILL exits with a negative code; the
        # most common cause is the shutdown sweep reaching this group while we
        # were parked in `communicate`. Flag it interrupted so it is not
        # mistaken for a normal non-timeout completion.
        stdout, stderr = drained
        interrupted = proc.returncode in _INTERRUPTED_RETURNCODES
        return _SubprocessResult(
            stdout, stderr, proc.returncode, False, interrupted,
        )


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole process group, then SIGKILL if anything survives.

    The per-timeout cleanup for a single agent run.
    `_sigkill_unless_group_gone` carries the leader-vs-group safety model
    shared with `terminate_all_running`: after the SIGTERM it waits a grace
    period for the leader and SIGKILLs the group unless a `killpg(_, 0)` probe
    proves it empty.

    The initial-SIGTERM `ProcessLookupError` is an expected race (the leader
    may have exited between the Python-side timeout firing and our killpg
    call); short-circuit, since an already-gone group needs no further kill.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    _sigkill_unless_group_gone(proc, timeout=5)


@contextmanager
def _codex_last_message_file() -> Iterator[Path]:
    """Yield a per-spawn tempfile path for codex's `-o` last-message output.

    The file lives OUTSIDE the worktree so the target repo's `git status`
    never sees it as untracked. Putting it inside cwd worked when the
    orchestrator managed its own repo (whose .gitignore covers `.codex-*`),
    but broke `_worktree_dirty_files` on any target repo without that rule --
    the orchestrator would park awaiting_human on its own scratch on every
    codex review pass. Removed when the block exits, tolerating a codex run
    that never wrote it.
    """
    fd, path_str = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
    os.close(fd)
    path = Path(path_str)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _read_last_message(path: Path) -> str:
    """Read codex's `-o` last-message file, or '' if it is absent/unreadable.

    A run that was interrupted before codex flushed the file leaves it
    missing; a read error is treated the same way. Callers (the question /
    timeout paths in workflow.py) already accept an empty last_message.
    """
    if not path.exists():
        return ""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _build_agent_result(
    options: AgentRunOptions,
    process_result: _SubprocessResult,
    last_message: str,
) -> AgentResult:
    return AgentResult(
        session_id=(
            options.resume_session_id
            or parse_session_id(process_result.stdout)
        ),
        last_message=last_message,
        exit_code=process_result.exit_code,
        timed_out=process_result.timed_out,
        stdout=process_result.stdout,
        stderr=process_result.stderr,
        interrupted=process_result.interrupted,
    )


def _codex_command(
    prompt: str,
    cwd: Path,
    last_message_path: Path,
    options: AgentRunOptions,
) -> list[str]:
    # codex applies `-C` AFTER it has already chdir'd into the subprocess cwd,
    # so a relative path resolves twice (once by Popen, once by codex) and
    # codex hits "No such file or directory (os error 2)". Pass an absolute
    # path so the second resolution is a no-op. WORKTREES_DIR=../wt-...
    # in .env is the common shape that triggers this.
    cwd_abs = Path(cwd).resolve()
    # `codex exec resume` does not accept -C; we rely on subprocess cwd for it.
    # Configured `extra_args` (e.g. `-m gpt-5.5 -c '...'`) are codex global
    # options, so they go BEFORE the `exec` subcommand. The safety/output
    # flags (`--dangerously-...`, `--json`, `-o`) and the prompt itself
    # stay where they are -- operator-provided args must not be able to
    # silently displace them.
    common_args = [
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "-o", str(last_message_path),
    ]
    if options.resume_session_id:
        return [
            config.CODEX_BIN, *options.extra_args, "exec", "resume",
            *common_args, options.resume_session_id, prompt,
        ]
    return [
        config.CODEX_BIN, *options.extra_args, "exec", "-C", str(cwd_abs),
        *common_args, prompt,
    ]


def _log_agent_spawn(
    backend: str, cwd: Path, options: AgentRunOptions,
) -> None:
    log.info(
        "%s spawn: cwd=%s resume=%s timeout=%ss",
        backend,
        cwd,
        bool(options.resume_session_id),
        options.timeout_seconds,
    )


def _run_codex(
    prompt: str,
    cwd: Path,
    *,
    options: Optional[AgentRunOptions] = None,
    **option_fields: Unpack[_AgentRunOptionFields],
) -> AgentResult:
    run_options = _resolve_agent_run_options(options, option_fields)
    with _codex_last_message_file() as last_message_path:
        _log_agent_spawn("codex", cwd, run_options)
        process_result = _run_subprocess(
            _codex_command(prompt, cwd, last_message_path, run_options),
            cwd,
            _agent_env(run_options.extra_env),
            run_options.timeout_seconds,
        )
        return _build_agent_result(
            run_options,
            process_result,
            _read_last_message(last_message_path),
        )


def _decode_claude_event(raw_line: str) -> Optional[dict[str, Any]]:
    """Decode one stream event, ignoring blank or diagnostic output."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        event_payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event_payload if isinstance(event_payload, dict) else None


def _iter_claude_events(jsonl_output: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from Claude's mixed JSONL output."""
    for raw_line in jsonl_output.splitlines():
        event_payload = _decode_claude_event(raw_line)
        if event_payload is not None:
            yield event_payload


def _collect_claude_text_blocks(
    content_blocks: list[Any],
) -> Optional[str]:
    """Join the valid text blocks from one assistant message."""
    text_blocks: list[str] = []
    for content_block in content_blocks:
        if not isinstance(content_block, dict):
            continue
        if content_block.get("type") != "text":
            continue
        block_text = content_block.get("text")
        if isinstance(block_text, str):
            text_blocks.append(block_text)
    return "".join(text_blocks) if text_blocks else None


def _claude_result_text(event_payload: dict[str, Any]) -> Optional[str]:
    """Return a terminal result string without filtering its subtype."""
    if event_payload.get("type") != "result":
        return None
    result_text = event_payload.get("result")
    return result_text if isinstance(result_text, str) else None


def _claude_assistant_text(event_payload: dict[str, Any]) -> Optional[str]:
    """Return text from a supported assistant or message event."""
    if event_payload.get("type") not in ("assistant", "message"):
        return None
    nested_message = event_payload.get("message")
    message_payload = (
        nested_message if isinstance(nested_message, dict) else event_payload
    )
    message_content = message_payload.get("content")
    if isinstance(message_content, list):
        return _collect_claude_text_blocks(message_content)
    return message_content if isinstance(message_content, str) else None


def _collect_claude_message_candidates(
    events: Iterator[dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """Keep the latest valid terminal and assistant message candidates."""
    last_result: Optional[str] = None
    last_assistant_text: Optional[str] = None
    for event_payload in events:
        result_text = _claude_result_text(event_payload)
        if result_text is not None:
            last_result = result_text
        assistant_text = _claude_assistant_text(event_payload)
        if assistant_text is not None:
            last_assistant_text = assistant_text
    return last_result, last_assistant_text


def _select_claude_last_message(
    last_result: Optional[str],
    last_assistant_text: Optional[str],
    *,
    allow_assistant_fallback: bool,
) -> str:
    """Prefer a terminal result and gate partial-transcript fallback."""
    if last_result is not None:
        return last_result
    if allow_assistant_fallback:
        return last_assistant_text or ""
    return ""


def _claude_last_message(
    jsonl_output: str, *, allow_assistant_fallback: bool = True,
) -> str:
    """Pull the final assistant text out of claude's stream-json output.

    Prefers the terminal `{"type":"result", "result": "..."}` event, which is
    the documented final-message channel and is honored regardless of how the
    run ended. Falls back to the last `assistant` or `message` event's text
    content for forward-compat with schema drift -- but only when
    `allow_assistant_fallback` is True. The caller passes False for interrupted
    or non-zero runs: without a terminal `result` event those produce a partial
    transcript, and treating the last streamed chunk as the agent's considered
    final answer is wrong, so they get "" instead. Returns "" on total absence;
    the question/timeout paths in workflow.py already accept an empty
    last_message.
    """
    events = _iter_claude_events(jsonl_output)
    last_result, last_assistant_text = _collect_claude_message_candidates(
        events
    )
    return _select_claude_last_message(
        last_result,
        last_assistant_text,
        allow_assistant_fallback=allow_assistant_fallback,
    )


def _claude_command(
    prompt: str, options: AgentRunOptions,
) -> list[str]:
    # Configured `extra_args` (e.g. `--model claude-opus-4-7 --effort high`)
    # go right after the binary, before our own flags and the prompt. The
    # safety/output flags (`-p`, `--dangerously-skip-permissions`,
    # `--output-format stream-json`, `--include-partial-messages`,
    # `--verbose`) and the prompt itself stay where they are so operator
    # args cannot silently override them.
    command = [
        config.CLAUDE_BIN,
        *options.extra_args,
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if options.resume_session_id:
        command += ["--resume", options.resume_session_id]
    command.append(prompt)
    return command


def _claude_process_last_message(
    process_result: _SubprocessResult,
) -> str:
    # Only a clean, completed run may fall back to the last streamed assistant
    # chunk; interrupted/timed-out/non-zero runs expose "" unless they emitted
    # a terminal `result` event.
    succeeded = (
        process_result.exit_code == 0
        and not process_result.timed_out
        and not process_result.interrupted
    )
    return _claude_last_message(
        process_result.stdout,
        allow_assistant_fallback=succeeded,
    )


def _run_claude(
    prompt: str,
    cwd: Path,
    *,
    options: Optional[AgentRunOptions] = None,
    **option_fields: Unpack[_AgentRunOptionFields],
) -> AgentResult:
    run_options = _resolve_agent_run_options(options, option_fields)
    _log_agent_spawn("claude", cwd, run_options)
    process_result = _run_subprocess(
        _claude_command(prompt, run_options),
        cwd,
        _agent_env(run_options.extra_env),
        run_options.timeout_seconds,
    )
    return _build_agent_result(
        run_options,
        process_result,
        _claude_process_last_message(process_result),
    )


def run_agent(
    backend: str,
    prompt: str,
    cwd: Path,
    *,
    options: Optional[AgentRunOptions] = None,
    **option_fields: Unpack[_AgentRunOptionFields],
) -> AgentResult:
    """Dispatch to the per-backend runner. Config validates `backend` at
    import time, but we re-check here so a misuse from non-config call sites
    fails loudly instead of silently no-opping.

    The optional controls can be passed as an `AgentRunOptions` object or as
    the established keyword fields. `extra_args` are forwarded verbatim to
    the backend CLI (e.g. `-m gpt-5.5` for codex, `--model
    claude-opus-4-7` for claude). Callers typically pull these from the
    role-specific config entries
    (`DEV_AGENT_ARGS`, `REVIEW_AGENT_ARGS`, `DECOMPOSE_AGENT_ARGS`) so a
    role like "implement with codex at xhigh reasoning" stays declarative
    in env. They are injected for both fresh spawns and resumes; the
    backend's own session store carries forward model/effort selection
    across resumes, but explicit args keep the contract identical.
    """
    run_options = _resolve_agent_run_options(options, option_fields)
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
        options=run_options,
    )
