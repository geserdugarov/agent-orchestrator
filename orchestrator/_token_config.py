# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""GitHub token lookup outside the repository checkout."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_github_token(repo_slug: str) -> str:
    """Resolve a token from process env or the per-repository token file."""
    environment_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if environment_token:
        return environment_token
    default_path = Path.home() / ".config" / repo_slug / "token"
    token_file = Path(
        os.environ.get("ORCHESTRATOR_TOKEN_FILE", str(default_path)),
    )
    try:
        return token_file.read_text().strip()
    except FileNotFoundError:
        return ""
    except OSError as error:
        sys.stderr.write(
            f"orchestrator: could not read token file {token_file}: {error}\n",
        )
        return ""
