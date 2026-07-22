# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop entry point and stable runtime patch surface.

CLI/setup, shutdown, self-update probes, per-repository ticks, logging, and
loop lifecycle live in focused leaves. Their compatibility aliases remain on
this module so signal handling and tests resolve every collaborator at call
time from the same process-wide façade.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

from orchestrator import _main_api, _main_dependencies

log = logging.getLogger("orchestrator")

os = _main_dependencies.os
signal = _main_dependencies.signal
agents = _main_dependencies.agents
analytics = _main_dependencies.analytics
config = _main_dependencies.current_config()
workflow = _main_dependencies.workflow
GitHubClient = _main_dependencies.GitHubClient
IssueScheduler = _main_dependencies.IssueScheduler
_MainOptions = _main_api.MainOptions
_shutdown = _main_api.shutdown
_arm_shutdown_watchdog = _main_api.arm_shutdown_watchdog
_shutdown_terminate_grace = _main_api.shutdown_terminate_grace
_run_shutdown_watchdog = _main_api.run_shutdown_watchdog
_force_exit = _main_api.force_exit
_rotating_file_handler = _main_api.rotating_file_handler
_configure_logging = _main_api.configure_logging
_git = _main_api.git
_own_head_sha = _main_api.own_head_sha
_self_modifying_merge_happened = _main_api.self_modifying_merge_happened
_parse_main_options = _main_api.parse_main_options
_connect_clients = _main_api.connect_clients
_create_scheduler = _main_api.create_scheduler
_activate_scheduler = _main_api.activate_scheduler
_wait_for_next_tick = _main_api.wait_for_next_tick
_run_polling_loop = _main_api.run_polling_loop
_drive_main_loop = _main_api.drive_main_loop
_drain_scheduler = _main_api.drain_scheduler
_signal_exit_code = _main_api.signal_exit_code
_scheduler_drained = _main_api.scheduler_drained
_tick_one_repo = _main_api.tick_one_repo
_fan_out_repo_ticks = _main_api.fan_out_repo_ticks
_run_tick = _main_api.run_tick
main = _main_api.main

running = True
received_signal: Optional[int] = None
active_scheduler: Optional[IssueScheduler] = None
_shutdown_complete = threading.Event()
_TERMINATE_SWEEP_RESERVE_CAP_SECONDS = 5.0
_SIGNAL_EXIT_BASE = 128


if __name__ == "__main__":
    sys.exit(main())
