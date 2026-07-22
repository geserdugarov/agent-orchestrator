# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Static compatibility inventory for the polling entry point."""
from __future__ import annotations

from orchestrator import (
    _main_logging,
    _main_loop,
    _main_self_update,
    _main_setup,
    _main_shutdown,
    _main_ticks,
)

shutdown = _main_shutdown.shutdown
arm_shutdown_watchdog = _main_shutdown.arm_shutdown_watchdog
shutdown_terminate_grace = _main_shutdown.shutdown_terminate_grace
run_shutdown_watchdog = _main_shutdown.run_shutdown_watchdog
force_exit = _main_shutdown.force_exit
rotating_file_handler = _main_logging.rotating_file_handler
configure_logging = _main_logging.configure_logging
git = _main_self_update.git
own_head_sha = _main_self_update.own_head_sha
self_modifying_merge_happened = _main_self_update.self_modifying_merge_happened
MainOptions = _main_setup.MainOptions
parse_main_options = _main_setup.parse_main_options
connect_clients = _main_setup.connect_clients
create_scheduler = _main_setup.create_scheduler
activate_scheduler = _main_setup.activate_scheduler
wait_for_next_tick = _main_loop.wait_for_next_tick
run_polling_loop = _main_loop.run_polling_loop
drive_main_loop = _main_loop.drive_main_loop
drain_scheduler = _main_loop.drain_scheduler
signal_exit_code = _main_loop.signal_exit_code
scheduler_drained = _main_loop.scheduler_drained
tick_one_repo = _main_ticks.tick_one_repo
fan_out_repo_ticks = _main_ticks.fan_out_repo_ticks
run_tick = _main_ticks.run_tick
main = _main_loop.run_main
