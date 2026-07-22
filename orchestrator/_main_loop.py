# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop lifecycle and cooperative scheduler drain."""
from __future__ import annotations

import contextlib
import sys
import time
from typing import Iterator, Optional

_MAIN_MODULE = "orchestrator.main"


def wait_for_next_tick() -> None:
    """Sleep interruptibly until the next configured polling interval."""
    main_module = sys.modules[_MAIN_MODULE]
    for _ in range(main_module.config.POLL_INTERVAL):
        if not main_module.running:
            return
        time.sleep(1)


def run_polling_loop(
    clients: list[tuple[object, object]],
    scheduler: object,
) -> Optional[int]:
    """Poll until signaled or a self-modifying merge requests restart."""
    main_module = sys.modules[_MAIN_MODULE]
    own_sha = main_module._own_head_sha()
    main_module.log.info("own HEAD=%s", own_sha)
    while main_module.running:
        if own_sha and main_module._self_modifying_merge_happened(own_sha):
            main_module.log.info(
                "self-modifying merge detected; exiting for restart",
            )
            return 0
        main_module._run_tick(clients, scheduler)
        main_module._wait_for_next_tick()
    return None


def drive_main_loop(
    options: object,
    clients: list[tuple[object, object]],
    scheduler: object,
) -> Optional[int]:
    """Choose one-shot or recurring polling from parsed options."""
    main_module = sys.modules[_MAIN_MODULE]
    if options.once:
        main_module._run_tick(clients, scheduler)
        return None
    return main_module._run_polling_loop(clients, scheduler)


def drain_scheduler(scheduler: object) -> None:
    """Stop child groups when signaled, then wait for every worker."""
    main_module = sys.modules[_MAIN_MODULE]
    if main_module.received_signal is not None:
        main_module.agents.terminate_all_running()
    scheduler.shutdown(wait=True)
    main_module.active_scheduler = None
    main_module._shutdown_complete.set()


def signal_exit_code() -> int:
    """Return a shell-style signal exit code or zero."""
    main_module = sys.modules[_MAIN_MODULE]
    if main_module.received_signal is not None:
        return main_module._SIGNAL_EXIT_BASE + main_module.received_signal
    return 0


@contextlib.contextmanager
def scheduler_drained(scheduler: object) -> Iterator[None]:
    """Guarantee scheduler drain after the wrapped main-loop body."""
    try:
        yield
    finally:
        sys.modules[_MAIN_MODULE]._drain_scheduler(scheduler)


def run_main(argv: Optional[list[str]] = None) -> int:
    """Configure dependencies, drive polling, and return the exit reason."""
    main_module = sys.modules[_MAIN_MODULE]
    options = main_module._parse_main_options(argv)
    main_module._configure_logging(options.log_level)
    main_module.signal.signal(main_module.signal.SIGTERM, main_module._shutdown)
    main_module.signal.signal(main_module.signal.SIGINT, main_module._shutdown)
    clients = main_module._connect_clients()
    scheduler = main_module._create_scheduler()
    main_module._activate_scheduler(scheduler)
    restart_exit_code: Optional[int] = None
    with main_module._scheduler_drained(scheduler):
        restart_exit_code = main_module._drive_main_loop(
            options,
            clients,
            scheduler,
        )
    if restart_exit_code is not None:
        return restart_exit_code
    return main_module._signal_exit_code()
