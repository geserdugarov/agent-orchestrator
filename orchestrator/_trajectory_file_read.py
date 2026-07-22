# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory JSONL file decoding, ordering, and error handling."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from orchestrator._trajectory_run_model import TrajectoryRun


log = logging.getLogger("orchestrator.trajectory_reader")
RecordParser = Callable[..., Optional[TrajectoryRun]]


def parse_trajectory_line(
    line: str,
    *,
    sequence: int,
    parser: RecordParser,
) -> Optional[TrajectoryRun]:
    if not line.strip():
        return None
    try:
        record_object = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parser(record_object, seq=sequence)


def read_trajectory_file(
    path: Path,
    parser: RecordParser,
) -> list[TrajectoryRun]:
    runs: list[TrajectoryRun] = []
    with path.open("r", encoding="utf-8") as trajectory_file:
        for sequence, line in enumerate(trajectory_file):
            run = parse_trajectory_line(line, sequence=sequence, parser=parser)
            if run is not None:
                runs.append(run)
    return runs


def read_trajectories(
    log_path: Optional[Path],
    parser: RecordParser,
) -> list[TrajectoryRun]:
    if log_path is None:
        return []
    try:
        runs = read_trajectory_file(log_path, parser)
    except FileNotFoundError:
        return []
    except OSError as error:
        log.warning("could not read trajectory log %s: %s", log_path, error)
        return []
    runs.sort(key=run_sort_key, reverse=True)
    return runs


def run_sort_key(run: TrajectoryRun) -> tuple[str, int]:
    return run.ts, run.seq
