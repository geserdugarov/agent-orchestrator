#!/usr/bin/env bash
# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
# Self-restarting orchestrator wrapper. Exits cleanly when the orchestrator
# detects a self-modifying merge so the new code is picked up on next loop.
set -uo pipefail
cd "$(dirname "$0")"

# Ctrl+C / SIGTERM at the wrapper level must not be swallowed by the restart
# loop -- without a trap, a signal that arrives while bash is in `sleep 1` or
# `git pull` just interrupts that command and the loop relaunches the
# orchestrator anyway.
trap 'exit 130' INT
trap 'exit 143' TERM

# Read ORCHESTRATOR_BASE_BRANCH from .env so the wrapper pulls the orchestrator
# repo's own branch (REPO_ROOT) for self-update -- not BASE_BRANCH, which is
# the *target* repo's base branch and may differ (e.g. target=`master` while
# the orchestrator itself ships from `main`).
base_branch="${ORCHESTRATOR_BASE_BRANCH:-}"
if [ -z "$base_branch" ] && [ -f .env ]; then
    base_branch=$(sed -n 's/^[[:space:]]*ORCHESTRATOR_BASE_BRANCH[[:space:]]*=[[:space:]]*//p' .env \
        | head -n1 | tr -d '"' | tr -d "'")
fi
base_branch="${base_branch:-main}"

self_update() {
    # A non-base checkout or a non-fast-forward pull must never stop the wrapper:
    # under the production systemd unit (Restart=always) an exit here degrades
    # into a silent crash loop where the orchestrator never actually runs. Warn
    # loudly and keep the existing working tree -- stale-but-running is strictly
    # better than a restart loop, and the journal warning is the operator's
    # signal to fix the checkout.
    current_branch=$(git branch --show-current 2>/dev/null)
    if [ "$current_branch" != "$base_branch" ]; then
        msg="[$(date -Iseconds)] WARNING: self-update skipped -- running existing code. "
        msg+="Checked-out branch '$current_branch' is not the expected base branch '$base_branch'. "
        msg+="Restore the base checkout to resume self-update."
        echo "$msg" >&2
        return 0
    fi
    git pull --ff-only origin "$base_branch" && return 0
    rc=$?
    msg="[$(date -Iseconds)] WARNING: self-update failed -- running existing code. "
    msg+="'git pull --ff-only origin $base_branch' exited with code $rc. "
    msg+="Resolve the checkout state to resume self-update."
    echo "$msg" >&2
    return 0
}

self_update
while true; do
    .venv/bin/python -m orchestrator.main "$@"
    rc=$?
    # 130 = SIGINT, 143 = SIGTERM. The orchestrator exits with these codes
    # when it stops because of an explicit signal, which is the user's "I
    # want this to stop" -- restarting would defeat the Ctrl+C entirely.
    if [ "$rc" -eq 130 ] || [ "$rc" -eq 143 ]; then
        echo "[$(date -Iseconds)] orchestrator exited via signal (code $rc); stopping wrapper."
        exit "$rc"
    fi
    echo "[$(date -Iseconds)] orchestrator exited with code $rc; restarting in 1s..."
    sleep 1
    self_update
done
