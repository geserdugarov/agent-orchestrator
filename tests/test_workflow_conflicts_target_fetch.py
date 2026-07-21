# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch as mock_patch

from orchestrator import config, git_plumbing, workflow

from tests.workflow_helpers import (
    _GitRunRecorder,
    _TEST_SPEC,
    _TokenResolver,
    _temp_git_repo_with_local_config,
)


class AuthedTargetFetchTest(unittest.TestCase):
    """`_authed_target_fetch` replaces the plain `git fetch <remote> <branch>`
    invocations the worktree creators / per-tick base refresh used to run
    in `spec.target_root`. The plain form relied on git's ambient credential
    helper or session state, which fails under systemd (`GIT_TERMINAL_PROMPT=0`
    disables the prompt) and has no way to pick a per-repo token when the
    local clone has multiple GitHub-pointing remotes whose slug differs from
    `config.REPO`. Mirrors `AuthedFetchHardeningTest`'s shape but covers
    target-root semantics: token selection follows `spec.slug`,
    local-namespace ref selection follows `spec.remote_name`.
    """

    def test_uses_spec_token_and_remote_ref(self) -> None:
        # Acceptance criterion: a `REPOS` row like
        # `geserdugarov/lance-private|...|cache-branch|private` should
        # resolve its token from `~/.config/geserdugarov/lance-private/token`
        # (i.e. `spec.slug`) and write the fetched ref under
        # `refs/remotes/private/...` (i.e. `spec.remote_name`). Without
        # this split the bug surfaces as `fatal: could not read Username
        # for 'https://github.com'`.
        run_recorder = _GitRunRecorder()
        token_resolver = _TokenResolver()

        repo = config.RepoSpec(
            slug="geserdugarov/lance-private",
            target_root=Path("/tmp/orchestrator-test-shared-clone"),
            base_branch="cache-branch",
            remote_name="private",
        )
        with mock_patch("subprocess.run", side_effect=run_recorder), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", token_resolver,
             ):
            fetch = workflow._authed_target_fetch(repo, "cache-branch")

        self.assertEqual(fetch.returncode, 0)
        # Token resolved exactly once -- for the spec's slug, NOT the
        # `remote_name` (which is just a local namespace label).
        self.assertEqual(token_resolver.slugs, ["geserdugarov/lance-private"])
        env = run_recorder.env
        self.assertEqual(
            env.get("GIT_TOKEN"), "ghp-token-for-geserdugarov-lance-private",
        )
        # Auth URL targets the spec's slug, NOT `remote_name`.
        self.assertIn(
            "https://x-access-token@github.com/geserdugarov/lance-private.git",
            run_recorder.args,
        )
        # The refspec writes under `refs/remotes/private/...`, NOT
        # `refs/remotes/origin/...` -- the local clone's `private` remote
        # is what the worktree creators anchor on.
        self.assertIn(
            "+refs/heads/cache-branch:refs/remotes/private/cache-branch",
            run_recorder.args,
        )
        # And the fetch runs in `spec.target_root` (the shared local clone).
        self.assertEqual(run_recorder.cwd, str(repo.target_root))

    def test_token_is_delivered_via_askpass_not_argv(self) -> None:
        # Same hardening as `_push_branch` / `_authed_fetch`: token in
        # GIT_TOKEN env var (read by a tempfile askpass), never in argv,
        # global/system config detached, hooks/fsmonitor/credential
        # helpers blocked.
        run_recorder = _GitRunRecorder()

        with mock_patch("subprocess.run", side_effect=run_recorder), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="super-secret-token",
             ):
            workflow._authed_target_fetch(_TEST_SPEC, "main")

        env = run_recorder.env
        self.assertIn("GIT_ASKPASS", env)
        self.assertEqual(env.get("GIT_TOKEN"), "super-secret-token")
        # Token must NOT appear in argv (would surface in /proc/<pid>/cmdline).
        for arg in run_recorder.args:
            self.assertNotIn("super-secret-token", str(arg))
        # Global/system git config detached so url rewrites planted in
        # `~/.gitconfig` cannot redirect the fetch.
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), os.devnull)
        # Hooks / fsmonitor / credential helpers blocked via -c overrides.
        argv = run_recorder.args
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("credential.helper=", argv)
        self.assertIn("core.fsmonitor=", argv)

    def test_root_url_rewrite_rule_is_refused(self) -> None:
        # The agent has write access to linked worktrees, and a linked
        # worktree can rewrite the parent clone's local config via
        # `git config --local`. Local config still applies even with
        # GIT_CONFIG_GLOBAL/SYSTEM detached, so a planted
        # `url.https://evil.example/.insteadOf https://github.com/`
        # would redirect the token-bearing fetch to the attacker host
        # and exfiltrate GIT_TOKEN. The pre-flight check must refuse.
        rewrite_check = MagicMock(
            returncode=0,
            stdout="url.https://evil.example/.insteadof https://github.com/\n",
            stderr="",
        )
        run_recorder = _GitRunRecorder(probe_result=rewrite_check)

        with mock_patch("subprocess.run", side_effect=run_recorder), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="super-secret-token",
             ):
            fetch = workflow._authed_target_fetch(_TEST_SPEC, "main")

        # Only the rewrite probe ran; the token-bearing fetch did NOT.
        self.assertEqual(len(run_recorder.calls), 1)
        self.assertEqual(
            run_recorder.calls[0][:3],
            ["git", "config", "--get-regexp"],
        )
        self.assertNotEqual(fetch.returncode, 0)
        # And the token NEVER reached the (skipped) fetch subprocess env.
        for arg in run_recorder.calls[0]:
            self.assertNotIn("super-secret-token", str(arg))

    def test_local_ssl_verify_disable_is_refused(self) -> None:
        # A linked worktree can disable TLS verification in the parent clone's
        # local config via `git config --local http.sslVerify false`; the
        # token-bearing target fetch must fail closed on it, not just on url
        # rewrites. Real git config resolution (not a mocked probe) proves the
        # broadened regexp catches http.* transport keys.
        with _temp_git_repo_with_local_config(
            [("http.sslVerify", "false")]
        ) as repo:
            spec = config.RepoSpec(
                slug="geserdugarov/agent-orchestrator",
                target_root=repo,
                base_branch="main",
            )
            with mock_patch.object(
                workflow.config, "_resolve_github_token",
                return_value="super-secret-token",
            ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
                fetch = workflow._authed_target_fetch(spec, "main")
        self.assertNotEqual(fetch.returncode, 0)
        self.assertTrue(
            any("sslverify" in line.lower() for line in cm.output),
            f"expected sslVerify in refusal log, got {cm.output!r}",
        )

    def test_missing_token_fails_without_subprocess(self) -> None:
        # When the per-spec token file is missing, fail loudly with the
        # slug in the log -- a multi-repo deployment that forgot to drop
        # `~/.config/<slug>/token` gets a debuggable error rather than
        # a generic "could not read Username".
        subprocess_run = MagicMock()

        repo = config.RepoSpec(
            slug="geserdugarov/lance-private",
            target_root=Path("/tmp/orchestrator-test-shared-clone"),
            base_branch="cache-branch",
            remote_name="private",
        )
        with mock_patch("subprocess.run", subprocess_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", return_value="",
             ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
            fetch = workflow._authed_target_fetch(repo, "cache-branch")

        # Failed without ever shelling out.
        subprocess_run.assert_not_called()
        self.assertNotEqual(fetch.returncode, 0)
        # Slug is in the log so the operator knows which token file to fix.
        self.assertTrue(
            any("geserdugarov/lance-private" in line for line in cm.output),
            f"expected slug in log output, got {cm.output!r}",
        )


if __name__ == "__main__":
    unittest.main()
