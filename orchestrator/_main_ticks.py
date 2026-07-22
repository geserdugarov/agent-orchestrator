# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Single-repository and cross-repository polling-tick execution."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

_MAIN_MODULE = "orchestrator.main"


def _main_module():
    return sys.modules[_MAIN_MODULE]


def tick_one_repo(spec: object, github_client: object, scheduler: object) -> None:
    """Drive one repository tick while isolating shutdown and failures."""
    main_module = _main_module()
    if not main_module.running:
        main_module.log.info(
            "repo=%s shutdown requested before tick start; skipping",
            spec.slug,
        )
        return
    main_module.log.info("tick: repo=%s", spec.slug)
    try:
        main_module.workflow.tick(
            github_client,
            spec,
            scheduler=scheduler,
        )
    except Exception:
        main_module.log.exception(
            "tick failed for repo=%s; continuing",
            spec.slug,
        )


def fan_out_repo_ticks(
    clients: list[tuple[object, object]],
    scheduler: object,
) -> None:
    """Run configured repository ticks concurrently."""
    with ThreadPoolExecutor(
        max_workers=len(clients),
        thread_name_prefix="orch-repo",
    ) as executor:
        future_repos = {
            executor.submit(tick_one_repo, spec, client, scheduler): spec.slug
            for spec, client in clients
        }
        for future in as_completed(future_repos):
            try:
                future.result()
            except Exception:
                _main_module().log.exception(
                    "repo=%s tick worker raised unexpectedly",
                    future_repos[future],
                )


def run_tick(
    clients: list[tuple[object, object]],
    scheduler: object,
) -> None:
    """Drive one polling pass and its completion/retention drains."""
    if not clients:
        return
    if len(clients) == 1:
        spec, github_client = clients[0]
        tick_one_repo(spec, github_client, scheduler)
    else:
        fan_out_repo_ticks(clients, scheduler)
    scheduler.reap()
    _main_module().analytics.prune_with_retention_logging()
