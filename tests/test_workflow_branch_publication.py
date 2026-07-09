# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`_push_branch` divergence handling: ls-remote + --force-with-lease for
the legacy self-restart case, empty-lease for first-time pushes, refusal
on local url.<host>.insteadOf rewrites and http.* proxy/TLS transport config,
and per-spec token resolution so multi-repo deployments honor each
`~/.config/<owner>/<repo>/token` file."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config, git_plumbing, workflow

from tests.workflow_helpers import (
    _FAKE_WT, _TEST_SPEC, _temp_git_repo_with_local_config,
)


class PushBranchTest(unittest.TestCase):
    """`_push_branch` handles the divergence cases that bit issue-5.

    A self-restart can leave the local worktree on a different SHA than the
    one already pushed (e.g. codex `resume=False` rerun produced equivalent
    work with new committer dates). A plain push then fails non-fast-forward
    and parks the issue. The function uses ls-remote + --force-with-lease so
    the retry succeeds, and the lease still blocks unobserved updates.
    """

    @staticmethod
    def _ok(stdout: str = "", stderr: str = "") -> "object":
        result = MagicMock()
        result.returncode = 0
        result.stdout = stdout
        result.stderr = stderr
        return result

    @staticmethod
    def _fail(stderr: str = "boom") -> "object":
        result = MagicMock()
        result.returncode = 128
        result.stdout = ""
        result.stderr = stderr
        return result

    def _patch(self, run_results: list) -> "tuple":
        run_mock = MagicMock(side_effect=run_results)
        # `_push_branch` resolves the token per-spec via
        # `config._resolve_github_token(spec.slug)`; patch the function so
        # tests don't depend on a real token file existing on disk.
        token_patch = patch.object(
            workflow.config, "_resolve_github_token",
            return_value="ghp-test-secret",
        )
        run_patch = patch.object(workflow.subprocess, "run", run_mock)
        return run_mock, token_patch, run_patch

    def test_existing_remote_branch_uses_observed_sha(self) -> None:
        # rewrite check (clean), ls-remote (returns sha), push (ok)
        sha = "87b2bc94b03a1729ef8b8145836d0959f433600e"
        ls_stdout = f"{sha}\trefs/heads/orchestrator/issue-5\n"
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._ok(stdout=ls_stdout), self._ok()]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertTrue(ok)
        push_cmd = run_mock.call_args_list[2].args[0]
        self.assertIn("push", push_cmd)
        self.assertIn(
            f"--force-with-lease=refs/heads/orchestrator/issue-5:{sha}",
            push_cmd,
        )
        self.assertIn("HEAD:refs/heads/orchestrator/issue-5", push_cmd)

    def test_missing_remote_branch_uses_empty_lease(self) -> None:
        # First push ever for this branch -- ls-remote returns nothing, the
        # lease becomes "expect ref to not exist" so a concurrent create still
        # fails the lease.
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._ok(stdout=""), self._ok()]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-9"
            )
        self.assertTrue(ok)
        push_cmd = run_mock.call_args_list[2].args[0]
        self.assertIn(
            "--force-with-lease=refs/heads/orchestrator/issue-9:",
            push_cmd,
        )

    def test_ls_remote_failure_aborts_without_pushing(self) -> None:
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._fail("network down")]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)
        # Only rewrite-check + ls-remote ran; the push subprocess.run was not
        # invoked.
        self.assertEqual(run_mock.call_count, 2)

    def test_push_failure_returns_false(self) -> None:
        ls_stdout = "abc123\trefs/heads/orchestrator/issue-5\n"
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._ok(stdout=ls_stdout), self._fail("rejected")]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)

    def test_url_rewrite_in_local_config_refuses_push(self) -> None:
        # Local .git/config carrying a url.<host>.insteadOf rewrite is the
        # exfil vector the security hardening guards against; ls-remote and
        # push must never run.
        rewrite_hit = MagicMock()
        rewrite_hit.returncode = 0
        rewrite_hit.stdout = (
            "url.https://evil.example.com/.insteadof https://github.com/\n"
        )
        rewrite_hit.stderr = ""
        run_mock, token_patch, run_patch = self._patch([rewrite_hit])
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)
        self.assertEqual(run_mock.call_count, 1)

    def test_uses_per_spec_token_for_git_push(self) -> None:
        # Multi-repo regression guard: `_push_branch` must resolve the token
        # from `spec.slug` (so a per-repo `~/.config/<owner>/<repo>/token`
        # file is honored), not from the cached single-repo
        # `config.GITHUB_TOKEN` that was looked up once for `config.REPO`.
        sha = "deadbeefcafef00ddeadbeefcafef00ddeadbeef"
        ls_stdout = f"{sha}\trefs/heads/orchestrator/issue-5\n"
        run_mock = MagicMock(side_effect=[
            self._ok(),                # rewrite check (clean)
            self._ok(stdout=ls_stdout),  # ls-remote
            self._ok(),                # push
        ])
        resolved: list[str] = []

        def fake_resolve(slug: str) -> str:
            resolved.append(slug)
            # Return distinct tokens so a regression that fell back to the
            # cached `config.GITHUB_TOKEN` would surface in GIT_TOKEN below.
            return f"ghp-token-for-{slug.replace('/', '-')}"

        other_spec = config.RepoSpec(
            slug="acme/widgets",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
        )
        with patch.object(workflow.config, "_resolve_github_token", fake_resolve), \
             patch.object(workflow.subprocess, "run", run_mock):
            ok = workflow._push_branch(
                other_spec, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertTrue(ok)
        # Token was resolved exactly once, for the spec's slug.
        self.assertEqual(resolved, ["acme/widgets"])
        ls_call = run_mock.call_args_list[1]
        push_call = run_mock.call_args_list[2]
        # ls-remote and push both run with the per-spec token in GIT_TOKEN.
        self.assertEqual(
            ls_call.kwargs["env"]["GIT_TOKEN"], "ghp-token-for-acme-widgets"
        )
        self.assertEqual(
            push_call.kwargs["env"]["GIT_TOKEN"], "ghp-token-for-acme-widgets"
        )
        # Auth URL targets the spec's slug, not the cached config.REPO.
        self.assertIn(
            "https://x-access-token@github.com/acme/widgets.git",
            ls_call.args[0],
        )

    def test_missing_per_spec_token_aborts_with_slug_in_log(self) -> None:
        # A multi-repo deployment that forgot to populate the per-slug
        # token file should refuse to push and log which repo is misconfigured
        # rather than the generic "GITHUB_TOKEN missing" the legacy code emitted.
        run_mock = MagicMock()
        with patch.object(
            workflow.config, "_resolve_github_token", return_value=""
        ), patch.object(workflow.subprocess, "run", run_mock):
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)
        # Push aborted before any subprocess ran.
        run_mock.assert_not_called()


class TransportConfigHardeningTest(unittest.TestCase):
    """Authenticated git ops must not honor agent-writable local HTTP proxy /
    TLS config while GIT_TOKEN is in scope. A worktree the agent controls can
    plant `http.proxy` / `http.sslVerify=false` in `.git/config` to tunnel the
    token-bearing push through an attacker proxy or strip TLS verification, so
    the pre-flight fails closed on any local `http.*` key as well as the
    url.<host>.insteadOf rewrites it already rejected. These exercise real git
    config resolution because a mocked `--get-regexp` probe would pass even
    against the old rewrite-only regexp.
    """

    def test_unsafe_config_flags_transport_keys(self) -> None:
        cases = {
            "http.proxy": [("http.proxy", "http://evil.example:8080")],
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
                self.assertTrue(
                    flagged, f"{label} should be rejected, got {flagged!r}"
                )

    def test_unsafe_config_allows_clean_clone_config(self) -> None:
        # A normal clone's local config (remote URL + branch tracking) must NOT
        # trip the pre-flight, or every push/fetch would be refused.
        clean = [
            ("remote.origin.url", "https://github.com/acme/widgets.git"),
            ("branch.main.remote", "origin"),
            ("core.logAllRefUpdates", "true"),
        ]
        with _temp_git_repo_with_local_config(clean) as repo:
            self.assertEqual(
                git_plumbing._unsafe_local_transport_config(repo), ""
            )

    def test_unsafe_config_follows_local_include_path(self) -> None:
        # `git config --local` does not expand `include.path`, but a real
        # fetch/push does -- so an agent can hide `http.proxy` behind an
        # `[include] path = ...` in `.git/config`. The preflight must resolve
        # includes the same way the token-bearing command will.
        with _temp_git_repo_with_local_config([]) as repo:
            evil = repo / "evil.conf"
            evil.write_text("[http]\n\tproxy = http://evil.example:8080\n")
            subprocess.run(
                ["git", "config", "--local", "include.path", str(evil)],
                cwd=repo, check=True, capture_output=True,
            )
            self.assertIn(
                "http.proxy",
                git_plumbing._unsafe_local_transport_config(repo),
            )

    def test_unsafe_config_reads_per_worktree_config(self) -> None:
        # With `extensions.worktreeConfig` enabled git honors the per-worktree
        # `config.worktree`, which `git config --local` misses -- another place
        # the agent can plant `http.proxy`. The preflight must read it too.
        with _temp_git_repo_with_local_config(
            [("extensions.worktreeConfig", "true")]
        ) as repo:
            subprocess.run(
                ["git", "config", "--worktree", "http.proxy",
                 "http://evil.example:9090"],
                cwd=repo, check=True, capture_output=True,
            )
            self.assertIn(
                "http.proxy",
                git_plumbing._unsafe_local_transport_config(repo),
            )

    def test_push_refused_on_real_local_http_proxy(self) -> None:
        # End-to-end: a real worktree carrying `http.proxy` makes `_push_branch`
        # fail closed before the token-bearing ls-remote / push ever runs.
        with _temp_git_repo_with_local_config(
            [("http.proxy", "http://evil.example:8080")]
        ) as repo, patch.object(
            workflow.config, "_resolve_github_token",
            return_value="ghp-test-secret",
        ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
            ok = workflow._push_branch(_TEST_SPEC, repo, "orchestrator/issue-5")
        self.assertFalse(ok)
        self.assertTrue(
            any("http.proxy" in line for line in cm.output),
            f"expected http.proxy in refusal log, got {cm.output!r}",
        )
