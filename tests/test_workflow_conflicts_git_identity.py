# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow


class GitHardenedInjectsIdentityTest(unittest.TestCase):
    """`_git_hardened` strips global/system git config (where `user.name`
    / `user.email` typically live), so without explicit `GIT_AUTHOR_*` /
    `GIT_COMMITTER_*` env vars a `git rebase` that needs to replay commits
    can fail with "Committer identity unknown" and park the issue as a
    non-conflict failure rather than resolving.
    """

    def test_env_includes_committer_and_author_identity(self) -> None:
        from unittest.mock import patch as mock_patch

        captured: dict[str, dict] = {}

        def fake_run(args, *, cwd, capture_output, text, env):
            captured["env"] = env
            from unittest.mock import MagicMock
            return MagicMock(returncode=0, stdout="", stderr="")

        with mock_patch("subprocess.run", side_effect=fake_run):
            workflow._git_hardened("rebase", "x", cwd=Path("/tmp"))

        env = captured["env"]
        self.assertEqual(env.get("GIT_AUTHOR_NAME"), config.AGENT_GIT_NAME)
        self.assertEqual(env.get("GIT_AUTHOR_EMAIL"), config.AGENT_GIT_EMAIL)
        self.assertEqual(env.get("GIT_COMMITTER_NAME"), config.AGENT_GIT_NAME)
        self.assertEqual(env.get("GIT_COMMITTER_EMAIL"), config.AGENT_GIT_EMAIL)
        # Hardening still applied: global/system config blocked.
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), os.devnull)


if __name__ == "__main__":
    unittest.main()
