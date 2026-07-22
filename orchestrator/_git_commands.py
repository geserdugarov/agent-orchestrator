# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git commands."""
from __future__ import annotations

from orchestrator import _git_plumbing_state as _state
from orchestrator import git_plumbing as _owner

Path = _owner.Path
config = _owner.config
os = _owner.os
subprocess = _owner.subprocess
_GIT = _state._GIT
_GIT_NO_PROMPT_ENV = _state._GIT_NO_PROMPT_ENV
_HARDENED_GIT_PREFIX = _state._HARDENED_GIT_PREFIX
_UNSAFE_TRANSPORT_CONFIG_RE = _state._UNSAFE_TRANSPORT_CONFIG_RE


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_GIT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_NO_PROMPT_ENV},
    )


def _git_hardened(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """`_git` plus the agent-hostile-environment hardening from `_push_branch`.

    Used for local git operations inside a worktree the agent can write to: a
    planted `core.hooksPath`, `core.fsmonitor`, or url rewrite rule in
    the worktree's `.git/config` (or in `~/.gitconfig`) would otherwise
    execute attacker code mid-operation or redirect a transient fetch to an
    attacker-controlled host. Drops global/system git config so url
    `insteadOf` rewrites and host-wide hooks cannot apply, and disables
    repo-local hooks / fsmonitor / credential helpers / commit signing via
    `-c` overrides. No askpass is wired in -- this helper is for local-only
    operations (rebase, diff, rev-parse); push remains the only call site
    that handles GIT_TOKEN.

    Injects `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars (matching the
    agent spawn's `_agent_env`) so a `git rebase` that needs to replay
    commits doesn't fail with "Committer identity unknown" -- stripping
    global config also strips any `user.name` / `user.email` set there,
    and env vars take precedence over config.
    """
    env = {
        **os.environ,
        **_GIT_NO_PROMPT_ENV,
        "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
        "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
        "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        [*_HARDENED_GIT_PREFIX, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def _unsafe_local_transport_config(cwd: Path) -> str:
    """Return non-global git config in `cwd` that could hijack token transport.

    Scans the exact config view a token-bearing fetch/push honors: the local
    config plus any `include.path` file it pulls in and, when
    `extensions.worktreeConfig` is set, the per-worktree `config.worktree` --
    with global/system config detached (the same `GIT_CONFIG_GLOBAL`/`SYSTEM`
    envelope the fetch/push runs under). It deliberately does NOT scope to
    `--local`: a `git config --local` probe reads only the raw local file, so
    it misses `include.path` targets and per-worktree config that the real
    command still resolves and honors. Returns the matching
    `git config --get-regexp` lines joined for logging, or "" when the config
    view is clean; callers refuse to run any GIT_TOKEN-bearing git command
    while the result is non-empty.
    """
    probe = subprocess.run(
        [_GIT, "config", "--get-regexp", _UNSAFE_TRANSPORT_CONFIG_RE],
        cwd=str(cwd), capture_output=True, text=True,
        env={
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return probe.stdout.strip()
    return ""
