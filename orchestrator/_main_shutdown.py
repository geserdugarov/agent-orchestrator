# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Signal handling, watchdog timing, and forced-exit cleanup."""
from __future__ import annotations

import contextlib
import sys
import threading
from types import ModuleType
from typing import Any


def _main_module() -> ModuleType:
    return sys.modules["orchestrator.main"]


class ForcedExit:
    """Ensure the watchdog exits even when process-group cleanup raises."""

    def __init__(self, main_module: ModuleType, exit_code: int) -> None:
        self._main_module = main_module
        self._exit_code = exit_code

    def __enter__(self) -> "ForcedExit":
        return self

    def __exit__(self, *error_details: Any) -> bool:
        self._main_module.os._exit(self._exit_code)
        return False


def shutdown(signum: int, _frame: object) -> None:
    """Close submission on the first signal and arm bounded shutdown."""
    main_module = _main_module()
    if main_module.received_signal is not None:
        return
    main_module.received_signal = signum
    main_module.log.info(
        "signal %s received; will stop after this tick",
        signum,
    )
    main_module.running = False
    scheduler = main_module.active_scheduler
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            main_module.log.exception(
                "signal handler scheduler.shutdown(wait=False) failed",
            )
    main_module._arm_shutdown_watchdog(signum)
    with contextlib.suppress(OSError, ValueError):
        main_module.signal.signal(signum, main_module.signal.SIG_DFL)


def arm_shutdown_watchdog(signum: int) -> None:
    """Start the daemon watchdog that force-exits an overlong drain."""
    threading.Thread(
        target=_main_module()._run_shutdown_watchdog,
        args=(signum,),
        name="shutdown-watchdog",
        daemon=True,
    ).start()


def shutdown_terminate_grace() -> float:
    """Return the shutdown budget reserved for process-group termination."""
    main_module = _main_module()
    return min(
        main_module._TERMINATE_SWEEP_RESERVE_CAP_SECONDS,
        main_module.config.SHUTDOWN_GRACE_SECONDS / 2,
    )


def run_shutdown_watchdog(signum: int) -> None:
    """Wait for cooperative drain, then invoke the forced-exit path."""
    main_module = _main_module()
    drain_budget = max(
        0,
        main_module.config.SHUTDOWN_GRACE_SECONDS
        - main_module._shutdown_terminate_grace(),
    )
    if main_module._shutdown_complete.wait(timeout=drain_budget):
        return
    main_module._force_exit(signum)


def force_exit(signum: int) -> None:
    """Terminate live child groups and hard-exit with the signal code."""
    main_module = _main_module()
    main_module.log.warning(
        "shutdown grace (%ss) expired; terminating agents and forcing exit",
        main_module.config.SHUTDOWN_GRACE_SECONDS,
    )
    exit_code = main_module._SIGNAL_EXIT_BASE + signum
    with ForcedExit(main_module, exit_code):
        main_module.agents.terminate_all_running(
            grace=main_module._shutdown_terminate_grace(),
        )
