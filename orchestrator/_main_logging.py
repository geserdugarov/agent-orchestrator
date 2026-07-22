# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-process logging configuration."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler


def rotating_file_handler() -> logging.Handler:
    """Build the rotating file handler after creating the log directory."""
    config = sys.modules["orchestrator.main"].config
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        config.LOG_DIR / "orchestrator.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )


def configure_logging(level: str) -> None:
    """Configure stderr plus best-effort rotating file logging."""
    config = sys.modules["orchestrator.main"].config
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_handlers.append(rotating_file_handler())
    except OSError as error:
        logging.basicConfig(
            level=level,
            format=log_format,
            handlers=log_handlers,
        )
        logging.getLogger("orchestrator").warning(
            "file logging disabled: %s (%s)",
            config.LOG_DIR,
            error,
        )
        return
    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=log_handlers,
    )
