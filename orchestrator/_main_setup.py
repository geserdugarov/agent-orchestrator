# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""CLI parsing and process-wide client/scheduler construction."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MainOptions:
    """Parsed polling-loop command-line options."""

    once: bool
    log_level: str


def parse_main_options(argv: Optional[list[str]]) -> MainOptions:
    """Parse single-tick and log-level command-line options."""
    parser = argparse.ArgumentParser(
        description="Agent orchestrator polling loop.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit.",
    )
    parser.add_argument("--log-level", default="INFO")
    parsed_options = parser.parse_args(argv)
    return MainOptions(
        once=parsed_options.once,
        log_level=parsed_options.log_level,
    )


def connect_clients() -> list[tuple[object, object]]:
    """Connect once per configured repository and ensure its labels."""
    main_module = sys.modules["orchestrator.main"]
    clients: list[tuple[object, object]] = []
    for repo_spec in main_module.config.default_repo_specs():
        github_client = main_module.GitHubClient(repo_spec=repo_spec)
        main_module.log.info("connected: repo=%s", repo_spec.slug)
        github_client.ensure_workflow_labels()
        clients.append((repo_spec, github_client))
    return clients


def create_scheduler():
    """Build the process-wide scheduler shared by every polling tick."""
    main_module = sys.modules["orchestrator.main"]
    return main_module.IssueScheduler(
        global_cap=main_module.config.MAX_PARALLEL_ISSUES_GLOBAL,
        per_repo_cap=main_module.config.MAX_PARALLEL_ISSUES_PER_REPO,
        thread_name_prefix="orch-issue",
    )


def activate_scheduler(scheduler: object) -> None:
    """Publish the scheduler so the signal handler can close submission."""
    sys.modules["orchestrator.main"].active_scheduler = scheduler
