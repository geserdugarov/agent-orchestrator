# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration package: the public settings surface over resolved env values.

This initializer is a binding / re-export surface, not a resolver. On every
import (and every reload) it invokes `environment._SettingsResolver`, which
loads the non-secret `.env`, reads each `os.environ` key, validates it, and
returns the resolved mapping; this module then binds each value as a
module-level attribute so callers and tests keep patching them on
`orchestrator.config` itself. The resolution lives in the leaves: `_dotenv`
owns the `.env` loader, `environment` the env-value parsers and the resolver,
`credentials` the token resolver, `models` the repository-config data types
(`RepoSpec`, `RepoEnvEntry`), and `repositories` the `REPOS` parsing /
default-spec construction.

Secrets are deliberately NOT loaded from REPO_ROOT/.env. The implementer agent
runs in a sibling worktree with sandbox bypass, so anything readable inside
REPO_ROOT (including .env) is recoverable by a prompt-injected agent via a
relative-path read like `cat ../agent-orchestrator/.env`. GITHUB_TOKEN is
only read from the process environment or from a token file outside REPO_ROOT
(default `~/.config/<owner>/<repo>/token` derived from REPO, override with
ORCHESTRATOR_TOKEN_FILE).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

from orchestrator.config import _dotenv, credentials, environment
# The repository-entry model lives in the config `models` leaf and the REPOS
# parsing / default-spec construction in `repositories`; re-export `RepoSpec`
# so `orchestrator.config` stays the compatibility import site for every
# caller and test patch target.
from orchestrator.config.models import RepoSpec as RepoSpec

# The public package surface: resolved settings plus the repository-config
# API. `__all__` bounds `from orchestrator.config import *`; the private
# `_config_*` / `_parse_*` / `_load_dotenv` / `_strip_dotenv_quotes` /
# `_resolve_github_token` aliases below are deliberately excluded and stay
# reachable only by the unmigrated consumers that still import them by name.
__all__ = [
    "RepoSpec",
    "default_repo_specs",
    "REPO_ROOT",
    "REPO",
    "GITHUB_TOKEN",
    "POLL_INTERVAL",
    "AGENT_TIMEOUT",
    "CLOSED_ISSUE_SWEEP_EVERY_N_TICKS",
    "SHUTDOWN_GRACE_SECONDS",
    "LOG_DIR",
    "EVENT_LOG_PATH",
    "REVIEW_TIMEOUT",
    "MAX_REVIEW_ROUNDS",
    "MAX_CONFLICT_ROUNDS",
    "MAX_RETRIES_PER_DAY",
    "DEV_SESSION_MAX_RESUMES",
    "HITL_HANDLES",
    "HITL_HANDLE",
    "HITL_MENTIONS",
    "ALLOWED_ISSUE_AUTHORS",
    "CODEX_BIN",
    "CLAUDE_BIN",
    "DEV_AGENT_SPEC",
    "DEV_AGENT",
    "DEV_AGENT_ARGS",
    "REVIEW_AGENT_SPEC",
    "REVIEW_AGENT",
    "REVIEW_AGENT_ARGS",
    "DECOMPOSE_AGENT_SPEC",
    "DECOMPOSE_AGENT",
    "DECOMPOSE_AGENT_ARGS",
    "AGENT_GIT_NAME",
    "AGENT_GIT_EMAIL",
    "TARGET_REPO_ROOT",
    "WORKTREES_DIR",
    "BASE_BRANCH",
    "REMOTE_NAME",
    "MAX_PARALLEL_ISSUES_PER_REPO",
    "MAX_PARALLEL_ISSUES_GLOBAL",
    "WORKFLOW_TRANSITION_GUARD",
    "ORCHESTRATOR_BASE_BRANCH",
    "IN_REVIEW_DEBOUNCE_SECONDS",
    "DECOMPOSE",
    "SQUASH_ON_APPROVAL",
    "EXPOSE_TRACKED_REPOS",
    "VERIFY_COMMANDS",
    "VERIFY_TIMEOUT",
]

# The orchestrator checkout itself -- two levels above this package
# (`orchestrator/config/`) -- which every other path default and the `.env`
# lookup are derived from.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_error(message: str) -> NoReturn:
    """Abort import when the configuration is invalid.

    Every invalid-config path funnels through here so a typo in the
    deployment env stops the process -- on stderr, with exit code 1 --
    before the first GitHub call. `sys.exit(str)` makes `str(exc)` the
    message, which the import-time validation tests assert on.
    """
    sys.exit(message)


def _config_warning(message: str) -> None:
    """Emit a non-fatal configuration diagnostic to stderr.

    Warnings (an ignored .env secret, an unreadable token file, a missing
    REPOS target_root) surface the problem but let the process continue,
    so they go to stderr rather than aborting like `_config_error`.
    """
    sys.stderr.write(f"{message}\n")


def _load_dotenv() -> None:
    """Load REPO_ROOT/.env into the process environment.

    A compatibility shim over the `_dotenv` leaf: the resolver loads the
    same file on every import, but reload tests still drive dotenv loading
    directly after patching `REPO_ROOT`.
    """
    _dotenv.load_dotenv(REPO_ROOT, os.environ, _config_warning)


def _parse_agent_spec(name: str, spec: str) -> tuple[str, tuple[str, ...]]:
    """Parse a shell-like backend spec into (backend, extra_args).

    A thin binding over `environment.parse_agent_spec` on the config error
    funnel. Reused at runtime by `workflow.py` to re-parse a spec persisted
    to pinned state, so a legacy bare-backend value (`"codex"` / `"claude"`)
    round-trips to `(backend, ())` and a full spec round-trips to its tokens.
    """
    return environment.parse_agent_spec(name, spec, _config_error)


_strip_dotenv_quotes = _dotenv.strip_dotenv_quotes
_resolve_github_token = credentials.resolve_github_token
_parse_verify_commands = environment.parse_verify_commands


# Resolve every setting from a `.env`-loaded process environment. Building a
# fresh resolver here (not caching a module-level default) is what preserves
# the reload / patch contract: re-importing `orchestrator.config` re-runs this
# against the current `os.environ`, and each bound value below stays an
# independently patchable module attribute.
_RESOLVED = environment._SettingsResolver(
    os.environ, REPO_ROOT, _config_error, _config_warning,
).resolve()


REPO: str = _RESOLVED["REPO"]
GITHUB_TOKEN: str = _RESOLVED["GITHUB_TOKEN"]
POLL_INTERVAL: int = _RESOLVED["POLL_INTERVAL"]
AGENT_TIMEOUT: int = _RESOLVED["AGENT_TIMEOUT"]

# How many polling ticks apart the closed-issue recovery sweep in
# `GitHubClient.list_pollable_issues` runs. That sweep issues one
# `GET /repos/.../issues?state=closed&labels=<L>` per non-terminal workflow
# label, PER REPO, every tick -- a fixed request cost that is independent of
# how much real work the repo has. Across many configured repos at a short
# `POLL_INTERVAL` it dominates per-tick request volume and is the principal
# driver of GitHub *primary* rate-limit (5000 req/hour/PAT) exhaustion: once
# the hourly budget is spent PyGithub's GithubRetry sleeps until the reset
# (~uninterruptible 1000s+ stalls observed). The latency-sensitive open-issue
# poll still runs every tick; only the closed recovery sweep is batched to
# once per N ticks. `1` (default) preserves the legacy every-tick behavior.
# Raise it (e.g. 4-5) on multi-repo deployments to stay under the hourly cap;
# the only cost is that an externally-merged/closed issue may take up to N-1
# extra ticks to finalize to `done` -- pinned GitHub state stays authoritative
# in the meantime, so nothing is lost, only briefly deferred.
CLOSED_ISSUE_SWEEP_EVERY_N_TICKS: int = _RESOLVED["CLOSED_ISSUE_SWEEP_EVERY_N_TICKS"]

# Hard ceiling, in seconds, on how long the polling loop may take to exit
# after a SIGTERM/SIGINT before it force-terminates in-flight agent
# subprocesses and hard-exits. Must stay comfortably below the systemd
# unit's `TimeoutStopSec` (90s by default) so `systemctl restart` sees a
# clean stop instead of escalating to SIGKILL. Without a bound, `main`'s
# shutdown drain waits on in-flight workers and an agent subprocess is
# capped only by `AGENT_TIMEOUT` (1800s) -- 20x the stop deadline -- so a
# restart issued while any agent was running always timed out into a brute
# kill of both `run.sh` and python.
SHUTDOWN_GRACE_SECONDS: int = _RESOLVED["SHUTDOWN_GRACE_SECONDS"]

# Persistent log location. main.py attaches a FileHandler here in addition to
# the existing stderr stream, so post-mortems don't depend on the terminal
# `run.sh` was started in. Already covered by the `*.log` .gitignore rule.
LOG_DIR: Path = _RESOLVED["LOG_DIR"]

# Optional JSONL sink for structured audit events. When set, `GitHubClient`
# (and `FakeGitHubClient`) append one JSON object per line whenever a
# handler emits an event via `gh.emit_event(...)`. Event types today:
# `stage_enter` (label transition), `agent_spawn` / `agent_exit`
# (bookending every agent invocation with role, session id, duration, and
# exit metadata), `review_verdict` (parsed reviewer decision), and
# `park_awaiting_human` (every park call site with stage + reason). Unset
# (the default) leaves the legacy behavior in place: no file is opened,
# no IO happens. Synchronous append is intentional: tick volume is low
# and ordering matters for the operator reading the file.
EVENT_LOG_PATH = _RESOLVED["EVENT_LOG_PATH"]

# Sink settings for the project-local analytics JSONL file
# (`ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`) and the libpq URL
# for the analytics Postgres service (`ANALYTICS_DB_URL`) live in
# `orchestrator.analytics`; that package owns its own parsing /
# defaulting so consumers of `config.LOG_DIR` do not pull the analytics
# defaults in transitively. The audit event log (`EVENT_LOG_PATH`)
# above stays here because `GitHubClient.emit_event` is a
# general-purpose audit surface, not analytics-specific.

REVIEW_TIMEOUT: int = _RESOLVED["REVIEW_TIMEOUT"]
MAX_REVIEW_ROUNDS: int = _RESOLVED["MAX_REVIEW_ROUNDS"]
# Cap on how many auto-conflict-resolution attempts one PR can use before
# `_handle_resolving_conflict` parks awaiting human. Mirrors the
# `MAX_REVIEW_ROUNDS` shape so a stuck rebase loop cannot burn tokens
# indefinitely.
MAX_CONFLICT_ROUNDS: int = _RESOLVED["MAX_CONFLICT_ROUNDS"]
# Cap on how many fresh implementing-codex spawns one issue can use within a
# 24h window opened at the first counted attempt. The window resets once 24h
# elapses since that start. Resumes on human reply do not count. 0 = unbounded
# (matches MAX_REVIEW_ROUNDS's implied semantics).
MAX_RETRIES_PER_DAY: int = _RESOLVED["MAX_RETRIES_PER_DAY"]
# Proactive dev-session rotation: after a single `dev_session_id` has been
# resumed this many times, retire it and start a fresh spawn from durable
# state (issue body + recent comments + the committed branch) instead of
# replaying an ever-growing transcript. Each `--resume` replays the whole
# accumulated history, so a long-lived session creeps toward the model
# context window and eventually overflows ("Prompt is too long"). This caps
# that creep before it overflows; the reactive overflow handler in
# `_resume_dev_with_text` still catches a session that blows the window in
# fewer resumes (one huge round). 0 = unbounded (resume forever, old
# behavior).
DEV_SESSION_MAX_RESUMES: int = _RESOLVED["DEV_SESSION_MAX_RESUMES"]
HITL_HANDLES: tuple[str, ...] = _RESOLVED["HITL_HANDLES"]
HITL_HANDLE: str = _RESOLVED["HITL_HANDLE"]
HITL_MENTIONS: str = _RESOLVED["HITL_MENTIONS"]
# Comma-separated GitHub logins whose unlabeled issues the orchestrator is
# willing to auto-pick-up. Empty (the default) disables the allowlist and
# preserves the legacy "anyone can trigger" behavior. Set this on a public
# repo to keep random users from spending the orchestrator's compute budget
# on useless tasks. When set this list is also the comment trust boundary
# (see `comment_trust`): comments from authors outside it stay visible on
# GitHub but are dropped from agent prompts, the `user_content_hash` drift
# signal, awaiting-human resume signals, and PR / `fixing` feedback, so an
# outsider on a public repo cannot inject workflow-driving instructions.
# On these surfaces a Bot/App login is gated like any other author, kept
# out only once the allowlist is populated; a separate `user.type ==
# "Bot"` structural check covers the drift hash and community-PR sweep.
# Pickup itself still fires only on unlabeled issues: a
# maintainer who manually labels an outsider's issue (e.g. `implementing`)
# drives it to completion.
ALLOWED_ISSUE_AUTHORS: tuple[str, ...] = _RESOLVED["ALLOWED_ISSUE_AUTHORS"]
CODEX_BIN: str = _RESOLVED["CODEX_BIN"]
CLAUDE_BIN: str = _RESOLVED["CLAUDE_BIN"]

# Default split: claude implements, codex reviews. Validated at import so a
# typo in the deployment env aborts the process before the first GitHub call.
# Each spec is shell-like: the first token names the backend (`codex` /
# `claude`), and any remaining tokens are forwarded as backend-CLI args
# (model selection, reasoning effort, etc.) on every spawn for that role.
# The `*_SPEC` constant holds the raw configured string -- workflow.py
# persists it verbatim in pinned state so a config flip mid-flight cannot
# change what backend+args run on an in-flight issue (the stored spec is
# re-parsed on every resume; current config is only consulted for fresh
# spawns). The decomposer is a separate role and is parsed even when
# DECOMPOSE=off so flipping the kill switch back on does not surface a fresh
# "that env var was always invalid" failure.
DEV_AGENT_SPEC: str = _RESOLVED["DEV_AGENT_SPEC"]
DEV_AGENT: str = _RESOLVED["DEV_AGENT"]
DEV_AGENT_ARGS: tuple[str, ...] = _RESOLVED["DEV_AGENT_ARGS"]
REVIEW_AGENT_SPEC: str = _RESOLVED["REVIEW_AGENT_SPEC"]
REVIEW_AGENT: str = _RESOLVED["REVIEW_AGENT"]
REVIEW_AGENT_ARGS: tuple[str, ...] = _RESOLVED["REVIEW_AGENT_ARGS"]
DECOMPOSE_AGENT_SPEC: str = _RESOLVED["DECOMPOSE_AGENT_SPEC"]
DECOMPOSE_AGENT: str = _RESOLVED["DECOMPOSE_AGENT"]
DECOMPOSE_AGENT_ARGS: tuple[str, ...] = _RESOLVED["DECOMPOSE_AGENT_ARGS"]

# git identity injected into each agent spawn via GIT_AUTHOR_*/GIT_COMMITTER_*
# env vars (see agents._agent_env). Env vars take precedence over user.name
# and user.email from any config scope, so agent commits are attributable to
# the orchestrator without touching the host's git config or the shared repo
# config. The default email uses the GitHub-recognized noreply form so it
# won't bounce and won't link to a real user account.
AGENT_GIT_NAME: str = _RESOLVED["AGENT_GIT_NAME"]
AGENT_GIT_EMAIL: str = _RESOLVED["AGENT_GIT_EMAIL"]

# The repository whose issues / PRs this orchestrator manages. Defaults to
# REPO_ROOT (self-bootstrap: orchestrator manages its own repo). Override when
# the orchestrator code is installed in one clone but drives PRs into another.
# Worktrees are `git worktree add`-ed from this path, so commits land on its
# git history -- not the orchestrator's own.
TARGET_REPO_ROOT: Path = _RESOLVED["TARGET_REPO_ROOT"]

WORKTREES_DIR: Path = _RESOLVED["WORKTREES_DIR"]

# Base branch in the *target* repo: where worktrees branch from and where PRs
# are opened against.
BASE_BRANCH: str = _RESOLVED["BASE_BRANCH"]

# Name of the git remote in `TARGET_REPO_ROOT` that points at REPO on GitHub.
# Defaults to `origin`; override when the local clone uses several remotes
# (e.g. a public `origin` and a private fork named `private`) and the
# orchestrator should drive the non-default one. Ignored when `REPOS` is set
# -- the per-entry fourth field on each `REPOS` row takes precedence there.
REMOTE_NAME: str = _RESOLVED["REMOTE_NAME"]

# Per-repo cap on how many issues the orchestrator may advance in parallel
# within one repo on a single tick. Default 1 keeps the legacy "one issue
# at a time per repo" behavior. Each `REPOS` entry can override this via
# its optional fifth pipe-separated field.
MAX_PARALLEL_ISSUES_PER_REPO: int = _RESOLVED["MAX_PARALLEL_ISSUES_PER_REPO"]
# Global cap across all configured repos. Default 3 limits concurrent
# spawn fan-out when several `REPOS` entries are configured, regardless
# of the per-repo cap each one declares. Set higher only on hosts with
# the CPU / memory headroom to run that many agent CLIs at once.
MAX_PARALLEL_ISSUES_GLOBAL: int = _RESOLVED["MAX_PARALLEL_ISSUES_GLOBAL"]

# One of off / warn / enforce, governing only the transition-*legality* check
# in `set_workflow_label` (the typo guard is always strict). Default `warn`
# keeps production safe while the declared transition table soaks against live
# issues; flip to `enforce` once the warn logs are clean. An invalid value
# aborts at import so a typo can't silently disable the guard.
WORKFLOW_TRANSITION_GUARD: str = _RESOLVED["WORKFLOW_TRANSITION_GUARD"]

_REPO_SPECS: list[RepoSpec] = _RESOLVED["REPO_SPECS"]


def default_repo_specs() -> list[RepoSpec]:
    """The configured RepoSpecs (validated at import).

    A single element built from `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`
    when `REPOS` is unset (so existing single-repo deployments keep working
    unchanged); otherwise one element per `REPOS` entry. Returns a fresh
    list copy so callers cannot mutate the cached result.
    """
    return list(_REPO_SPECS)


# Base branch of the orchestrator's *own* repo (REPO_ROOT). Used only by the
# self-update path: `_self_modifying_merge_happened` watches `origin/<this>`
# for new commits under `orchestrator/`, and `run.sh` fast-forwards to it on
# every restart. Decoupled from BASE_BRANCH so the target repo can have a
# different default branch (e.g. `master`) without breaking self-update.
ORCHESTRATOR_BASE_BRANCH: str = _RESOLVED["ORCHESTRATOR_BASE_BRANCH"]

# Quiet window after the most recent PR/issue comment before resuming the dev
# session in `in_review`.
IN_REVIEW_DEBOUNCE_SECONDS: int = _RESOLVED["IN_REVIEW_DEBOUNCE_SECONDS"]

# Kill switch for the entire `decomposing` stage. off -> revert to the
# legacy "no label -> implementing" pickup, no children, no manifest. The
# rollout safety valve so the user can disable decomposition if manifest
# output proves unreliable, without redeploying old binaries.
DECOMPOSE: bool = _RESOLVED["DECOMPOSE"]

# After the reviewer agent emits VERDICT: APPROVED, squash the dev's commits
# on the PR branch into a single conventional-commit-shaped commit and
# force-push (with lease). Default on -- a one-commit PR is what humans
# expect on merge. Off restores the legacy "leave the dev's commit history
# as-is" behavior; useful if a workflow downstream (changelog generation,
# bisect tooling) depends on the per-step commit history.
SQUASH_ON_APPROVAL: bool = _RESOLVED["SQUASH_ON_APPROVAL"]

# Whether working agents are told about the *other* repos this orchestrator
# tracks (slug, local `target_root`, base branch) for cross-repo reference.
# Default on, but inert for single-repo hosts: the context builder gates on
# `len(specs) > 1`, so a default single-repo deployment sees zero added prompt
# tokens and zero behavior change. Off forces the disclosure off globally --
# the operator escape hatch for a security-conscious multi-repo host. The
# disclosed data is operator-configured and non-secret (no tokens, no remote
# URLs), and write-containment is unchanged, so default-on-when-multi-repo is
# the right posture; the kill switch keeps it reversible.
EXPOSE_TRACKED_REPOS: bool = _RESOLVED["EXPOSE_TRACKED_REPOS"]

# Local verification commands run in the per-issue worktree on
# VERDICT: APPROVED, before the issue is labeled `in_review`. Default
# empty -- no verification, preserving legacy behavior. Commands run
# sequentially via the shell with a bounded `VERIFY_TIMEOUT`; on a
# non-zero exit, a timeout, or a dirty worktree left behind, the issue
# is parked in `validating` with the failing command, exit/timeout, and
# a redacted/truncated tail of the output. GitHub CI still runs against
# the PR; the human merging it reads CI's verdict.
VERIFY_COMMANDS: tuple[str, ...] = _RESOLVED["VERIFY_COMMANDS"]
# Per-command wall-clock cap in seconds. Each command in VERIFY_COMMANDS
# is run with this timeout; a single slow command parks the issue rather
# than burning the orchestrator's tick budget.
VERIFY_TIMEOUT: int = _RESOLVED["VERIFY_TIMEOUT"]
