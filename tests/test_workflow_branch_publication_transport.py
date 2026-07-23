# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Real-git transport configuration guards for authenticated publication."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from orchestrator import git_plumbing, workflow

from tests.workflow_helpers import (
    _TEST_SPEC,
    _temp_git_repo_with_local_config,
)

ISSUE_BRANCH = "orchestrator/issue-5"
TOKEN_RESOLVER_ATTR = "_resolve_github_token"
HTTP_PROXY_KEY = "http.proxy"


class TransportConfigHardeningTest(unittest.TestCase):
    """Authenticated git operations reject agent-writable transport config."""

    def test_unsafe_config_flags_transport_keys(self) -> None:
        cases = {
            HTTP_PROXY_KEY: [(HTTP_PROXY_KEY, "http://evil.example:8080")],
            "http.sslVerify=false": [("http.sslVerify", "false")],
            "url-scoped http.proxy": [
                ("http.https://github.com/.proxy", "http://evil.example:8080"),
            ],
            "url rewrite": [
                ("url.https://evil.example/.insteadOf", "https://github.com/"),
            ],
        }
        for label, pairs in cases.items():
            with self.subTest(config=label):
                with _temp_git_repo_with_local_config(pairs) as repo:
                    flagged = git_plumbing._unsafe_local_transport_config(repo)
                self.assertTrue(flagged, f"{label} should be rejected, got {flagged!r}")

    def test_unsafe_config_allows_clean_clone_config(self) -> None:
        clean = [
            ("remote.origin.url", "https://github.com/acme/widgets.git"),
            ("branch.main.remote", "origin"),
            ("core.logAllRefUpdates", "true"),
        ]
        with _temp_git_repo_with_local_config(clean) as repo:
            self.assertEqual(git_plumbing._unsafe_local_transport_config(repo), "")

    def test_unsafe_config_follows_local_include_path(self) -> None:
        with _temp_git_repo_with_local_config([]) as repo:
            evil = repo / "evil.conf"
            evil.write_text("[http]\n\tproxy = http://evil.example:8080\n")
            subprocess.run(
                ["git", "config", "--local", "include.path", str(evil)],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            self.assertIn(
                HTTP_PROXY_KEY,
                git_plumbing._unsafe_local_transport_config(repo),
            )

    def test_unsafe_config_reads_per_worktree_config(self) -> None:
        with _temp_git_repo_with_local_config([("extensions.worktreeConfig", "true")]) as repo:
            subprocess.run(
                ["git", "config", "--worktree", HTTP_PROXY_KEY, "http://evil.example:9090"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            self.assertIn(
                HTTP_PROXY_KEY,
                git_plumbing._unsafe_local_transport_config(repo),
            )

    def test_push_refused_on_real_local_http_proxy(self) -> None:
        with (
            _temp_git_repo_with_local_config([(HTTP_PROXY_KEY, "http://evil.example:8080")]) as repo,
            patch.object(
                workflow.config,
                TOKEN_RESOLVER_ATTR,
                return_value="ghp-test-secret",
            ),
            self.assertLogs(git_plumbing.log, level="ERROR") as logs,
        ):
            ok = workflow._push_branch(_TEST_SPEC, repo, ISSUE_BRANCH)
            log_output = logs.output
        self.assertFalse(ok)
        self.assertTrue(
            any(HTTP_PROXY_KEY in line for line in log_output),
            f"expected {HTTP_PROXY_KEY} in refusal log, got {log_output!r}",
        )
