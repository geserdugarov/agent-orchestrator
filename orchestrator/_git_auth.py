# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git auth."""
from __future__ import annotations

from orchestrator import _git_plumbing_state as _state
from orchestrator import git_plumbing as _owner

Iterator = _owner.Iterator
Optional = _owner.Optional
Path = _owner.Path
config = _owner.config
contextmanager = _owner.contextmanager
dataclass = _owner.dataclass
os = _owner.os
subprocess = _owner.subprocess
tempfile = _owner.tempfile
threading = _owner.threading
_ASKPASS_MODE = _state._ASKPASS_MODE
_FETCH = _state._FETCH
_GIT = _state._GIT
_GIT_NO_PROMPT_ENV = _state._GIT_NO_PROMPT_ENV
_TARGET_ROOT_LOCKS = _state._TARGET_ROOT_LOCKS
_TARGET_ROOT_LOCKS_LOCK = _state._TARGET_ROOT_LOCKS_LOCK
log = _state.log


@dataclass(frozen=True)
class _GitAuthSession:
    """Token-bearing subprocess inputs scoped to one askpass directory."""

    token: str
    auth_url: str
    env: dict[str, str]


def _resolved_git_token(spec: config.RepoSpec, operation: str) -> Optional[str]:
    """Resolve a per-repository token and log an operation-specific error."""
    token = config._resolve_github_token(spec.slug)
    if token:
        return token
    log.error(
        "GITHUB_TOKEN missing for %s; cannot %s", spec.slug, operation,
    )
    return None


def _git_auth_env(
    askpass: Path, token: str, *, include_identity: bool,
) -> dict[str, str]:
    """Build the detached environment for one token-bearing git command."""
    auth_env = {
        **os.environ,
        **_GIT_NO_PROMPT_ENV,
        "GIT_ASKPASS": str(askpass),
        "GIT_TOKEN": token,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if include_identity:
        auth_env.update(
            {
                "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
                "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
                "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
                "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
            },
        )
    return auth_env


@contextmanager
def _git_auth_session(
    spec: config.RepoSpec, token: str, *, include_identity: bool = False,
) -> Iterator[_GitAuthSession]:
    """Keep a hardened askpass script alive for one authenticated operation."""
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as temp_dir:
        askpass = Path(temp_dir) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(_ASKPASS_MODE)
        yield _GitAuthSession(
            token=token,
            auth_url=f"https://x-access-token@github.com/{spec.slug}.git",
            env=_owner._git_auth_env(
                askpass, token, include_identity=include_identity,
            ),
        )


def _failed_fetch(stderr: str) -> subprocess.CompletedProcess:
    """Return the stable failure shape shared by authenticated fetches."""
    return subprocess.CompletedProcess(
        args=[_GIT, _FETCH], returncode=1, stdout="", stderr=stderr,
    )


def _target_root_lock(target_root: Path) -> threading.RLock:
    """Return the lock that serializes git plumbing against `target_root`.

    The process-local registry creates locks lazily and retains each lock for
    the target root's lifetime. Locks are re-entrant so a caller already
    holding one can call a helper that acquires it again.
    """
    return _TARGET_ROOT_LOCKS.for_root(target_root)
