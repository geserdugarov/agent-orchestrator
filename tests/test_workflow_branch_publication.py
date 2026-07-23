# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""``_push_branch`` lease, failure, and per-repository token decisions."""

from __future__ import annotations

import contextlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow

from tests.workflow_helpers import (
    _FAKE_WT,
    _TEST_SPEC,
)

ISSUE_BRANCH = "orchestrator/issue-5"
ISSUE_REF = f"refs/heads/{ISSUE_BRANCH}"
TOKEN_RESOLVER_ATTR = "_resolve_github_token"
GIT_FAILURE_EXIT_CODE = 128


def _git_result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    git_result = MagicMock()
    git_result.returncode = returncode
    git_result.stdout = stdout
    git_result.stderr = stderr
    return git_result


@contextlib.contextmanager
def _patched_push(run_results: list):
    run_mock = MagicMock(side_effect=run_results)
    with (
        patch.object(
            workflow.config,
            TOKEN_RESOLVER_ATTR,
            return_value="ghp-test-secret",
        ),
        patch.object(workflow.subprocess, "run", run_mock),
    ):
        yield run_mock


class _TokenResolver:
    def __init__(self) -> None:
        self.slugs: list[str] = []

    def __call__(self, slug: str) -> str:
        self.slugs.append(slug)
        return f"ghp-token-for-{slug.replace('/', '-')}"


class PushBranchTest(unittest.TestCase):
    """`_push_branch` handles the divergence cases that bit issue-5.

    A self-restart can leave the local worktree on a different SHA than the
    one already pushed (e.g. codex `resume=False` rerun produced equivalent
    work with new committer dates). A plain push then fails non-fast-forward
    and parks the issue. The function uses ls-remote + --force-with-lease so
    the retry succeeds, and the lease still blocks unobserved updates.
    """

    def test_existing_remote_branch_uses_observed_sha(self) -> None:
        # rewrite check (clean), ls-remote (returns sha), push (ok)
        sha = "87b2bc94b03a1729ef8b8145836d0959f433600e"
        ls_stdout = f"{sha}\t{ISSUE_REF}\n"
        with _patched_push(
            [
                _git_result(),
                _git_result(stdout=ls_stdout),
                _git_result(),
            ]
        ) as run_mock:
            ok = workflow._push_branch(_TEST_SPEC, _FAKE_WT, ISSUE_BRANCH)
            self.assertTrue(ok)
            push_cmd = run_mock.call_args_list[2].args[0]
            self.assertIn("push", push_cmd)
            self.assertIn(
                f"--force-with-lease={ISSUE_REF}:{sha}",
                push_cmd,
            )
            self.assertIn(f"HEAD:{ISSUE_REF}", push_cmd)

    def test_missing_remote_branch_uses_empty_lease(self) -> None:
        # First push ever for this branch -- ls-remote returns nothing, the
        # lease becomes "expect ref to not exist" so a concurrent create still
        # fails the lease.
        with _patched_push(
            [
                _git_result(),
                _git_result(stdout=""),
                _git_result(),
            ]
        ) as run_mock:
            ok = workflow._push_branch(_TEST_SPEC, _FAKE_WT, "orchestrator/issue-9")
            self.assertTrue(ok)
            push_cmd = run_mock.call_args_list[2].args[0]
            self.assertIn(
                "--force-with-lease=refs/heads/orchestrator/issue-9:",
                push_cmd,
            )

    def test_ls_remote_failure_aborts_without_pushing(self) -> None:
        with _patched_push(
            [
                _git_result(),
                _git_result(returncode=GIT_FAILURE_EXIT_CODE, stderr="network down"),
            ]
        ) as run_mock:
            ok = workflow._push_branch(_TEST_SPEC, _FAKE_WT, ISSUE_BRANCH)
            self.assertFalse(ok)
            # Only rewrite-check + ls-remote ran; the push subprocess.run was not
            # invoked.
            self.assertEqual(run_mock.call_count, 2)

    def test_push_failure_returns_false(self) -> None:
        ls_stdout = f"abc123\t{ISSUE_REF}\n"
        with _patched_push(
            [
                _git_result(),
                _git_result(stdout=ls_stdout),
                _git_result(returncode=GIT_FAILURE_EXIT_CODE, stderr="rejected"),
            ]
        ):
            ok = workflow._push_branch(_TEST_SPEC, _FAKE_WT, ISSUE_BRANCH)
        self.assertFalse(ok)

    def test_url_rewrite_in_local_config_refuses_push(self) -> None:
        # Local .git/config carrying a url.<host>.insteadOf rewrite is the
        # exfil vector the security hardening guards against; ls-remote and
        # push must never run.
        rewrite_hit = MagicMock()
        rewrite_hit.returncode = 0
        rewrite_hit.stdout = "url.https://evil.example.com/.insteadof https://github.com/\n"
        rewrite_hit.stderr = ""
        with _patched_push([rewrite_hit]) as run_mock:
            ok = workflow._push_branch(_TEST_SPEC, _FAKE_WT, ISSUE_BRANCH)
            self.assertFalse(ok)
            self.assertEqual(run_mock.call_count, 1)

    def test_uses_per_spec_token_for_git_push(self) -> None:
        # Multi-repo regression guard: `_push_branch` must resolve the token
        # from `spec.slug` (so a per-repo `~/.config/<owner>/<repo>/token`
        # file is honored), not from the cached single-repo
        # `config.GITHUB_TOKEN` that was looked up once for `config.REPO`.
        run_mock = MagicMock(
            side_effect=[
                _git_result(),
                _git_result(
                    stdout=f"deadbeefcafef00ddeadbeefcafef00ddeadbeef\t{ISSUE_REF}\n",
                ),
                _git_result(),
            ]
        )
        resolver = _TokenResolver()

        with (
            patch.object(workflow.config, TOKEN_RESOLVER_ATTR, resolver),
            patch.object(workflow.subprocess, "run", run_mock),
        ):
            self.assertTrue(
                workflow._push_branch(
                    config.RepoSpec(
                        slug="acme/widgets",
                        target_root=Path("/tmp/orchestrator-test-target-root"),
                        base_branch="main",
                    ),
                    _FAKE_WT,
                    ISSUE_BRANCH,
                )
            )
        # Token was resolved exactly once, for the spec's slug.
        self.assertEqual(resolver.slugs, ["acme/widgets"])
        ls_call = run_mock.call_args_list[1]
        push_call = run_mock.call_args_list[2]
        # ls-remote and push both run with the per-spec token in GIT_TOKEN.
        self.assertEqual(ls_call.kwargs["env"]["GIT_TOKEN"], "ghp-token-for-acme-widgets")
        self.assertEqual(push_call.kwargs["env"]["GIT_TOKEN"], "ghp-token-for-acme-widgets")
        # Auth URL targets the spec's slug, not the cached config.REPO.
        self.assertIn(
            "https://x-access-token@github.com/acme/widgets.git",
            ls_call.args[0],
        )

    def test_missing_spec_token_logs_slug_and_aborts(self) -> None:
        # A multi-repo deployment that forgot to populate the per-slug
        # token file should refuse to push and log which repo is misconfigured
        # rather than the generic "GITHUB_TOKEN missing" the legacy code emitted.
        run_mock = MagicMock()
        with (
            patch.object(workflow.config, TOKEN_RESOLVER_ATTR, return_value=""),
            patch.object(workflow.subprocess, "run", run_mock),
        ):
            ok = workflow._push_branch(_TEST_SPEC, _FAKE_WT, ISSUE_BRANCH)
        self.assertFalse(ok)
        # Push aborted before any subprocess ran.
        run_mock.assert_not_called()
