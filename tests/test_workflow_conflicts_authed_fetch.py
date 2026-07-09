# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path

from orchestrator import config, git_plumbing, workflow

from tests.workflow_helpers import _TEST_SPEC, _temp_git_repo_with_local_config


class AuthedFetchHardeningTest(unittest.TestCase):
    """`_authed_fetch` is the in-worktree authenticated fetch helper used
    by `_handle_resolving_conflict`. Mirrors `_push_branch`'s security
    envelope: askpass-based auth, detached global/system config, blocked
    hooks/fsmonitor/credential helpers, refusal to run when the worktree
    carries url-rewrite rules or http.* proxy/TLS transport config.
    """

    def test_askpass_token_and_blocks_inherited_config(self) -> None:
        from unittest.mock import patch as mock_patch, MagicMock

        # First subprocess.run call is the rewrite-rule probe (returncode=1
        # = no rewrite rules); second is the real fetch -- capture its env.
        captured: dict[str, dict] = {}

        rewrite_check = MagicMock(returncode=1, stdout="", stderr="")
        fetch_result = MagicMock(returncode=0, stdout="", stderr="")

        def fake_run(args, **kwargs):
            if args and args[:3] == ["git", "config", "--get-regexp"]:
                return rewrite_check
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return fetch_result

        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="fake-token-xyz",
             ):
            workflow._authed_fetch(
                _TEST_SPEC,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )

        env = captured["env"]
        # askpass wires the token via env, NOT argv.
        self.assertIn("GIT_ASKPASS", env)
        self.assertEqual(env.get("GIT_TOKEN"), "fake-token-xyz")
        # Token must NOT appear in argv.
        for arg in captured["args"]:
            self.assertNotIn("fake-token-xyz", str(arg))
        # Global/system config detached so url rewrites planted there
        # cannot redirect the fetch to an attacker-controlled host.
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), os.devnull)
        # Hooks / fsmonitor / credential helpers blocked via -c overrides.
        argv = captured["args"]
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("credential.helper=", argv)
        self.assertIn("core.fsmonitor=", argv)
        # Auth URL carries only the username, not the token.
        self.assertTrue(
            any(
                isinstance(a, str)
                and a.startswith("https://x-access-token@github.com/")
                for a in argv
            ),
            f"expected x-access-token auth URL in argv, got {argv!r}",
        )

    def test_refuses_when_worktree_has_url_rewrite_rule(self) -> None:
        from unittest.mock import patch as mock_patch, MagicMock

        # Rewrite-rule probe returns a hit; the real fetch must NOT run.
        rewrite_check = MagicMock(
            returncode=0,
            stdout="url.https://evil.example/.insteadof https://github.com/\n",
            stderr="",
        )
        fetch_result = MagicMock(returncode=0, stdout="", stderr="")
        runs: list = []

        def fake_run(args, **kwargs):
            runs.append(args)
            if args and args[:3] == ["git", "config", "--get-regexp"]:
                return rewrite_check
            return fetch_result

        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="fake-token-xyz",
             ):
            fetch = workflow._authed_fetch(
                _TEST_SPEC,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )

        # Only the rewrite probe ran -- the fetch was refused.
        self.assertEqual(len(runs), 1)
        self.assertNotEqual(fetch.returncode, 0)

    def test_refuses_on_real_local_http_proxy(self) -> None:
        # The url-rewrite refusal test above mocks the config probe, so it
        # can't prove the broadened regexp actually catches http.* keys. Use
        # real git config resolution: a worktree carrying `http.proxy` must
        # make `_authed_fetch` fail closed before the token-bearing fetch runs.
        from unittest.mock import patch as mock_patch

        with _temp_git_repo_with_local_config(
            [("http.proxy", "http://evil.example:8080")]
        ) as repo, mock_patch.object(
            workflow.config, "_resolve_github_token",
            return_value="fake-token-xyz",
        ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
            fetch = workflow._authed_fetch(
                _TEST_SPEC,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=repo,
            )
        self.assertNotEqual(fetch.returncode, 0)
        self.assertTrue(
            any("http.proxy" in line for line in cm.output),
            f"expected http.proxy in refusal log, got {cm.output!r}",
        )

    def test_no_token_returns_failure_without_subprocess(self) -> None:
        from unittest.mock import patch as mock_patch, MagicMock

        runs: list = []

        def fake_run(args, **kwargs):
            runs.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", return_value=""
             ):
            fetch = workflow._authed_fetch(
                _TEST_SPEC, "refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )

        # No subprocess at all when the token is missing.
        self.assertEqual(runs, [])
        self.assertNotEqual(fetch.returncode, 0)

    def test_uses_per_spec_token_for_git_fetch(self) -> None:
        # Multi-repo regression guard: `_authed_fetch` must resolve the token
        # from `spec.slug` (so a per-repo `~/.config/<owner>/<repo>/token`
        # file is honored), not from the cached single-repo
        # `config.GITHUB_TOKEN` looked up once for `config.REPO`. Without
        # this, `_handle_resolving_conflict` fetches origin/<branch> /
        # origin/<base> with the wrong (or empty) token for any repo other
        # than the legacy single-repo `REPO`.
        from unittest.mock import patch as mock_patch, MagicMock

        rewrite_check = MagicMock(returncode=1, stdout="", stderr="")
        fetch_result = MagicMock(returncode=0, stdout="", stderr="")
        captured: dict[str, object] = {}

        def fake_run(args, **kwargs):
            if args and args[:3] == ["git", "config", "--get-regexp"]:
                return rewrite_check
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return fetch_result

        resolved: list[str] = []

        def fake_resolve(slug: str) -> str:
            resolved.append(slug)
            # Distinct token per slug so a regression that fell back to
            # `config.GITHUB_TOKEN` would surface in GIT_TOKEN below.
            return f"ghp-token-for-{slug.replace('/', '-')}"

        repo = config.RepoSpec(
            slug="acme/widgets",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
        )
        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", fake_resolve
             ):
            fetch = workflow._authed_fetch(
                repo,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )
        self.assertEqual(fetch.returncode, 0)
        # Token resolved exactly once, for the spec's slug -- not for
        # `config.REPO`.
        self.assertEqual(resolved, ["acme/widgets"])
        env = captured["env"]
        self.assertEqual(env.get("GIT_TOKEN"), "ghp-token-for-acme-widgets")
        # Auth URL targets the spec's slug, not the cached config.REPO.
        self.assertIn(
            "https://x-access-token@github.com/acme/widgets.git",
            captured["args"],
        )

    def test_missing_per_spec_token_logs_slug(self) -> None:
        # A multi-repo deployment that forgot to populate the per-slug token
        # file should fail the fetch with the misconfigured slug surfaced in
        # the error log -- the resolving_conflict handler then parks awaiting
        # human, which is far more debuggable than a generic "GITHUB_TOKEN
        # missing" with no repo identifier.
        from unittest.mock import patch as mock_patch, MagicMock

        runs: list = []

        def fake_run(args, **kwargs):
            runs.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        repo = config.RepoSpec(
            slug="acme/widgets",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
        )
        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", return_value=""
             ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
            fetch = workflow._authed_fetch(
                repo,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )
        # Fetch aborted before any subprocess ran.
        self.assertEqual(runs, [])
        self.assertNotEqual(fetch.returncode, 0)
        self.assertTrue(
            any("acme/widgets" in line for line in cm.output),
            f"expected slug 'acme/widgets' in log output, got {cm.output!r}",
        )


if __name__ == "__main__":
    unittest.main()
