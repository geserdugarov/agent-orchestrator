# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration loaded from .env / process environment.

Every knob is resolved and validated here as the package is imported, so a
reload re-runs the whole assembly and callers and tests keep patching each
value on `orchestrator.config` itself. The non-secret `.env` parser lives in
`environment` and the token resolver in `credentials`.

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

from orchestrator import _agent_config, _repo_config, _runtime_config
from orchestrator.config import credentials, environment
# The repository-entry model and REPOS parsing / default-spec construction live
# in a focused private module; re-export `RepoSpec` so `orchestrator.config`
# stays the compatibility import site for every caller and test patch target.
from orchestrator._repo_config import RepoSpec as RepoSpec

# The orchestrator checkout itself -- two levels above this package
# (`orchestrator/config/`) -- which every other path default and the `.env`
# lookup are derived from.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Keys whose values must never be loaded from REPO_ROOT/.env. The agent has
# read access to that file via the orchestrator checkout; secrets belong in
# process env or in a file outside REPO_ROOT.
# Default value for boolean env knobs that ship enabled.
_DEFAULT_ENABLED = "on"
_DOTENV_TRUE_VALUES = environment._TRUE_VALUES


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
    environment.load_dotenv(REPO_ROOT, os.environ, _config_warning)


_strip_dotenv_quotes = environment.strip_dotenv_quotes
_resolve_github_token = credentials.resolve_github_token


_load_dotenv()


_parse_hitl_handles = _runtime_config.parse_hitl_handles


REPO: str = os.environ.get("REPO", "geserdugarov/agent-orchestrator")
GITHUB_TOKEN: str = _resolve_github_token(REPO)
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
AGENT_TIMEOUT: int = int(os.environ.get("AGENT_TIMEOUT", "1800"))

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
CLOSED_ISSUE_SWEEP_EVERY_N_TICKS: int = max(
    1, int(os.environ.get("CLOSED_ISSUE_SWEEP_EVERY_N_TICKS", "1"))
)

# Hard ceiling, in seconds, on how long the polling loop may take to exit
# after a SIGTERM/SIGINT before it force-terminates in-flight agent
# subprocesses and hard-exits. Must stay comfortably below the systemd
# unit's `TimeoutStopSec` (90s by default) so `systemctl restart` sees a
# clean stop instead of escalating to SIGKILL. Without a bound, `main`'s
# shutdown drain waits on in-flight workers and an agent subprocess is
# capped only by `AGENT_TIMEOUT` (1800s) -- 20x the stop deadline -- so a
# restart issued while any agent was running always timed out into a brute
# kill of both `run.sh` and python.
SHUTDOWN_GRACE_SECONDS: int = max(
    1, int(os.environ.get("SHUTDOWN_GRACE_SECONDS", "30"))
)

# Persistent log location. main.py attaches a FileHandler here in addition to
# the existing stderr stream, so post-mortems don't depend on the terminal
# `run.sh` was started in. Already covered by the `*.log` .gitignore rule.
LOG_DIR: Path = Path(os.environ.get("LOG_DIR", str(REPO_ROOT / "logs")))

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
_EVENT_LOG_PATH_RAW: str = os.environ.get("EVENT_LOG_PATH", "").strip()
EVENT_LOG_PATH = Path(_EVENT_LOG_PATH_RAW) if _EVENT_LOG_PATH_RAW else None

# Sink settings for the project-local analytics JSONL file
# (`ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`) and the libpq URL
# for the analytics Postgres service (`ANALYTICS_DB_URL`) live in
# `orchestrator.analytics`; that package owns its own parsing /
# defaulting so consumers of `config.LOG_DIR` do not pull the analytics
# defaults in transitively. The audit event log (`EVENT_LOG_PATH`)
# above stays here because `GitHubClient.emit_event` is a
# general-purpose audit surface, not analytics-specific.

REVIEW_TIMEOUT: int = int(os.environ.get("REVIEW_TIMEOUT", str(AGENT_TIMEOUT)))
MAX_REVIEW_ROUNDS: int = int(os.environ.get("MAX_REVIEW_ROUNDS", "3"))
# Cap on how many auto-conflict-resolution attempts one PR can use before
# `_handle_resolving_conflict` parks awaiting human. Mirrors the
# `MAX_REVIEW_ROUNDS` shape so a stuck rebase loop cannot burn tokens
# indefinitely.
MAX_CONFLICT_ROUNDS: int = int(os.environ.get("MAX_CONFLICT_ROUNDS", "3"))
# Cap on how many fresh implementing-codex spawns one issue can use within a
# 24h window opened at the first counted attempt. The window resets once 24h
# elapses since that start. Resumes on human reply do not count. 0 = unbounded
# (matches MAX_REVIEW_ROUNDS's implied semantics).
MAX_RETRIES_PER_DAY: int = int(os.environ.get("MAX_RETRIES_PER_DAY", "3"))
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
DEV_SESSION_MAX_RESUMES: int = int(os.environ.get("DEV_SESSION_MAX_RESUMES", "10"))
HITL_HANDLES: tuple[str, ...] = (
    _parse_hitl_handles(os.environ.get("HITL_HANDLE", "geserdugarov"))
    or ("geserdugarov",)
)
HITL_HANDLE: str = ",".join(HITL_HANDLES)
HITL_MENTIONS: str = " ".join(f"@{hitl_handle}" for hitl_handle in HITL_HANDLES)
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
ALLOWED_ISSUE_AUTHORS: tuple[str, ...] = _parse_hitl_handles(
    os.environ.get("ALLOWED_ISSUE_AUTHORS", "")
)
_CLAUDE = "claude"
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", _CLAUDE)


def _parse_agent_spec(name: str, spec: str) -> tuple[str, tuple[str, ...]]:
    """Parse a shell-like backend spec into (backend, extra_args).

    Accepts a bare backend (`claude`) or a backend with backend-CLI args
    (`codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`). Tokens are
    split with `shlex` so quoting works the same way an operator would
    type the command in a shell. The first token must be `codex` or
    `claude`; anything else aborts at import so a typo cannot silently
    fall back to a default backend on next restart.

    The same parser is reused at runtime by `workflow.py` to re-parse a
    spec that was previously persisted to pinned state, so a legacy bare-
    backend value (`"codex"` / `"claude"`) round-trips cleanly to
    `(backend, ())` and a full spec with args round-trips to its tokens.
    """
    return _agent_config.parse_agent_spec(name, spec, _config_error)


# Default split: claude implements, codex reviews. Validated at import so a
# typo in the deployment env aborts the process before the first GitHub call.
# Each spec is shell-like: the first token names the backend (`codex` /
# `claude`), and any remaining tokens are forwarded as backend-CLI args
# (model selection, reasoning effort, etc.) on every spawn for that role.
# The `*_SPEC` constant holds the raw configured string -- workflow.py
# persists it verbatim in pinned state so a config flip mid-flight cannot
# change what backend+args run on an in-flight issue (the stored spec is
# re-parsed on every resume; current config is only consulted for fresh
# spawns).
DEV_AGENT_SPEC: str = os.environ.get("DEV_AGENT", _CLAUDE)
DEV_AGENT, DEV_AGENT_ARGS = _parse_agent_spec("DEV_AGENT", DEV_AGENT_SPEC)
REVIEW_AGENT_SPEC: str = os.environ.get("REVIEW_AGENT", "codex")
REVIEW_AGENT, REVIEW_AGENT_ARGS = _parse_agent_spec(
    "REVIEW_AGENT", REVIEW_AGENT_SPEC
)
# Decomposer is a separate role from implementing/reviewing -- it reads the
# issue and produces a structured manifest. Parsed at import time even when
# DECOMPOSE=off so flipping the kill switch back on does not introduce a
# fresh "that env var was always invalid" failure.
DECOMPOSE_AGENT_SPEC: str = os.environ.get("DECOMPOSE_AGENT", _CLAUDE)
DECOMPOSE_AGENT, DECOMPOSE_AGENT_ARGS = _parse_agent_spec(
    "DECOMPOSE_AGENT", DECOMPOSE_AGENT_SPEC
)

# git identity injected into each agent spawn via GIT_AUTHOR_*/GIT_COMMITTER_*
# env vars (see agents._agent_env). Env vars take precedence over user.name
# and user.email from any config scope, so agent commits are attributable to
# the orchestrator without touching the host's git config or the shared repo
# config. The default email uses the GitHub-recognized noreply form so it
# won't bounce and won't link to a real user account.
AGENT_GIT_NAME: str = os.environ.get("AGENT_GIT_NAME", "agent-orchestrator")
AGENT_GIT_EMAIL: str = os.environ.get(
    "AGENT_GIT_EMAIL", "agent-orchestrator@users.noreply.github.com"
)

# The repository whose issues / PRs this orchestrator manages. Defaults to
# REPO_ROOT (self-bootstrap: orchestrator manages its own repo). Override when
# the orchestrator code is installed in one clone but drives PRs into another.
# Worktrees are `git worktree add`-ed from this path, so commits land on its
# git history -- not the orchestrator's own.
TARGET_REPO_ROOT: Path = Path(
    os.environ.get("TARGET_REPO_ROOT", str(REPO_ROOT))
)

WORKTREES_DIR: Path = Path(
    os.environ.get("WORKTREES_DIR", str(TARGET_REPO_ROOT.parent / "wt-orchestrator"))
)

# Base branch in the *target* repo: where worktrees branch from and where PRs
# are opened against.
BASE_BRANCH: str = os.environ.get("BASE_BRANCH", "main")

# Name of the git remote in `TARGET_REPO_ROOT` that points at REPO on GitHub.
# Defaults to `origin`; override when the local clone uses several remotes
# (e.g. a public `origin` and a private fork named `private`) and the
# orchestrator should drive the non-default one. Ignored when `REPOS` is set
# -- the per-entry fourth field on each `REPOS` row takes precedence there.
REMOTE_NAME: str = os.environ.get("REMOTE_NAME", "origin")


_parse_positive_int = _runtime_config.PositiveIntParser(_config_error)


# Per-repo cap on how many issues the orchestrator may advance in parallel
# within one repo on a single tick. Default 1 keeps the legacy "one issue
# at a time per repo" behavior. Each `REPOS` entry can override this via
# its optional fifth pipe-separated field.
MAX_PARALLEL_ISSUES_PER_REPO: int = _parse_positive_int(
    "MAX_PARALLEL_ISSUES_PER_REPO",
    os.environ.get("MAX_PARALLEL_ISSUES_PER_REPO", ""),
    1,
)
# Global cap across all configured repos. Default 3 limits concurrent
# spawn fan-out when several `REPOS` entries are configured, regardless
# of the per-repo cap each one declares. Set higher only on hosts with
# the CPU / memory headroom to run that many agent CLIs at once.
MAX_PARALLEL_ISSUES_GLOBAL: int = _parse_positive_int(
    "MAX_PARALLEL_ISSUES_GLOBAL",
    os.environ.get("MAX_PARALLEL_ISSUES_GLOBAL", ""),
    3,
)


def _parse_transition_guard(raw: str) -> str:
    """Parse `WORKFLOW_TRANSITION_GUARD`: one of off / warn / enforce.

    Governs only the transition-*legality* check in `set_workflow_label`
    (the typo guard is always strict). Default `warn` keeps production
    safe while the declared transition table soaks against live issues;
    flip to `enforce` once the warn logs are clean. An invalid value
    aborts at import so a typo can't silently disable the guard.
    """
    mode = (raw or "").strip().lower() or "warn"
    if mode not in ("off", "warn", "enforce"):
        _config_error(
            f"orchestrator: WORKFLOW_TRANSITION_GUARD={raw!r} is invalid; "
            "expected one of: off, warn, enforce"
        )
    return mode


WORKFLOW_TRANSITION_GUARD: str = _parse_transition_guard(
    os.environ.get("WORKFLOW_TRANSITION_GUARD", "")
)


def _parse_repos_env(raw: str) -> list[RepoSpec]:
    """Parse the REPOS env value into a list of RepoSpecs.

    Narrow compatibility wrapper: the entry parsing, owner/name and option
    validation, ordering, duplicate detection, and per-repo parallel-limit
    defaulting live in `_repo_config.parse_repos_env`; this shim injects this
    module's `MAX_PARALLEL_ISSUES_PER_REPO` default and the abort / warn
    diagnostics. Kept on `orchestrator.config` so existing callers and test
    patch targets resolve `config._parse_repos_env` unchanged.
    """
    return _repo_config.parse_repos_env(
        raw,
        default_parallel_limit=MAX_PARALLEL_ISSUES_PER_REPO,
        config_error=_config_error,
        config_warning=_config_warning,
    )


_REPO_SPECS: list[RepoSpec] = _repo_config.build_repo_specs(
    os.environ.get("REPOS", ""),
    default_spec=RepoSpec(
        slug=REPO,
        target_root=TARGET_REPO_ROOT,
        base_branch=BASE_BRANCH,
        remote_name=REMOTE_NAME,
        parallel_limit=MAX_PARALLEL_ISSUES_PER_REPO,
    ),
    config_error=_config_error,
    config_warning=_config_warning,
)


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
ORCHESTRATOR_BASE_BRANCH: str = os.environ.get("ORCHESTRATOR_BASE_BRANCH", "main")

# Quiet window after the most recent PR/issue comment before resuming the dev
# session in `in_review`.
IN_REVIEW_DEBOUNCE_SECONDS: int = int(
    os.environ.get("IN_REVIEW_DEBOUNCE_SECONDS", "600")
)

# Kill switch for the entire `decomposing` stage. off -> revert to the
# legacy "no label -> implementing" pickup, no children, no manifest. The
# rollout safety valve so the user can disable decomposition if manifest
# output proves unreliable, without redeploying old binaries.
DECOMPOSE: bool = (
    os.environ.get("DECOMPOSE", _DEFAULT_ENABLED).strip().lower() in _DOTENV_TRUE_VALUES
)

# After the reviewer agent emits VERDICT: APPROVED, squash the dev's commits
# on the PR branch into a single conventional-commit-shaped commit and
# force-push (with lease). Default on -- a one-commit PR is what humans
# expect on merge. Off restores the legacy "leave the dev's commit history
# as-is" behavior; useful if a workflow downstream (changelog generation,
# bisect tooling) depends on the per-step commit history.
SQUASH_ON_APPROVAL: bool = os.environ.get(
    "SQUASH_ON_APPROVAL", _DEFAULT_ENABLED
).strip().lower() in _DOTENV_TRUE_VALUES

# Whether working agents are told about the *other* repos this orchestrator
# tracks (slug, local `target_root`, base branch) for cross-repo reference.
# Default on, but inert for single-repo hosts: the context builder gates on
# `len(specs) > 1`, so a default single-repo deployment sees zero added prompt
# tokens and zero behavior change. Off forces the disclosure off globally --
# the operator escape hatch for a security-conscious multi-repo host. The
# disclosed data is operator-configured and non-secret (no tokens, no remote
# URLs), and write-containment is unchanged, so default-on-when-multi-repo is
# the right posture; the kill switch keeps it reversible.
EXPOSE_TRACKED_REPOS: bool = os.environ.get(
    "EXPOSE_TRACKED_REPOS", _DEFAULT_ENABLED
).strip().lower() in _DOTENV_TRUE_VALUES


_parse_verify_commands = _runtime_config.parse_verify_commands


# Local verification commands run in the per-issue worktree on
# VERDICT: APPROVED, before the issue is labeled `in_review`. Default
# empty -- no verification, preserving legacy behavior. Commands run
# sequentially via the shell with a bounded `VERIFY_TIMEOUT`; on a
# non-zero exit, a timeout, or a dirty worktree left behind, the issue
# is parked in `validating` with the failing command, exit/timeout, and
# a redacted/truncated tail of the output. GitHub CI still runs against
# the PR; the human merging it reads CI's verdict.
VERIFY_COMMANDS: tuple[str, ...] = _parse_verify_commands(
    os.environ.get("VERIFY_COMMANDS", "")
)
# Per-command wall-clock cap in seconds. Each command in VERIFY_COMMANDS
# is run with this timeout; a single slow command parks the issue rather
# than burning the orchestrator's tick budget.
VERIFY_TIMEOUT: int = int(os.environ.get("VERIFY_TIMEOUT", "600"))
