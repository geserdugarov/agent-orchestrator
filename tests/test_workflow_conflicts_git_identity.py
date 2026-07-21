# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow


class GitHardenedInjectsIdentityTest(unittest.TestCase):
    """`_git_hardened` strips global/system git config (where `user.name`
    / `user.email` typically live), so without explicit `GIT_AUTHOR_*` /
    `GIT_COMMITTER_*` env vars a `git rebase` that needs to replay commits
    can fail with "Committer identity unknown" and park the issue as a
    non-conflict failure rather than resolving.
    """

    def test_env_has_committer_and_author_identity(self) -> None:
        subprocess_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        )

        with patch("subprocess.run", subprocess_run):
            workflow._git_hardened("rebase", "x", cwd=Path("/tmp"))

        env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(env.get("GIT_AUTHOR_NAME"), config.AGENT_GIT_NAME)
        self.assertEqual(env.get("GIT_AUTHOR_EMAIL"), config.AGENT_GIT_EMAIL)
        self.assertEqual(env.get("GIT_COMMITTER_NAME"), config.AGENT_GIT_NAME)
        self.assertEqual(env.get("GIT_COMMITTER_EMAIL"), config.AGENT_GIT_EMAIL)
        # Hardening still applied: global/system config blocked.
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), os.devnull)


if __name__ == "__main__":
    unittest.main()
