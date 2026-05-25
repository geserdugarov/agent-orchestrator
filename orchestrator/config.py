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
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keys whose values must never be loaded from REPO_ROOT/.env. The agent has
# read access to that file via the orchestrator checkout; secrets belong in
# process env or in a file outside REPO_ROOT.
_SECRET_KEYS = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
})


def _strip_dotenv_quotes(value: str) -> str:
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
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def _load_dotenv() -> None:
    if os.environ.get("ORCHESTRATOR_SKIP_DOTENV", "").strip().lower() in (
        "1", "true", "on", "yes",
    ):
        return
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _strip_dotenv_quotes(value)
        if key in _SECRET_KEYS:
            print(
                f"orchestrator: ignoring {key} in {env_path}; the implementer "
                f"agent can read this file. Move the token to "
                f"~/.config/<owner>/<repo>/token (path derived from REPO) "
                f"or export {key} before launching.",
                file=sys.stderr,
            )
            continue
        os.environ.setdefault(key, value)


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
    except OSError as e:
        print(
            f"orchestrator: could not read token file {token_file}: {e}",
            file=sys.stderr,
        )
        return ""


_load_dotenv()


def _parse_hitl_handles(raw: str) -> tuple[str, ...]:
    handles: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        handle = part.strip().lstrip("@").strip()
        if not handle or handle in seen:
            continue
        handles.append(handle)
        seen.add(handle)
    return tuple(handles)

REPO: str = os.environ.get("REPO", "geserdugarov/agent-orchestrator")
GITHUB_TOKEN: str = _resolve_github_token(REPO)
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
AGENT_TIMEOUT: int = int(os.environ.get("AGENT_TIMEOUT", "1800"))

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

# Project-local analytics sink. Distinct from EVENT_LOG_PATH (the audit
# event log emitted through `GitHubClient.emit_event`): the analytics
# sink is a foundation layer for future aggregation / reporting work and
# is opted in / out independently. `orchestrator/analytics.py` appends
# one JSON object per line `{ts, repo, issue, event, optional stage,
# ...}` and `prune_old_records` removes records older than
# `ANALYTICS_RETENTION_DAYS`.
#
# Default path lives under `LOG_DIR` so the sink writes inside the
# project's existing log area (already covered by the `logs/`
# .gitignore rule). Set `ANALYTICS_LOG_PATH=` (empty) or to one of
# `off` / `disabled` / `none` to disable writes entirely -- in that
# mode `append_record` and `prune_old_records` are silent no-ops and
# no file is opened. Pruning never touches the pinned GitHub state on
# any issue; the analytics file is local-filesystem observability only.
_ANALYTICS_LOG_PATH_RAW = os.environ.get("ANALYTICS_LOG_PATH")
if _ANALYTICS_LOG_PATH_RAW is None:
    ANALYTICS_LOG_PATH = LOG_DIR / "analytics.jsonl"
else:
    _stripped_analytics = _ANALYTICS_LOG_PATH_RAW.strip()
    if not _stripped_analytics or _stripped_analytics.lower() in (
        "off", "disabled", "none",
    ):
        ANALYTICS_LOG_PATH = None
    else:
        ANALYTICS_LOG_PATH = Path(_stripped_analytics)

# Retention window for `ANALYTICS_LOG_PATH` in days. Records whose `ts`
# is older than this window are removed by
# `analytics.prune_old_records(...)`. Default 90 days. 0 (or any
# non-positive value) keeps raw data indefinitely -- the prune helper
# becomes a no-op so operators can opt out of cleanup without disabling
# the sink itself.
ANALYTICS_RETENTION_DAYS: int = int(
    os.environ.get("ANALYTICS_RETENTION_DAYS", "90")
)
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
HITL_HANDLES: tuple[str, ...] = (
    _parse_hitl_handles(os.environ.get("HITL_HANDLE", "geserdugarov"))
    or ("geserdugarov",)
)
HITL_HANDLE: str = ",".join(HITL_HANDLES)
HITL_MENTIONS: str = " ".join(f"@{handle}" for handle in HITL_HANDLES)
# Comma-separated GitHub logins whose unlabeled issues the orchestrator is
# willing to auto-pick-up. Empty (the default) disables the allowlist and
# preserves the legacy "anyone can trigger" behavior. Set this on a public
# repo to keep random users from spending the orchestrator's compute budget
# on useless tasks. The guard only fires at pickup; if a maintainer manually
# labels an outsider's issue (e.g. `implementing`) the workflow still drives
# it to completion, so this is purely a triage filter.
ALLOWED_ISSUE_AUTHORS: tuple[str, ...] = _parse_hitl_handles(
    os.environ.get("ALLOWED_ISSUE_AUTHORS", "")
)
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")


def _parse_agent_spec(name: str, value: str) -> tuple[str, tuple[str, ...]]:
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
    raw = (value or "").strip()
    if not raw:
        raise SystemExit(
            f"orchestrator: {name}={value!r} is empty; expected 'codex' "
            "or 'claude' (optionally followed by CLI args)"
        )
    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        raise SystemExit(
            f"orchestrator: {name}={value!r} is not a valid shell-like "
            f"command spec ({e}); expected 'codex' or 'claude' "
            "(optionally followed by CLI args)"
        )
    if not tokens:
        raise SystemExit(
            f"orchestrator: {name}={value!r} parses to no tokens; expected "
            "'codex' or 'claude' (optionally followed by CLI args)"
        )
    backend = tokens[0].lower()
    if backend not in ("codex", "claude"):
        raise SystemExit(
            f"orchestrator: {name}={value!r} first token {tokens[0]!r} is "
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
DEV_AGENT_SPEC: str = os.environ.get("DEV_AGENT", "claude")
DEV_AGENT, DEV_AGENT_ARGS = _parse_agent_spec("DEV_AGENT", DEV_AGENT_SPEC)
REVIEW_AGENT_SPEC: str = os.environ.get("REVIEW_AGENT", "codex")
REVIEW_AGENT, REVIEW_AGENT_ARGS = _parse_agent_spec(
    "REVIEW_AGENT", REVIEW_AGENT_SPEC
)
# Decomposer is a separate role from implementing/reviewing -- it reads the
# issue and produces a structured manifest. Parsed at import time even when
# DECOMPOSE=off so flipping the kill switch back on does not introduce a
# fresh "that env var was always invalid" failure.
DECOMPOSE_AGENT_SPEC: str = os.environ.get("DECOMPOSE_AGENT", "claude")
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
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise SystemExit(
            f"orchestrator: {name}={raw!r} is not a valid integer; "
            "expected a positive integer (>= 1)"
        )
    if parsed < 1:
        raise SystemExit(
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


@dataclass(frozen=True)
class RepoSpec:
    """Per-repo identity threaded through the workflow.

    Replaces the global `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` reads
    inside workflow.py so a future multi-repo loop can drive several repos
    from one orchestrator process without touching module-level state.

    `remote_name` is the name of the git remote in `target_root` that points
    at this repo on GitHub. Defaults to `origin`; override when the local
    clone uses several remotes (e.g. a public `origin` and a private fork
    under a different remote name) and the orchestrator should drive the
    non-default one.

    `parallel_limit` caps how many issues this repo may advance in parallel
    on a single tick. Defaults to 1 (legacy one-at-a-time behavior); each
    `REPOS` entry can override it via the optional fifth pipe-separated
    field. The global `MAX_PARALLEL_ISSUES_GLOBAL` ceiling still applies
    across all repos regardless of any one repo's `parallel_limit`.
    """

    slug: str
    target_root: Path
    base_branch: str
    remote_name: str = "origin"
    parallel_limit: int = 1


def _parse_repos_env(raw: str) -> list[RepoSpec]:
    """Parse the REPOS env value into a list of RepoSpecs.

    Format: one entry per line,
    ``owner/name|target_root|base_branch[|remote_name[|parallel_limit]]``.
    The fourth (``remote_name``, defaults to ``origin``) and fifth
    (``parallel_limit``, defaults to ``MAX_PARALLEL_ISSUES_PER_REPO``)
    fields are optional. The fifth field is positional, so overriding
    ``parallel_limit`` requires also writing the ``remote_name`` (use
    ``origin`` explicitly to keep the default).
    Blank lines and lines starting with ``#`` are skipped. ``;`` is also
    accepted as an entry separator so the value fits on a single line in a
    ``.env`` file (the simple parser in `_load_dotenv` cannot represent
    multi-line values). Aborts (SystemExit) on malformed entries or
    duplicate slugs; a missing ``target_root`` is warned to stderr but not
    fatal so a freshly-cloned host can still start the orchestrator and
    notice the problem on the first tick rather than at import.
    """
    specs: list[RepoSpec] = []
    seen: set[str] = set()
    # ';' accepted in addition to '\n' so the value can be one line in .env.
    for entry_no, raw_line in enumerate(
        raw.replace(";", "\n").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) not in (3, 4, 5):
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} is malformed "
                f"(expected 'owner/name|target_root|base_branch' "
                f"with optional '|remote_name' and '|parallel_limit'): "
                f"{line!r}"
            )
        parallel_limit_raw: str | None = None
        if len(parts) == 3:
            slug, target_root, base_branch = (p.strip() for p in parts)
            remote_name = "origin"
        elif len(parts) == 4:
            slug, target_root, base_branch, remote_name = (
                p.strip() for p in parts
            )
            if not remote_name:
                raise SystemExit(
                    f"orchestrator: REPOS entry #{entry_no} has empty "
                    "remote_name (omit the trailing '|' to default to "
                    "'origin')"
                )
        else:
            (
                slug,
                target_root,
                base_branch,
                remote_name,
                parallel_limit_raw,
            ) = (p.strip() for p in parts)
            if not remote_name:
                raise SystemExit(
                    f"orchestrator: REPOS entry #{entry_no} has empty "
                    "remote_name (omit the trailing '|' to default to "
                    "'origin')"
                )
        # Require exactly two non-empty components separated by a single
        # '/'. A bare substring check would accept 'owner//repo' (empty
        # owner or repo) and 'owner/repo/extra' (extra path segment).
        slug_components = slug.split("/")
        if len(slug_components) != 2 or not all(slug_components):
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has invalid "
                f"owner/name {slug!r}; expected exactly 'owner/name' "
                "with non-empty owner and name"
            )
        if not target_root:
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has empty target_root"
            )
        if not base_branch:
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has empty base_branch"
            )
        if slug in seen:
            raise SystemExit(
                f"orchestrator: REPOS lists duplicate slug {slug!r}; "
                "each repo can appear only once"
            )
        seen.add(slug)
        if parallel_limit_raw is None:
            parallel_limit = MAX_PARALLEL_ISSUES_PER_REPO
        elif not parallel_limit_raw:
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has empty "
                "parallel_limit (omit the trailing '|' to default to "
                f"MAX_PARALLEL_ISSUES_PER_REPO={MAX_PARALLEL_ISSUES_PER_REPO})"
            )
        else:
            try:
                parallel_limit = int(parallel_limit_raw)
            except ValueError:
                raise SystemExit(
                    f"orchestrator: REPOS entry #{entry_no} parallel_limit "
                    f"{parallel_limit_raw!r} is not a valid integer; expected "
                    "a positive integer (>= 1)"
                )
            if parallel_limit < 1:
                raise SystemExit(
                    f"orchestrator: REPOS entry #{entry_no} parallel_limit "
                    f"{parallel_limit_raw!r} must be >= 1 (zero or negative "
                    "would block all work for this repo)"
                )
        target_path = Path(target_root)
        if not target_path.exists():
            print(
                f"orchestrator: REPOS entry {slug!r} target_root "
                f"{target_path} does not exist; worktree creation will fail",
                file=sys.stderr,
            )
        specs.append(
            RepoSpec(
                slug=slug,
                target_root=target_path,
                base_branch=base_branch,
                remote_name=remote_name,
                parallel_limit=parallel_limit,
            )
        )
    if not specs:
        raise SystemExit(
            "orchestrator: REPOS is set but contains no valid entries; "
            "either unset it or provide at least one "
            "'owner/name|target_root|base_branch' entry"
        )
    return specs


_REPOS_RAW: str = os.environ.get("REPOS", "")
_REPO_SPECS: list[RepoSpec] = (
    _parse_repos_env(_REPOS_RAW)
    if _REPOS_RAW.strip()
    else [
        RepoSpec(
            slug=REPO,
            target_root=TARGET_REPO_ROOT,
            base_branch=BASE_BRANCH,
            remote_name=REMOTE_NAME,
            parallel_limit=MAX_PARALLEL_ISSUES_PER_REPO,
        )
    ]
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

# When `in_review` and the reviewer (and any branch protections GitHub knows
# about) are happy, the orchestrator can merge the PR itself. Default off so
# the legacy "humans merge" behavior keeps working until users opt in.
AUTO_MERGE: bool = os.environ.get("AUTO_MERGE", "off").strip().lower() in (
    "1", "true", "on", "yes",
)
# Quiet window after the most recent PR/issue comment before resuming the dev
# session in `in_review`.
IN_REVIEW_DEBOUNCE_SECONDS: int = int(
    os.environ.get("IN_REVIEW_DEBOUNCE_SECONDS", "600")
)

# Kill switch for the entire `decomposing` stage. off -> revert to the
# legacy "no label -> implementing" pickup, no children, no manifest. The
# rollout safety valve so the user can disable decomposition if manifest
# output proves unreliable, without redeploying old binaries.
DECOMPOSE: bool = os.environ.get("DECOMPOSE", "on").strip().lower() in (
    "1", "true", "on", "yes",
)

# After the reviewer agent emits VERDICT: APPROVED, squash the dev's commits
# on the PR branch into a single conventional-commit-shaped commit and
# force-push (with lease). Default on -- a one-commit PR is what humans
# expect on merge. Off restores the legacy "leave the dev's commit history
# as-is" behavior; useful if a workflow downstream (changelog generation,
# bisect tooling) depends on the per-step commit history.
SQUASH_ON_APPROVAL: bool = os.environ.get(
    "SQUASH_ON_APPROVAL", "on"
).strip().lower() in ("1", "true", "on", "yes")


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
# a redacted/truncated tail of the output. CI is still the later
# auto-merge gate.
VERIFY_COMMANDS: tuple[str, ...] = _parse_verify_commands(
    os.environ.get("VERIFY_COMMANDS", "")
)
# Per-command wall-clock cap in seconds. Each command in VERIFY_COMMANDS
# is run with this timeout; a single slow command parks the issue rather
# than burning the orchestrator's tick budget.
VERIFY_TIMEOUT: int = int(os.environ.get("VERIFY_TIMEOUT", "600"))
