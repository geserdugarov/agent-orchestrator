# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow usage."""
from __future__ import annotations

from orchestrator import workflow as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
UsageMetrics = _owner.UsageMetrics


def _accumulate_issue_usage(
    state: PinnedState, usage: Optional[UsageMetrics]
) -> None:
    """Fold one agent run's parsed usage into the per-issue running totals.

    Called by the developer (implementing) and reviewer (validating) run
    sites right after `_run_agent_tracked` returns, mutating the SAME
    `PinnedState` the handler persists later -- never a second writer. The
    runner deliberately does not write pinned state itself, so an
    `interrupted` run whose handler returns without `write_pinned_state`
    (the shutdown-sweep contract) simply never persists these counters: a
    slight, accepted undercount on killed runs, with the analytics sink
    still holding ground truth.

    Keys folded (all new to the pinned-state schema):
      * ``issue_agent_runs``     -- +1 per real agent exit.
      * ``issue_total_tokens``   -- input + output + cache-read + cache-write.
        codex's ``cached_tokens`` is intentionally excluded: it is the
        portion of ``input_tokens`` already served from cache, so summing it
        would double-count part of the input.
      * ``issue_total_cost_usd`` -- sum of each run's ``cost_usd``; ``None``
        costs (``no-usage`` / ``unknown-price``) contribute nothing.
      * ``issue_cost_sources``   -- sorted distinct ``cost_source`` tags seen.
        The minimal aggregate a terminal verdict needs to mark ``(est.)``
        (any ``estimated``) or an unpriced ``unknown`` (any ``unknown-price``)
        without re-reading the analytics sink.

    A ``None`` usage -- the fail-open case where the parse itself failed --
    is a no-op: with no parsed metrics there is nothing to fold and the run
    is not counted.
    """
    if usage is None:
        return

    agent_runs = int(state.get("issue_agent_runs") or 0)
    state.set("issue_agent_runs", agent_runs + 1)

    tokens = sum((
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    ))
    state.set(
        "issue_total_tokens",
        int(state.get("issue_total_tokens") or 0) + tokens,
    )

    if usage.cost_usd is not None:
        state.set(
            "issue_total_cost_usd",
            float(state.get("issue_total_cost_usd") or 0) + usage.cost_usd,
        )

    prior_sources = state.get("issue_cost_sources")
    seen = set(prior_sources) if isinstance(prior_sources, list) else set()
    seen.add(usage.cost_source)
    state.set("issue_cost_sources", sorted(seen))


def _format_issue_usage_verdict(state: PinnedState) -> Optional[str]:
    """Render the cumulative per-issue usage verdict for a terminal surface.

    Reads the counters `_accumulate_issue_usage` folds onto pinned state and
    returns a single visible line:

        :receipt: this issue: 3 agent runs · 45,200 tokens · $0.87

    The cost slot follows `issue_cost_sources`: `(est.)` is appended when any
    run's cost was `estimated` from the price table, and the whole figure
    collapses to `unknown` when any `unknown-price` run leaves the priced
    total incomplete (that dominates -- an unknown total cannot also be an
    estimate). A `no-usage` run contributes nothing and marks neither.

    Returns None when no agent run was ever counted (`issue_agent_runs` is
    0 / absent) so a terminal with nothing to report skips the line instead
    of posting a zero receipt.
    """
    runs = int(state.get("issue_agent_runs") or 0)
    if runs <= 0:
        return None
    tokens = int(state.get("issue_total_tokens") or 0)
    prior_sources = state.get("issue_cost_sources")
    sources = set(prior_sources) if isinstance(prior_sources, list) else set()
    if "unknown-price" in sources:
        cost = "unknown"
    else:
        cost = f"${float(state.get('issue_total_cost_usd') or 0):.2f}"
        if "estimated" in sources:
            cost = f"{cost} (est.)"
    return (
        f":receipt: this issue: {runs} agent runs · "
        f"{tokens:,} tokens · {cost}"
    )


def _post_issue_usage_verdict(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> None:
    """Post the terminal usage verdict as its own tracked issue comment.

    Thin wrapper over `_format_issue_usage_verdict` + `_post_issue_comment`
    for the PR merged / rejected finalizers, which otherwise post no comment
    of their own. Must run BEFORE the finalizer's `write_pinned_state` so the
    comment id lands in the same persisted state and a later drift/watermark
    tick recognizes it as orchestrator-authored. A no-op when there is
    nothing to report (no counted agent run).
    """
    verdict = _owner._format_issue_usage_verdict(state)
    if verdict:
        _owner._post_issue_comment(gh, issue, state, verdict)
