# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git push."""
from __future__ import annotations

from orchestrator import _git_plumbing_state as _state
from orchestrator import git_plumbing as _owner

_GitAuthSession = _owner._GitAuthSession
Optional = _owner.Optional
Path = _owner.Path
config = _owner.config
subprocess = _owner.subprocess
_AUTHED_GIT_PREFIX = _state._AUTHED_GIT_PREFIX
log = _state.log


def _remote_branch_sha(
    auth_session: _GitAuthSession,
    worktree: Path,
    branch: str,
    ref: str,
    force_with_lease: Optional[str],
) -> Optional[str]:
    """Return the expected remote SHA, or None when it cannot be read."""
    if force_with_lease is not None:
        return force_with_lease
    ls_remote = subprocess.run(
        [*_AUTHED_GIT_PREFIX, "ls-remote", auth_session.auth_url, ref],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        env=auth_session.env,
    )
    if ls_remote.returncode != 0:
        scrubbed = (ls_remote.stderr or "").replace(
            auth_session.token, "***",
        )
        log.error("git ls-remote failed for %s: %s", branch, scrubbed)
        return None
    for output_line in (ls_remote.stdout or "").splitlines():
        parts = output_line.strip().split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    return ""


def _push_with_auth(
    auth_session: _GitAuthSession,
    worktree: Path,
    branch: str,
    force_with_lease: Optional[str],
) -> bool:
    """Push one branch through an established askpass session."""
    ref = f"refs/heads/{branch}"
    remote_sha = _owner._remote_branch_sha(
        auth_session, worktree, branch, ref, force_with_lease,
    )
    if remote_sha is None:
        return False
    push_result = subprocess.run(
        [
            *_AUTHED_GIT_PREFIX,
            "push",
            f"--force-with-lease={ref}:{remote_sha}",
            auth_session.auth_url,
            f"HEAD:{ref}",
        ],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        env=auth_session.env,
    )
    if push_result.returncode == 0:
        return True
    scrubbed = (push_result.stderr or "").replace(
        auth_session.token, "***",
    )
    log.error("git push failed for %s: %s", branch, scrubbed)
    return False


def _push_branch(
    spec: config.RepoSpec, worktree: Path, branch: str,
    *,
    force_with_lease: Optional[str] = None,
) -> bool:
    """Push via GIT_ASKPASS so the token never appears in argv.

    `force_with_lease`, when provided, is the SHA the caller expects the
    remote ref to be at. The push then uses
    `--force-with-lease=refs/heads/<branch>:<sha>` against that exact SHA,
    so a concurrent update to the remote rejects the push instead of being
    silently clobbered. This is the squash/rewrite path: pinning the lease
    to the caller-supplied pre-rewrite HEAD (rather than reading it from
    the live remote) prevents the "out-of-band update happened in the
    window between approval and push" race -- a fresh `ls-remote` would
    treat the unexpected new remote SHA as the lease value and silently
    overwrite it.

    When `force_with_lease` is None (the default), the function reads the
    current remote SHA via `ls-remote` and uses that as the lease. This is
    the normal-push path: the orchestrator owns the
    `orchestrator/<slug>/issue-<n>` namespace, but a self-restart between commit
    and push can leave the worktree on a different SHA than what was
    already pushed -- e.g. a `resume=False` rerun of codex amending
    equivalent work. A plain push then fails non-fast-forward and parks
    the issue. The lease lets the retry succeed while still refusing to
    clobber a concurrent foreign update (the lease check compares against
    what we observed, not a stale remote-tracking ref).

    The push target URL carries only the username (`x-access-token`); the
    token itself is read from the GIT_TOKEN env var by a tempfile askpass
    script. This keeps the PAT out of `/proc/<pid>/cmdline`, which is
    world-readable on Linux. We also use an explicit `HEAD:refs/heads/<branch>`
    refspec so no upstream is set and no remote URL is stored in .git/config.

    The worktree is shared with the codex agent, so anything in `.git/hooks/`
    or `.git/config` is attacker-controlled. The agent also writes as the same
    OS user, so it can plant `~/.gitconfig` (or anything pointed at by
    XDG_CONFIG_HOME) before we push. We harden the push so a planted pre-push
    hook, credential helper, fsmonitor, url-rewrite rule, or http proxy /
    TLS override cannot observe GIT_TOKEN or redirect the push to an
    attacker-controlled host:
      * `core.hooksPath=/dev/null` disables `.git/hooks/*` and any hooksPath
        override the agent set in the local config.
      * `credential.helper=` (empty) clears all inherited credential helpers
        so a repo-local helper script never executes with GIT_TOKEN in env.
      * `core.fsmonitor=` disables any fsmonitor program git would otherwise
        spawn for index-touching operations.
      * `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_SYSTEM=/dev/null` block
        global/system config entirely, so url.<host>.insteadOf or
        pushInsteadOf rules planted in `~/.gitconfig` (or `/etc/gitconfig`)
        cannot rewrite our auth URL and exfiltrate the askpass token.
      * We also refuse to push if the local config contains any url
        insteadOf/pushInsteadOf rewrite or any `http.*` transport setting
        (`_unsafe_local_transport_config`). A rewrite redelivers the token
        to whatever host the agent picked; a local `http.proxy` /
        `http.sslVerify=false` (or URL-scoped `http.<url>.*` variant, which a
        command-line `-c http.proxy=` override cannot beat) would tunnel the
        token-bearing push through an attacker proxy or disable TLS
        verification. Env-var proxies (`https_proxy`) are operator-set and
        stay honored -- only agent-writable config-file transport is rejected.
    """
    # Resolve the token from `spec.slug` rather than the cached
    # `config.GITHUB_TOKEN` (which was looked up once for `config.REPO`),
    # so a multi-repo deployment with one token file per slug under
    # `~/.config/<owner>/<repo>/token` pushes with the right repo's token.
    # Single-repo deployments see identical behavior because
    # `_resolve_github_token(REPO)` returns the same value.
    token = _owner._resolved_git_token(spec, "push")
    if not token:
        return False
    unsafe = _owner._unsafe_local_transport_config(worktree)
    if unsafe:
        log.error(
            "refusing to push %s: worktree .git/config has "
            "transport-hijacking config: %s", branch, unsafe,
        )
        return False
    with _owner._git_auth_session(spec, token) as auth_session:
        # An empty expected SHA means the remote ref must not exist, which
        # preserves the create-branch lease behavior.
        return _owner._push_with_auth(
            auth_session, worktree, branch, force_with_lease,
        )
