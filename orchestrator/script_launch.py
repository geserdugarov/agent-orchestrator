# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Repo-root `sys.path` shim for Streamlit script-launched entry points.

`streamlit run orchestrator/dashboard.py` (and the trajectory viewer)
executes the file as a top-level script via `runpy` with no parent
package: the Streamlit launcher prepends the *script's own directory*
(`orchestrator/`) to `sys.path`, NOT the repo root. Under that layout a
`from . import ...` raises `ImportError: attempted relative import with no
known parent package` and a bare `from orchestrator import ...` fails too,
before any Streamlit code can render. Adding the repo root (the parent of
`orchestrator/`) to `sys.path` makes the absolute `orchestrator.*` imports
resolve in both the script-launched and the package-imported
(`import orchestrator.dashboard`) contexts.

The entry points select the import by launch mode, keyed on `__package__`.
A script launch (empty/absent `__package__`) uses the bare
`import script_launch`, which resolves the helper from the script's own
`orchestrator/` directory WITHOUT importing the `orchestrator` package
first -- importing the parent before the repo root is on `sys.path` would
bind it to any stale/installed `orchestrator` copy already importable. A
package import (`__package__ == "orchestrator"`) uses the qualified
`from orchestrator.script_launch import ...`, so a stray top-level
`script_launch` on `sys.path` cannot shadow it. It is import-light (stdlib
only) so pulling it in never drags the dashboard's Streamlit / Plotly
footprint into the polling tick.
"""
from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_root_on_path(script_file: str) -> None:
    """Insert the repo root (parent of `orchestrator/`) onto `sys.path`.

    `script_file` is the caller's `__file__`; the repo root is its
    grandparent. The insert is idempotent -- in the package-imported case
    the entry is already present and this is a no-op -- so calling it from
    every script-launched entry point is safe.
    """
    repo_root = Path(script_file).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
