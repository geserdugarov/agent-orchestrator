# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration loaded from .env / process environment.

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
import shlex
import sys
from pathlib import Path
from typing import NoReturn, Optional

from orchestrator import _repo_config
# The repository-entry model and REPOS parsing / default-spec construction live
# in a focused private module; re-export `RepoSpec` so `orchestrator.config`
# stays the compatibility import site for every caller and test patch target.
from orchestrator._repo_config import RepoSpec as RepoSpec

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keys whose values must never be loaded from REPO_ROOT/.env. The agent has
# read access to that file via the orchestrator checkout; secrets belong in
# process env or in a file outside REPO_ROOT.
_SECRET_KEYS = frozenset((
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
))
_DOTENV_TRUE_VALUES = frozenset(("1", "true", "on", "yes"))
# Default value for boolean env knobs that ship enabled.
_DEFAULT_ENABLED = "on"


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


def _has_matched_outer_quotes(dotenv_value: str) -> bool:
    if len(dotenv_value) < 2:
        return False
    quote = dotenv_value[0]
    if quote not in ('"', "'"):
        return False
    return dotenv_value[-1] == quote


def _strip_dotenv_quotes(dotenv_value: str) -> str:
    """Strip a single matched outer quote pair off a dotenv value.

    The legacy form (`value.strip('"').strip("'")`) stripped quote
    characters off both ends independently and across both quote types,
    which corrupted any value whose payload legitimately ended in a
    quote -- e.g. the shell-spec form
    ``codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`` would have
    its trailing `'` stripped by `.strip("'")` even though it is the
    closing half of an inner quote pair, leaving `shlex.split` to choke
    on `No closing quotation`.

    Only a single matched outer pair (`"..."` or `'...'`) is unwrapped;
    anything else is returned verbatim so quoted segments inside the
    value survive untouched.
    """
    stripped_value = dotenv_value.strip()
    if _has_matched_outer_quotes(stripped_value):
        return stripped_value[1:-1]
    return stripped_value


def _dotenv_loading_disabled() -> bool:
    setting = os.environ.get("ORCHESTRATOR_SKIP_DOTENV", "")
    return setting.strip().lower() in _DOTENV_TRUE_VALUES


def _parse_dotenv_entry(raw_line: str) -> Optional[tuple[str, str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    key, _, raw_value = line.partition("=")
    return key.strip(), _strip_dotenv_quotes(raw_value)


def _warn_ignored_dotenv_secret(key: str, env_path: Path) -> None:
    _config_warning(
        f"orchestrator: ignoring {key} in {env_path}; the implementer "
        f"agent can read this file. Move the token to "
        f"~/.config/<owner>/<repo>/token (path derived from REPO) "
        f"or export {key} before launching."
    )


def _load_dotenv_entry(raw_line: str, env_path: Path) -> None:
    entry = _parse_dotenv_entry(raw_line)
    if entry is None:
        return
    key, entry_value = entry
    if key in _SECRET_KEYS:
        _warn_ignored_dotenv_secret(key, env_path)
        return
    os.environ.setdefault(key, entry_value)


def _load_dotenv() -> None:
    if _dotenv_loading_disabled():
        return
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        _load_dotenv_entry(raw_line, env_path)


def _resolve_github_token(repo: str) -> str:
    """Resolve GITHUB_TOKEN from process env or a file outside REPO_ROOT.

    Default file path is `~/.config/<owner>/<repo>/token`, derived from REPO so
    a single host can drive multiple repos without colliding token files.
    Returns "" when neither is set; GitHubClient surfaces the actionable error.
    """
    env_val = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_val:
        return env_val
    default_path = Path.home() / ".config" / repo / "token"
    token_file = Path(os.environ.get("ORCHESTRATOR_TOKEN_FILE", str(default_path)))
    try:
        return token_file.read_text().strip()
    except FileNotFoundError:
        return ""
    except OSError as err:
        _config_warning(
            f"orchestrator: could not read token file {token_file}: {err}"
        )
        return ""


_load_dotenv()


def _parse_hitl_handles(raw: str) -> tuple[str, ...]:
    handles: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        hitl_handle = part.strip().lstrip("@").strip()
        if not hitl_handle or hitl_handle in seen:
            continue
        handles.append(hitl_handle)
        seen.add(hitl_handle)
    return tuple(handles)


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


def _agent_spec_tokens(name: str, spec: str) -> list[str]:
    """Shell-split a backend spec into tokens, aborting on an empty or
    unparseable value (the same import-time validation `_parse_agent_spec`
    relies on)."""
    raw = (spec or "").strip()
    if not raw:
        _config_error(
            f"orchestrator: {name}={spec!r} is empty; expected 'codex' "
            "or 'claude' (optionally followed by CLI args)"
        )
    try:
        tokens = shlex.split(raw)
    except ValueError as err:
        _config_error(
            f"orchestrator: {name}={spec!r} is not a valid shell-like "
            f"command spec ({err}); expected 'codex' or 'claude' "
            "(optionally followed by CLI args)"
        )
    if not tokens:
        _config_error(
            f"orchestrator: {name}={spec!r} parses to no tokens; expected "
            "'codex' or 'claude' (optionally followed by CLI args)"
        )
    return tokens


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
    tokens = _agent_spec_tokens(name, spec)
    backend = tokens[0].lower()
    if backend not in ("codex", _CLAUDE):
        _config_error(
            f"orchestrator: {name}={spec!r} first token {tokens[0]!r} is "
            "invalid; expected 'codex' or 'claude'"
        )
    return backend, tuple(tokens[1:])


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


def _parse_positive_int(name: str, raw: str, default: int) -> int:
    """Parse an env value as a positive int; abort at import on bad values.

    Used by the parallel-limit knobs (`MAX_PARALLEL_ISSUES_PER_REPO`,
    `MAX_PARALLEL_ISSUES_GLOBAL`). Empty/unset falls back to `default`;
    non-numeric or non-positive values abort startup so a typo cannot
    silently degrade the orchestrator to e.g. "process zero issues at a
    time" without surfacing the misconfiguration.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return default
    try:
        parsed = int(stripped)
    except ValueError:
        _config_error(
            f"orchestrator: {name}={raw!r} is not a valid integer; "
            "expected a positive integer (>= 1)"
        )
    if parsed < 1:
        _config_error(
            f"orchestrator: {name}={raw!r} must be >= 1 "
            "(zero or negative would block all work)"
        )
    return parsed


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


def _parse_verify_commands(raw: str) -> tuple[str, ...]:
    """Parse VERIFY_COMMANDS into an ordered tuple of shell command strings.

    Each command is a single non-empty line; ``;`` is also accepted as a
    separator so the value can fit on one line in a ``.env`` file (the
    simple ``_load_dotenv`` parser cannot represent newlines inside a
    value, mirroring how ``REPOS`` is parsed). Blank lines and lines
    starting with ``#`` are skipped. Commands are executed via the shell
    in `_run_verify_commands`, so quoting / pipes / `&&` work the way an
    operator would type them.
    """
    commands: list[str] = []
    for raw_line in raw.replace(";", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        commands.append(line)
    return tuple(commands)


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
