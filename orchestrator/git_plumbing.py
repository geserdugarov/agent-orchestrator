# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Hardened git subprocess layer shared by worktree and stage code.

Owns the primitives that every direct shell-out to `git` is routed
through:

* `_GIT_NO_PROMPT_ENV` -- env-var injection that disables git's
  `/dev/tty` fallback prompt in any subprocess we spawn.
* `_target_root_lock` and the module-level `_TARGET_ROOT_LOCKS` /
  `_TARGET_ROOT_LOCKS_LOCK` it manages -- per-target_root re-entrant
  locks that serialize concurrent writes against the same parent
  clone's `.git/config`, `.git/refs`, and `.git/packed-refs`.
* `_git` -- thin `subprocess.run` wrapper that drops the
  no-prompt env var on every invocation.
* `_git_hardened` -- `_git` plus the agent-hostile-environment
  hardening (no hooks / no fsmonitor / no credential helper /
  detached global+system config / explicit AGENT_GIT identity).
* `_authed_fetch` / `_authed_target_fetch` -- authenticated `git fetch`
  variants that deliver the GitHub PAT via a tempfile askpass so the
  token never appears on argv, with the same hardening envelope as
  `_push_branch`.
* `_push_branch` -- the only authed push path, which delivers the PAT
  via askpass and pre-flights against worktree-local url-rewrite
  rules.

The squash helper stays in `worktrees.py`; the local-verify runner
and its worktree-state probes (`VerifyResult`, `_run_verify_commands`,
`_truncate_verify_output`, `_head_sha`, `_worktree_dirty_files`) live
in `verify.py`; the per-tick base refresh and rebase routing
(`_rebase_base_into_worktree`, `_merge_base_into_worktree`,
`_rebase_in_progress`, `_refresh_base_and_worktrees`,
`_PR_REFRESH_DETOUR_LABELS`, `_sync_worktree_with_base`,
`_route_pr_worktree_to_resolving_conflict`) lives in `base_sync.py`.
All three modules' names -- the ones defined above, the verify-module
set, and the base-sync set -- are re-exported from `worktrees.py`
under their original names so existing imports and
`patch.object(worktrees, "_foo", ...)` test patches keep working.
The leading underscore convention is preserved because these helpers
remain module-internal contracts -- the public surface is the stage
handlers in `orchestrator/stages/` driven by `workflow.py`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from . import config
from .config import RepoSpec

log = logging.getLogger(__name__)

# Disable git's /dev/tty fallback prompts in any subprocess we spawn.
_GIT_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


# Per-target_root locks that serialize git plumbing against the parent
# clone. `git worktree add` / `worktree remove` / `branch -D` / authenticated
# `fetch` all write to the parent repo's `.git/config` and grab its
# `.git/config.lock`. Two `workflow.tick` worker threads driving
# `_ensure_worktree` against the same `spec.target_root` can race that
# lock file and surface as `error: could not lock config file .git/config:
# File exists`, failing the worker before the agent even spawns. The
# long-running agent work itself runs in the per-issue worktree (with its
# own per-worktree config under `<git-dir>/worktrees/<name>/`) so we
# release the lock as soon as the plumbing finishes -- agents stay
# concurrent, only the parent-repo writes are serialized.
#
# Locks are keyed by the string form of `spec.target_root` so two
# `RepoSpec` instances pointing at the same on-disk clone share a lock
# (the case `_run_tick` produces when one Python process drives several
# RepoSpec entries that happen to point at the same target_root for
# operator convenience). `_TARGET_ROOT_LOCKS_LOCK` only guards the dict
# lookup/insert; the per-key lock is acquired outside that guard so a
# slow git operation can't block lookup for other repos.
#
# Per-key locks are `RLock` so a caller that already holds the lock can
# re-enter via a helper that also acquires it -- specifically,
# `_authed_target_fetch` acquires the lock internally to keep its
# critical section honest in isolation, and the worktree creators
# (`_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`)
# also hold the lock for the whole add sequence. Cross-thread serialization
# is unchanged.
_TARGET_ROOT_LOCKS_LOCK = threading.Lock()
_TARGET_ROOT_LOCKS: dict[str, threading.RLock] = {}


def _target_root_lock(target_root: Path) -> threading.RLock:
    """Return the lock that serializes git plumbing against `target_root`.

    Created lazily on first use so a single-repo deployment never pays
    for a lock it doesn't need. The dict is process-global; clearing it
    is a test-only concern handled inline (no public API). Re-entrant
    so a caller already holding the lock can call into a helper that
    also acquires it.
    """
    key = str(target_root)
    with _TARGET_ROOT_LOCKS_LOCK:
        lock = _TARGET_ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TARGET_ROOT_LOCKS[key] = lock
        return lock


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
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
    git_prefix = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "credential.helper=",
        "-c", "core.fsmonitor=",
        "-c", "commit.gpgsign=false",
        "-c", "rebase.autoStash=false",
    ]
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
        [*git_prefix, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def _authed_fetch(
    spec: RepoSpec, refspec: str, *, cwd: Path
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
        attacker-controlled host.

    The auth URL carries only the username (`x-access-token`); the
    token itself is read from $GIT_TOKEN by a tempfile askpass script
    so it never appears in argv. Global/system git config is detached
    via `GIT_CONFIG_GLOBAL=/dev/null` / `GIT_CONFIG_SYSTEM=/dev/null`
    so url-rewrite rules planted there cannot apply. We also refuse
    to run if the worktree's local config already carries any url
    rewrite rule, mirroring `_push_branch`'s pre-flight check.

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
    token = config._resolve_github_token(spec.slug)
    if not token:
        log.error("GITHUB_TOKEN missing for %s; cannot fetch", spec.slug)
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="GITHUB_TOKEN missing",
        )
    rewrite = subprocess.run(
        ["git", "config", "--local", "--get-regexp",
         r"^url\..*\.(insteadof|pushinsteadof)$"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to fetch into %s: worktree .git/config has url "
            "rewrite rules: %s", cwd, rewrite.stdout.strip(),
        )
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="url rewrite rules in worktree .git/config",
        )
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": token,
            "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
            "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
            "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
            "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        git_prefix = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.fsmonitor=",
        ]
        with _target_root_lock(spec.target_root):
            return subprocess.run(
                [*git_prefix, "fetch", "--quiet", auth_url, refspec],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=env,
            )


def _authed_target_fetch(
    spec: RepoSpec, branch: str
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
    `url.<host>.(insteadOf|pushInsteadOf)` rule that could redirect
    the token-bearing fetch to an attacker-controlled host.

    Serialized via `_target_root_lock` (`RLock` so a caller already
    holding it -- the worktree creators -- re-enters cleanly) for the
    same `.git/config.lock` reason described on `_ensure_worktree`.
    """
    token = config._resolve_github_token(spec.slug)
    if not token:
        log.error("GITHUB_TOKEN missing for %s; cannot fetch", spec.slug)
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="GITHUB_TOKEN missing",
        )
    rewrite = subprocess.run(
        ["git", "config", "--local", "--get-regexp",
         r"^url\..*\.(insteadof|pushinsteadof)$"],
        cwd=str(spec.target_root), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to fetch into %s: target_root .git/config has url "
            "rewrite rules: %s", spec.target_root, rewrite.stdout.strip(),
        )
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="url rewrite rules in target_root .git/config",
        )
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
    refspec = (
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}"
    )
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": token,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        git_prefix = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.fsmonitor=",
        ]
        with _target_root_lock(spec.target_root):
            return subprocess.run(
                [*git_prefix, "fetch", "--quiet", auth_url, refspec],
                cwd=str(spec.target_root),
                capture_output=True,
                text=True,
                env=env,
            )


def _push_branch(
    spec: RepoSpec, worktree: Path, branch: str,
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
    `orchestrator/issue-<n>` namespace, but a self-restart between commit
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
    hook, credential helper, fsmonitor, or url-rewrite rule cannot observe
    GIT_TOKEN or redirect the push to an attacker-controlled host:
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
        insteadOf/pushInsteadOf rewrite, since those rewrite our auth URL
        and would deliver the token to whatever host the agent picked.
    """
    # Resolve the token from `spec.slug` rather than the cached
    # `config.GITHUB_TOKEN` (which was looked up once for `config.REPO`),
    # so a multi-repo deployment with one token file per slug under
    # `~/.config/<owner>/<repo>/token` pushes with the right repo's token.
    # Single-repo deployments see identical behavior because
    # `_resolve_github_token(REPO)` returns the same value.
    token = config._resolve_github_token(spec.slug)
    if not token:
        log.error("GITHUB_TOKEN missing for %s; cannot push", spec.slug)
        return False
    rewrite = subprocess.run(
        ["git", "config", "--local", "--get-regexp",
         r"^url\..*\.(insteadof|pushinsteadof)$"],
        cwd=str(worktree), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to push %s: worktree .git/config has url rewrite rules: %s",
            branch, rewrite.stdout.strip(),
        )
        return False
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": token,
            # Detach from any agent-writable global/system git config; the
            # only config that applies is the local worktree config (already
            # checked above) plus our explicit -c overrides below.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        git_prefix = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.fsmonitor=",
        ]
        ref = f"refs/heads/{branch}"
        if force_with_lease is not None:
            remote_sha = force_with_lease
        else:
            ls = subprocess.run(
                [*git_prefix, "ls-remote", auth_url, ref],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                env=env,
            )
            if ls.returncode != 0:
                scrubbed = (ls.stderr or "").replace(token, "***")
                log.error("git ls-remote failed for %s: %s", branch, scrubbed)
                return False
            remote_sha = ""
            for line in (ls.stdout or "").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == ref:
                    remote_sha = parts[0]
                    break
        # An empty <expected> in --force-with-lease means "expect the ref to
        # not exist", which is the right lease for the create-branch case.
        r = subprocess.run(
            [
                *git_prefix,
                "push",
                f"--force-with-lease={ref}:{remote_sha}",
                auth_url, f"HEAD:{ref}",
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=env,
        )
    if r.returncode != 0:
        # Scrub the token out of any error output before logging.
        scrubbed = (r.stderr or "").replace(token, "***")
        log.error("git push failed for %s: %s", branch, scrubbed)
        return False
    return True
