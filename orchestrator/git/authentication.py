# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Token-bearing git fetches and pushes plus the askpass session they run under.

Token resolution, the askpass session, the two fetches, the branch push, and
the fully-qualified ref plumbing beside it share this module because the token
only ever travels through the session: the session writes the askpass script
that prints it and builds the environment that carries it, and each transport
call passes that environment along with an argv that names nothing but the
`x-access-token` username. Splitting them would put a raw token on a
module boundary.

The ref plumbing is the branch push read one level down -- a remote read, a
write, and a delete against a whole refname rather than a branch -- and it
exists for the caller that owns an immutable namespace rather than a branch.
What differs is the lease: a branch push may look the remote up for itself,
because a branch is a moving thing whose current tip is the honest expectation,
while a ref update here states what the caller established was there and has
no form that overwrites whatever it finds. What policy that serves -- which
namespace, and what an existing ref at another commit means -- belongs to
`git/snapshots/`, which is the only caller.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from orchestrator import config
from orchestrator.git import commands, locks

# The channel is named for the git-plumbing domain rather than for this
# module's path: operators filter the rendered `orchestrator.git_plumbing`
# prefix and attach handlers to it, so every fetch and push refusal reports
# where their filters already point.
log = logging.getLogger("orchestrator.git_plumbing")

_FETCH = "fetch"

_PUSH = "push"

# What a push publishes when the caller names no commit of its own: whatever
# the worktree is on now, which is right for every caller that just made the
# work it is publishing.
_HEAD = "HEAD"

_ASKPASS_MODE = 0o700


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
        **commands._GIT_NO_PROMPT_ENV,
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
            env=_git_auth_env(
                askpass, token, include_identity=include_identity,
            ),
        )


def _failed_fetch(stderr: str) -> subprocess.CompletedProcess:
    """Return the stable failure shape shared by authenticated fetches."""
    return subprocess.CompletedProcess(
        args=[commands._GIT, _FETCH], returncode=1, stdout="", stderr=stderr,
    )


def _authed_fetch(
    spec: config.RepoSpec, refspec: str, *, cwd: Path
) -> subprocess.CompletedProcess:
    """Authenticated, hardened `git fetch` -- the same security envelope as
    `_push_branch`.

    Used for fetches from inside an agent-writable worktree where any
    of the following vectors could leak GIT_TOKEN to an attacker host:
      * a planted credential helper in the worktree's `.git/config`,
      * a planted `core.hooksPath` / `core.fsmonitor` that runs an
        attacker-controlled binary with GIT_TOKEN in env,
      * a planted `url.<host>.insteadOf` rewrite in the worktree's
        local config OR in `~/.gitconfig` redirecting fetch to an
        attacker-controlled host,
      * a planted `http.proxy` / `http.sslVerify=false` (or other
        `http.*` TLS/proxy key) in the worktree's local config routing
        the token-bearing fetch through an attacker proxy or disabling
        certificate verification.

    The auth URL carries only the username (`x-access-token`); the
    token itself is read from $GIT_TOKEN by a tempfile askpass script
    so it never appears in argv. Global/system git config is detached
    via `GIT_CONFIG_GLOBAL=/dev/null` / `GIT_CONFIG_SYSTEM=/dev/null`
    so url-rewrite rules planted there cannot apply. We also refuse to
    run if the worktree's local config carries any url-rewrite rule or
    `http.*` transport setting (`_unsafe_local_transport_config`),
    mirroring `_push_branch`'s pre-flight check.

    `refspec` is the fetch refspec; pass an explicit form like
    `+refs/heads/<branch>:refs/remotes/origin/<branch>` so single-branch
    clones still update the remote-tracking ref instead of leaving the
    fetched payload only in FETCH_HEAD.

    The fetch updates the parent clone's `refs/remotes/<remote>/...`
    namespace from inside an agent-writable worktree, which means it
    grabs the parent's ref-update lock under `<git-dir>/packed-refs.lock`
    and `<git-dir>/refs/remotes/<remote>/<branch>.lock`. Two concurrent
    `_authed_fetch` calls from different worktrees of the same
    `target_root` (the common shape during fan-out of multiple
    `resolving_conflict` issues) race those lock files and one fails
    with `Unable to create '...': File exists.`, parking the issue.
    The actual subprocess call is therefore held under the
    per-target_root lock; the pre-flight URL-rewrite check stays
    outside the lock since it only reads the worktree's own
    `.git/config`.
    """
    # Resolve the token from `spec.slug` rather than the cached
    # `config.GITHUB_TOKEN` (which was looked up once for `config.REPO`),
    # so a multi-repo deployment with one token file per slug under
    # `~/.config/<owner>/<repo>/token` fetches with the right repo's token.
    # Mirrors `_push_branch`'s per-spec token resolution; without this,
    # `_handle_resolving_conflict` would fail conflict resolution for any
    # repo other than the legacy `REPO` (or use the wrong token).
    token = _resolved_git_token(spec, _FETCH)
    if not token:
        return _failed_fetch("GITHUB_TOKEN missing")
    unsafe = commands._unsafe_local_transport_config(cwd)
    if unsafe:
        log.error(
            "refusing to fetch into %s: worktree .git/config has "
            "transport-hijacking config: %s", cwd, unsafe,
        )
        return _failed_fetch(
            "unsafe transport config in worktree .git/config",
        )
    with _git_auth_session(
        spec, token, include_identity=True,
    ) as auth_session:
        with locks._target_root_lock(spec.target_root):
            return subprocess.run(
                [
                    *commands._AUTHED_GIT_PREFIX,
                    _FETCH, "--quiet", auth_session.auth_url, refspec,
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=auth_session.env,
            )


def _authed_target_fetch(
    spec: config.RepoSpec, branch: str
) -> subprocess.CompletedProcess:
    """Authed `git fetch` into `spec.target_root` using the per-spec token.

    Replaces the plain `git fetch <remote_name> <branch>` invocations the
    worktree creators (`_ensure_worktree` / `_ensure_pr_worktree` /
    `_ensure_decompose_worktree`) and the per-tick base refresh
    (`_refresh_base_and_worktrees`) used to run. The plain form relied on
    git's ambient credential helper or session state, which fails under
    systemd (`GIT_TERMINAL_PROMPT=0` disables the fallback prompt) and
    has no way to pick a per-repo token when the local clone has several
    GitHub-pointing remotes whose `slug` differs from the
    `~/.config/<owner>/<repo>/token` of the configured `REPO`.

    The `spec.remote_name` field selects the local remote namespace --
    refs land under `refs/remotes/<spec.remote_name>/<branch>` -- while
    `spec.slug` selects which GitHub repo / token to authenticate with.
    Without this split, a `REPOS` row like
    `geserdugarov/lance-private|...|private-cache|private` would try to
    use the cached single-repo `config.GITHUB_TOKEN` (looked up once for
    `config.REPO`) and fail to fetch even with a correct per-spec token
    file in place.

    An explicit refspec `+refs/heads/<branch>:refs/remotes/<remote_name>/<branch>`
    is used so single-branch / narrowed clones still update the
    remote-tracking ref instead of leaving the fetched payload only in
    FETCH_HEAD -- the worktree creators then anchor `git worktree add`
    on `<remote>/<branch>` without surprise.

    Same security envelope as `_push_branch` / `_authed_fetch`: token
    delivered via GIT_ASKPASS (never argv), global/system git config
    detached so url-rewrite rules planted in `~/.gitconfig` cannot
    redirect the fetch to an attacker-controlled host, hooks /
    fsmonitor / credential helpers blocked via `-c` overrides. The
    target_root is normally operator-owned, but a linked worktree
    (which the agent does write) can still mutate the parent clone's
    local config via `git config --local`, and local config still
    applies even with GIT_CONFIG_GLOBAL/SYSTEM detached. Mirror the
    `_authed_fetch` / `_push_branch` pre-flight refusal: bail out if
    `target_root`'s local config carries any
    `url.<host>.(insteadOf|pushInsteadOf)` rule or `http.*` proxy/TLS
    setting that could redirect the token-bearing fetch to an
    attacker-controlled host or strip TLS verification
    (`_unsafe_local_transport_config`).

    Serialized via `_target_root_lock` (`RLock` so a caller already
    holding it -- the worktree creators -- re-enters cleanly) for the
    same `.git/config.lock` reason described on `_ensure_worktree`.
    """
    token = _resolved_git_token(spec, _FETCH)
    if not token:
        return _failed_fetch("GITHUB_TOKEN missing")
    unsafe = commands._unsafe_local_transport_config(spec.target_root)
    if unsafe:
        log.error(
            "refusing to fetch into %s: target_root .git/config has "
            "transport-hijacking config: %s", spec.target_root, unsafe,
        )
        return _failed_fetch(
            "unsafe transport config in target_root .git/config",
        )
    refspec = (
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}"
    )
    with _git_auth_session(spec, token) as auth_session:
        with locks._target_root_lock(spec.target_root):
            return subprocess.run(
                [
                    *commands._AUTHED_GIT_PREFIX,
                    _FETCH, "--quiet", auth_session.auth_url, refspec,
                ],
                cwd=str(spec.target_root),
                capture_output=True,
                text=True,
                env=auth_session.env,
            )


def _remote_branch_tip(
    spec: config.RepoSpec, worktree: Path, branch: str,
) -> Optional[str]:
    """Ask the REMOTE what `branch` is at, ignoring every local ref.

    For the caller that has to measure an agent's work against a base it
    cannot have moved. `refs/remotes/<remote>/<base>` looks like that base but
    is a local ref in an object store the agent's worktree shares, so an agent
    that commits code, repoints that ref at its own commit, and then commits
    the plan leaves a base-relative diff showing only the plan -- while the
    branch it would publish carries both. The remote's own answer is the one
    nothing on this host can rewrite.

    None on any failure -- a missing token, a worktree whose config could
    hijack the transport, or an unreachable remote -- and "" when the branch
    does not exist there. A caller pinning a base treats both as "no base was
    established", which is the only safe reading for a check that gates a push.

    A caller asking whether its own work is still out there has to tell them
    apart, and the discussion stage's publication does: "" is the remote saying
    that branch is not there, which is what lets a record of an unfinished
    publication finally be spent, while None established nothing and keeps it.
    Collapsing the two would drop the record of a plan on every reading that
    failed.
    """
    token = _resolved_git_token(spec, "read the remote branch tip")
    if not token:
        return None
    unsafe = commands._unsafe_local_transport_config(worktree)
    if unsafe:
        log.error(
            "refusing to read %s from the remote: worktree .git/config has "
            "transport-hijacking config: %s", branch, unsafe,
        )
        return None
    with _git_auth_session(spec, token) as auth_session:
        return _remote_branch_sha(
            auth_session, worktree, branch, f"refs/heads/{branch}", None,
        )


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
        [*commands._AUTHED_GIT_PREFIX, "ls-remote", auth_session.auth_url, ref],
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
    revision: str,
) -> bool:
    """Push one branch through an established askpass session."""
    ref = f"refs/heads/{branch}"
    remote_sha = _remote_branch_sha(
        auth_session, worktree, branch, ref, force_with_lease,
    )
    if remote_sha is None:
        return False
    push_result = subprocess.run(
        [
            *commands._AUTHED_GIT_PREFIX,
            _PUSH,
            f"--force-with-lease={ref}:{remote_sha}",
            auth_session.auth_url,
            f"{revision}:{ref}",
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
    revision: Optional[str] = None,
) -> bool:
    """Push via GIT_ASKPASS so the token never appears in argv.

    `revision`, when provided, is the exact commit to publish, and it exists
    for the caller that decided to push by INSPECTING one: the discussion
    stage reads a branch, proves it carries the agreed plan and nothing else,
    and then pushes. `HEAD` between those two moments is not necessarily the
    commit that was proven -- another tick, an operator, or a stray agent can
    move it -- and pushing whatever HEAD says would publish work no check ever
    saw while the record named the commit that passed. Naming the SHA closes
    that window in the only place it can be closed: a revision the local repo
    no longer has is refused by git rather than substituted.

    `force_with_lease`, when provided, is the SHA the caller expects the
    remote ref to be at. The push then uses
    `--force-with-lease=refs/heads/<branch>:<sha>` against that exact SHA,
    so a concurrent update to the remote rejects the push instead of being
    silently clobbered, and no `ls-remote` of our own is taken. Any caller
    that DECIDED to push by reading the remote belongs on this path: the
    squash/rewrite, which pins the pre-rewrite HEAD it approved, and the
    `discussion` stage's plan publication, which pins the tip it
    established the branch was safe to move. Pinning is what prevents the
    "out-of-band update happened in the window between the reading and the
    push" race -- a fresh `ls-remote` would treat the unexpected new remote
    SHA as the lease value and silently overwrite it, which for a
    publication being retried after a crash means overwriting whoever
    pushed to the branch in between.

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
    token = _resolved_git_token(spec, _PUSH)
    if not token:
        return False
    unsafe = commands._unsafe_local_transport_config(worktree)
    if unsafe:
        log.error(
            "refusing to push %s: worktree .git/config has "
            "transport-hijacking config: %s", branch, unsafe,
        )
        return False
    with _git_auth_session(spec, token) as auth_session:
        # An empty expected SHA means the remote ref must not exist, which
        # preserves the create-branch lease behavior.
        return _push_with_auth(
            auth_session, worktree, branch, force_with_lease, revision or _HEAD,
        )


@dataclass(frozen=True)
class _RefUpdate:
    """One lease-pinned write to a fully-qualified ref, and what it is called.

    Carried as a record rather than as four arguments because the four are one
    decision: the ref names what is being written, the refspec says whether
    that is a commit or a deletion, the lease says what the caller established
    was there first, and the name is what a refusal is reported as. A caller
    assembling three of them and forgetting the fourth would be pushing
    without a lease, which is the one thing this transport does not do.
    """

    ref: str
    refspec: str
    expected: str
    operation: str


def _remote_ref_sha(
    spec: config.RepoSpec, worktree: Path, ref: str,
) -> Optional[str]:
    """Ask the REMOTE what one fully-qualified ref resolves to.

    The read every snapshot decision is made on, and it is taken from the
    remote rather than from a local ref for the reason `_remote_branch_tip`
    is: the object store a worktree shares is writable by the agent that runs
    in it, so a local ref that looks like the snapshot proves nothing about
    what the remote actually carries.

    Three answers, and the caller has to tell them apart. A SHA is the ref as
    the remote holds it. "" is the remote saying it does not carry that ref at
    all, which is what makes an absent-is-success deletion and a create that
    may proceed possible. None established nothing -- a missing token, a
    worktree whose config could hijack the transport, an unreachable remote --
    and a caller that created or deleted on the strength of it would be acting
    on a reading nobody gave.
    """
    token = _resolved_git_token(spec, "read the remote ref")
    if not token:
        return None
    unsafe = commands._unsafe_local_transport_config(worktree)
    if unsafe:
        log.error(
            "refusing to read %s from the remote: worktree .git/config has "
            "transport-hijacking config: %s", ref, unsafe,
        )
        return None
    with _git_auth_session(spec, token) as auth_session:
        return _remote_branch_sha(auth_session, worktree, ref, ref, None)


def _push_ref(
    spec: config.RepoSpec,
    worktree: Path,
    *,
    ref: str,
    revision: str,
    expected: str,
) -> bool:
    """Publish one exact commit under one fully-qualified ref.

    `expected` is the SHA the caller established the remote ref was at, and it
    is required rather than optional: this is the transport an immutable ref
    namespace is written through, so it has no form that overwrites whatever
    happens to be there. An empty string is the lease saying the ref must not
    exist, which is how a snapshot is created; any other value is the lease
    saying it must still be exactly what the caller read.

    The revision is named rather than pushed as `HEAD`, for the reason the
    branch push takes one: what is published is a commit somebody proved, and
    HEAD between the proof and the push is not necessarily still it.
    """
    return _authed_ref_update(spec, worktree, _RefUpdate(
        ref=ref,
        refspec=f"{revision}:{ref}",
        expected=expected,
        operation=_PUSH,
    ))


def _delete_remote_ref(
    spec: config.RepoSpec, worktree: Path, *, ref: str, expected: str,
) -> bool:
    """Delete one fully-qualified ref the caller has just read.

    Pinned to what that read said, for the reason the create is: a ref
    somebody re-pointed between the read and the delete is not the ref this
    caller decided was reclaimable, and deleting it would destroy an artifact
    nobody adjudicated. A caller that found nothing there has nothing to
    delete and never reaches this.
    """
    return _authed_ref_update(spec, worktree, _RefUpdate(
        ref=ref,
        refspec=f":{ref}",
        expected=expected,
        operation="delete",
    ))


def _authed_ref_update(
    spec: config.RepoSpec, worktree: Path, update: _RefUpdate,
) -> bool:
    """Run one lease-pinned ref update under the whole transport envelope.

    The same envelope `_push_branch` runs under -- per-spec token, askpass so
    the token never reaches argv, global and system config detached, hooks,
    credential helpers, and fsmonitor disabled by `-c`, and a refusal when the
    local config carries a url rewrite or an `http.*` setting that could
    redirect the token-bearing push -- because this call carries the same token
    to the same host.

    Held under the target-root lock, which the branch push does not need and
    this does: the namespace it writes is the one a verifying fetch reads back
    into the shared clone, so a concurrent fetch of the same namespace from
    another worktree of this target root would race the update it is proving.
    """
    token = _resolved_git_token(spec, f"{update.operation} {update.ref}")
    if not token:
        return False
    unsafe = commands._unsafe_local_transport_config(worktree)
    if unsafe:
        log.error(
            "refusing to %s %s: worktree .git/config has "
            "transport-hijacking config: %s",
            update.operation, update.ref, unsafe,
        )
        return False
    with _git_auth_session(spec, token) as auth_session:
        with locks._target_root_lock(spec.target_root):
            updated = subprocess.run(
                [
                    *commands._AUTHED_GIT_PREFIX,
                    _PUSH,
                    f"--force-with-lease={update.ref}:{update.expected}",
                    auth_session.auth_url,
                    update.refspec,
                ],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                env=auth_session.env,
            )
        if updated.returncode == 0:
            return True
        log.error(
            "git %s failed for %s: %s",
            update.operation,
            update.ref,
            (updated.stderr or "").replace(auth_session.token, "***"),
        )
    return False
