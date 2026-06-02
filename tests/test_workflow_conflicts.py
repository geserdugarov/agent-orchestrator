# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, git_plumbing, workflow, worktrees
from orchestrator.github import BASE_SYNC_HOLD_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class ResolvingConflictEventEmissionTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`_handle_resolving_conflict` emits `merge_attempt` for each base-
    rebase attempt, `conflict_round` whenever the counter ticks, and the
    same `pr_merged` / `pr_closed_without_merge` terminals as in_review.
    """

    BRANCH = "orchestrator/issue-300"
    PR_NUMBER = 900

    @staticmethod
    def _events_of(gh, event_name: str) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == event_name]

    def _seed(self, *, pr_state="open", pr_merged=False, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(300, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="feed1234"),
            mergeable=False, check_state="success",
            merged=pr_merged, state=pr_state,
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=2, conflict_round=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(300, **state)
        return gh, issue, pr

    def _run_with_merge(
        self, gh, issue, *,
        merge_succeeded=True, conflicted_files=(),
        head_shas=("before", "after"), push_branch=True,
        run_agent_result=None,
    ):
        from unittest.mock import MagicMock

        agent = run_agent_result or _agent(
            session_id="dev-sess", last_message="resolved",
        )
        merge_mock = MagicMock(
            return_value=(merge_succeeded, list(conflicted_files))
        )
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock), \
             patch.object(workflow, "_git", MagicMock(return_value=ok)), \
             patch.object(workflow, "_git_hardened", MagicMock(return_value=ok)):
            return self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=agent,
                push_branch=push_branch,
                head_shas=head_shas,
            )

    def test_merge_attempt_success_on_clean_base_rebase(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=True,
            head_shas=["before", "merged"],
        )
        attempts = self._events_of(gh, "merge_attempt")
        self.assertEqual(len(attempts), 1)
        ev = attempts[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        self.assertEqual(ev["pr_number"], self.PR_NUMBER)
        self.assertEqual(ev["method"], "base_rebase")
        self.assertEqual(ev["result"], "success")
        self.assertEqual(ev["conflict_round"], 0)

    def test_merge_attempt_conflict_when_base_rebase_leaves_unmerged_paths(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=False,
            conflicted_files=["a.py", "b.py"],
            head_shas=["before", "merged"],
        )
        attempts = self._events_of(gh, "merge_attempt")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["result"], "conflict")

    def test_conflict_round_incremented_on_clean_base_rebase_push(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=True,
            head_shas=["before", "merged"], push_branch=True,
        )
        rounds = [
            e for e in self._events_of(gh, "conflict_round")
            if e["action"] == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["conflict_round"], 1)
        self.assertEqual(rounds[0]["outcome"], "base_rebased_clean")
        # SHA is the after-rebase HEAD captured before the push.
        self.assertEqual(rounds[0]["sha"], "merged")

    def test_conflict_round_incremented_on_base_up_to_date_no_op(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=True,
            head_shas=["samehead", "samehead"],
        )
        rounds = [
            e for e in self._events_of(gh, "conflict_round")
            if e["action"] == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["outcome"], "base_up_to_date")

    def test_conflict_round_incremented_after_agent_resolves(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["before", "merged"],
            push_branch=True,
        )
        rounds = [
            e for e in self._events_of(gh, "conflict_round")
            if e["action"] == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["outcome"], "agent_resolved")

    def test_pr_merged_event_on_resolving_conflict_external_merge(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed", pr_merged=True)
        self._run_with_merge(gh, issue)
        merged = self._events_of(gh, "pr_merged")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["stage"], "resolving_conflict")
        self.assertEqual(merged[0]["pr_number"], self.PR_NUMBER)
        # No base rebase attempted on the terminal path.
        self.assertEqual(self._events_of(gh, "merge_attempt"), [])

    def test_pr_closed_without_merge_event_on_resolving_conflict_closed(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed", pr_merged=False)
        self._run_with_merge(gh, issue)
        closed = self._events_of(gh, "pr_closed_without_merge")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["stage"], "resolving_conflict")
        self.assertEqual(closed[0]["pr_number"], self.PR_NUMBER)


class EnsurePrWorktreeRestoresFromRemoteBranchTest(unittest.TestCase):
    """When the local PR branch has been pruned (host restart, manual
    cleanup, `git branch -D`), `_ensure_pr_worktree` must restore it
    from `origin/<branch>` -- NOT from `origin/<base>`. Rebuilding from
    base would silently discard the PR's commits and the conflict
    resolution would never converge.
    """

    ISSUE_NUMBER = 300
    BRANCH = "orchestrator/issue-300"

    def _git_recorder(self, *, local_branch_present: bool):
        """Return a `_git` stand-in that records every invocation and
        answers `rev-parse --verify <branch>` per the flag.
        """
        from unittest.mock import MagicMock

        calls: list[tuple] = []

        def fake_git(*args, cwd):
            calls.append((args, cwd))
            cmd = args[0] if args else ""
            if cmd == "rev-parse":
                rc = 0 if local_branch_present else 1
                return MagicMock(returncode=rc, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(side_effect=fake_git), calls

    def _authed_fetch_mock(self):
        """Return a mock for `_authed_target_fetch` that records every
        call as `(spec, branch)` and returns success. The target-root
        fetches now go through this helper rather than plain `_git`
        because the bare form relied on git's ambient credential helper
        / session state (and could not pick a per-repo token when the
        local clone has multiple GitHub-pointing remotes).
        """
        from unittest.mock import MagicMock
        fetched: list[tuple] = []

        def fake_fetch(spec, branch):
            fetched.append((spec, branch))
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(side_effect=fake_fetch), fetched

    def test_missing_local_branch_restores_from_origin_branch(self) -> None:
        # The most common bad outcome: someone deletes the local branch.
        # Without our fix, `_ensure_worktree`'s fallback would create a
        # NEW branch from `origin/<base>`, discarding all the PR's
        # commits. Our helper must use `origin/<branch>` instead.
        from unittest.mock import MagicMock

        git_mock, calls = self._git_recorder(local_branch_present=False)
        fetch_mock, _ = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False  # worktree dir absent too

        with patch.object(worktrees, "_git", git_mock), \
             patch.object(worktrees, "_authed_target_fetch", fetch_mock), \
             patch.object(worktrees, "_worktree_path", return_value=wt_path), \
             patch.object(worktrees, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        # Find the `worktree add` invocation and verify it anchored on
        # `origin/<branch>`, not `origin/<base>`.
        worktree_adds = [
            args for args, _ in calls if args and args[0] == "worktree" and args[1] == "add"
        ]
        self.assertTrue(worktree_adds, "expected at least one `worktree add` call")
        add_args = worktree_adds[0]
        # Form is: ("worktree", "add", "-b", branch, str(wt), "origin/<branch>")
        self.assertEqual(add_args[2], "-b")
        self.assertEqual(add_args[3], self.BRANCH)
        self.assertEqual(add_args[5], f"origin/{self.BRANCH}")
        # NOT `origin/<base>` -- that would discard the PR's commits.
        self.assertNotEqual(add_args[5], f"origin/{_TEST_SPEC.base_branch}")

    def test_present_local_branch_uses_existing_ref(self) -> None:
        # When the local branch still exists, attach the worktree to it
        # directly (no -b restoration needed).
        from unittest.mock import MagicMock

        git_mock, calls = self._git_recorder(local_branch_present=True)
        fetch_mock, _ = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False

        with patch.object(worktrees, "_git", git_mock), \
             patch.object(worktrees, "_authed_target_fetch", fetch_mock), \
             patch.object(worktrees, "_worktree_path", return_value=wt_path), \
             patch.object(worktrees, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        worktree_adds = [
            args for args, _ in calls if args and args[0] == "worktree" and args[1] == "add"
        ]
        self.assertTrue(worktree_adds)
        add_args = worktree_adds[0]
        # No `-b` -- attach to the existing local branch as-is.
        self.assertNotIn("-b", add_args)
        self.assertEqual(add_args[3], self.BRANCH)

    def test_non_fetch_git_calls_run_in_target_root(self) -> None:
        # All non-fetch git invocations (rev-parse, worktree add/remove)
        # must run from `spec.target_root`. Authed fetches are routed
        # via `_authed_target_fetch` which already cd's into target_root.
        from unittest.mock import MagicMock

        git_mock, calls = self._git_recorder(local_branch_present=True)
        fetch_mock, _ = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False

        with patch.object(worktrees, "_git", git_mock), \
             patch.object(worktrees, "_authed_target_fetch", fetch_mock), \
             patch.object(worktrees, "_worktree_path", return_value=wt_path), \
             patch.object(worktrees, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        for args, cwd in calls:
            self.assertEqual(
                cwd, _TEST_SPEC.target_root,
                f"git invocation {args} ran from {cwd}, "
                f"expected {_TEST_SPEC.target_root}",
            )

    def test_branch_fetch_routed_through_authed_target_fetch(self) -> None:
        # `git fetch <remote> <branch>` in target_root used to relyon git's
        # ambient credential helper; `_authed_target_fetch` replaces it
        # with an askpass-delivered per-spec token. The branch fetch and
        # the base-branch fetch must both go through the helper, and
        # neither must surface as a plain `_git("fetch", ...)` call.
        from unittest.mock import MagicMock

        git_mock, git_calls = self._git_recorder(local_branch_present=True)
        fetch_mock, fetched = self._authed_fetch_mock()

        wt_path = MagicMock()
        wt_path.exists.return_value = False

        with patch.object(worktrees, "_git", git_mock), \
             patch.object(worktrees, "_authed_target_fetch", fetch_mock), \
             patch.object(worktrees, "_worktree_path", return_value=wt_path), \
             patch.object(worktrees, "_repo_worktrees_root", return_value=MagicMock()):
            workflow._ensure_pr_worktree(_TEST_SPEC, self.ISSUE_NUMBER)

        # Both fetches landed on the authed helper -- base and PR branch.
        self.assertEqual(len(fetched), 2)
        branches = {branch for _spec, branch in fetched}
        self.assertEqual(branches, {_TEST_SPEC.base_branch, self.BRANCH})
        # And no plain-git fetch leaked through (which would prompt for
        # credentials under systemd and fail).
        for args, _cwd in git_calls:
            self.assertNotEqual(
                args[0] if args else "", "fetch",
                f"plain `_git(\"fetch\", ...)` leaked: {args!r}",
            )


class HandleResolvingConflictUsesAuthedFetchTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The conflict-resolution fetch must run inside the agent-writable
    worktree under the same security envelope as `_push_branch`: askpass-
    based auth, detached global/system config, blocked hooks/fsmonitor/
    credential helpers. `_handle_resolving_conflict` MUST route the
    fetch through `_authed_fetch` (not plain `_git`) so a planted url
    rewrite / credential helper / hooksPath cannot exfiltrate the token.
    """

    def test_fetch_call_targets_authed_fetch_with_explicit_refspec(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        issue = make_issue(450, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=850, head_branch="orchestrator/issue-450",
            head=FakePRRef(sha="cafe1234"),
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            450, pr_number=850, branch="orchestrator/issue-450",
            dev_agent="claude", dev_session_id="dev-sess",
            conflict_round=0,
        )

        merge_mock = MagicMock(return_value=(True, []))

        # The mixin's `_run` itself patches `_authed_fetch` to a default
        # success mock, so we read the call back from the returned
        # mocks dict rather than installing our own outer patch (which
        # `_run`'s inner `with` would override).
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                head_shas=["sha", "sha"],
            )

        authed_fetch_mock = mocks["_authed_fetch"]
        # Two fetches per fresh resolving_conflict round: first for the
        # PR branch (so the SHA-alignment / unpushed-recovery check sees
        # current `origin/<branch>`), then for the base branch (so the
        # upcoming `git rebase` sees current `origin/<base>`).
        self.assertEqual(authed_fetch_mock.call_count, 2)
        refspecs = [call.args[1] for call in authed_fetch_mock.call_args_list]
        cwds = [call.kwargs["cwd"] for call in authed_fetch_mock.call_args_list]
        # All fetches run inside the WORKTREE (agent-writable), where
        # the hardening actually matters -- not `target_root`.
        for cwd in cwds:
            self.assertEqual(cwd, _FAKE_WT)
        # All refspecs use the explicit `+refs/heads/X:refs/remotes/origin/X`
        # form so single-branch clones still create the remote-tracking ref.
        for refspec in refspecs:
            self.assertTrue(
                refspec.startswith("+"),
                f"refspec {refspec!r} should start with '+' for force-update",
            )
        # Verify both refs are fetched: the PR branch and the base branch.
        joined = " ".join(refspecs)
        self.assertIn(
            f"refs/remotes/origin/{_TEST_SPEC.base_branch}", joined,
            "expected base-branch fetch refspec",
        )
        self.assertIn(
            "refs/remotes/origin/orchestrator/issue-450", joined,
            "expected PR-branch fetch refspec",
        )


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


class AuthedFetchHardeningTest(unittest.TestCase):
    """`_authed_fetch` is the in-worktree authenticated fetch helper used
    by `_handle_resolving_conflict`. Mirrors `_push_branch`'s security
    envelope: askpass-based auth, detached global/system config, blocked
    hooks/fsmonitor/credential helpers, refusal to run when the worktree
    carries url-rewrite rules.
    """

    def test_env_includes_askpass_token_and_blocks_inherited_config(self) -> None:
        from unittest.mock import patch as mock_patch, MagicMock

        # First subprocess.run call is the rewrite-rule probe (returncode=1
        # = no rewrite rules); second is the real fetch -- capture its env.
        captured: dict[str, dict] = {}

        rewrite_check = MagicMock(returncode=1, stdout="", stderr="")
        fetch_result = MagicMock(returncode=0, stdout="", stderr="")

        def fake_run(args, **kwargs):
            if args and args[:3] == ["git", "config", "--local"]:
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
            if args and args[:3] == ["git", "config", "--local"]:
                return rewrite_check
            return fetch_result

        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="fake-token-xyz",
             ):
            r = workflow._authed_fetch(
                _TEST_SPEC,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )

        # Only the rewrite probe ran -- the fetch was refused.
        self.assertEqual(len(runs), 1)
        self.assertNotEqual(r.returncode, 0)

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
            r = workflow._authed_fetch(
                _TEST_SPEC, "refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )

        # No subprocess at all when the token is missing.
        self.assertEqual(runs, [])
        self.assertNotEqual(r.returncode, 0)

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
            if args and args[:3] == ["git", "config", "--local"]:
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

        other_spec = config.RepoSpec(
            slug="acme/widgets",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
        )
        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", fake_resolve
             ):
            r = workflow._authed_fetch(
                other_spec,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )
        self.assertEqual(r.returncode, 0)
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

        other_spec = config.RepoSpec(
            slug="acme/widgets",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
        )
        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", return_value=""
             ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
            r = workflow._authed_fetch(
                other_spec,
                "+refs/heads/main:refs/remotes/origin/main",
                cwd=Path("/tmp"),
            )
        # Fetch aborted before any subprocess ran.
        self.assertEqual(runs, [])
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(
            any("acme/widgets" in line for line in cm.output),
            f"expected slug 'acme/widgets' in log output, got {cm.output!r}",
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

    def test_uses_per_spec_token_and_remote_namespace_ref(self) -> None:
        # Acceptance criterion: a `REPOS` row like
        # `geserdugarov/lance-private|...|cache-branch|private` should
        # resolve its token from `~/.config/geserdugarov/lance-private/token`
        # (i.e. `spec.slug`) and write the fetched ref under
        # `refs/remotes/private/...` (i.e. `spec.remote_name`). Without
        # this split the bug surfaces as `fatal: could not read Username
        # for 'https://github.com'`.
        from unittest.mock import patch as mock_patch, MagicMock

        captured: dict[str, object] = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return MagicMock(returncode=0, stdout="", stderr="")

        resolved: list[str] = []

        def fake_resolve(slug: str) -> str:
            resolved.append(slug)
            return f"ghp-token-for-{slug.replace('/', '-')}"

        private_spec = config.RepoSpec(
            slug="geserdugarov/lance-private",
            target_root=Path("/tmp/orchestrator-test-shared-clone"),
            base_branch="cache-branch",
            remote_name="private",
        )
        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", fake_resolve,
             ):
            r = workflow._authed_target_fetch(private_spec, "cache-branch")

        self.assertEqual(r.returncode, 0)
        # Token resolved exactly once -- for the spec's slug, NOT the
        # `remote_name` (which is just a local namespace label).
        self.assertEqual(resolved, ["geserdugarov/lance-private"])
        env = captured["env"]
        self.assertEqual(
            env.get("GIT_TOKEN"), "ghp-token-for-geserdugarov-lance-private",
        )
        # Auth URL targets the spec's slug, NOT `remote_name`.
        self.assertIn(
            "https://x-access-token@github.com/geserdugarov/lance-private.git",
            captured["args"],
        )
        # The refspec writes under `refs/remotes/private/...`, NOT
        # `refs/remotes/origin/...` -- the local clone's `private` remote
        # is what the worktree creators anchor on.
        self.assertIn(
            "+refs/heads/cache-branch:refs/remotes/private/cache-branch",
            captured["args"],
        )
        # And the fetch runs in `spec.target_root` (the shared local clone).
        self.assertEqual(captured["cwd"], str(private_spec.target_root))

    def test_token_is_delivered_via_askpass_not_argv(self) -> None:
        # Same hardening as `_push_branch` / `_authed_fetch`: token in
        # GIT_TOKEN env var (read by a tempfile askpass), never in argv,
        # global/system config detached, hooks/fsmonitor/credential
        # helpers blocked.
        from unittest.mock import patch as mock_patch, MagicMock

        captured: dict[str, object] = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return MagicMock(returncode=0, stdout="", stderr="")

        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="super-secret-token",
             ):
            workflow._authed_target_fetch(_TEST_SPEC, "main")

        env = captured["env"]
        self.assertIn("GIT_ASKPASS", env)
        self.assertEqual(env.get("GIT_TOKEN"), "super-secret-token")
        # Token must NOT appear in argv (would surface in /proc/<pid>/cmdline).
        for arg in captured["args"]:
            self.assertNotIn("super-secret-token", str(arg))
        # Global/system git config detached so url rewrites planted in
        # `~/.gitconfig` cannot redirect the fetch.
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), os.devnull)
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), os.devnull)
        # Hooks / fsmonitor / credential helpers blocked via -c overrides.
        argv = captured["args"]
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("credential.helper=", argv)
        self.assertIn("core.fsmonitor=", argv)

    def test_refuses_when_target_root_has_url_rewrite_rule(self) -> None:
        # The agent has write access to linked worktrees, and a linked
        # worktree can rewrite the parent clone's local config via
        # `git config --local`. Local config still applies even with
        # GIT_CONFIG_GLOBAL/SYSTEM detached, so a planted
        # `url.https://evil.example/.insteadOf https://github.com/`
        # would redirect the token-bearing fetch to the attacker host
        # and exfiltrate GIT_TOKEN. The pre-flight check must refuse.
        from unittest.mock import patch as mock_patch, MagicMock

        rewrite_check = MagicMock(
            returncode=0,
            stdout="url.https://evil.example/.insteadof https://github.com/\n",
            stderr="",
        )
        fetch_result = MagicMock(returncode=0, stdout="", stderr="")
        runs: list = []

        def fake_run(args, **kwargs):
            runs.append(args)
            if args and args[:3] == ["git", "config", "--local"]:
                return rewrite_check
            return fetch_result

        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token",
                 return_value="super-secret-token",
             ):
            r = workflow._authed_target_fetch(_TEST_SPEC, "main")

        # Only the rewrite probe ran; the token-bearing fetch did NOT.
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][:3], ["git", "config", "--local"])
        self.assertNotEqual(r.returncode, 0)
        # And the token NEVER reached the (skipped) fetch subprocess env.
        for arg in runs[0]:
            self.assertNotIn("super-secret-token", str(arg))

    def test_missing_token_returns_failure_without_subprocess(self) -> None:
        # When the per-spec token file is missing, fail loudly with the
        # slug in the log -- a multi-repo deployment that forgot to drop
        # `~/.config/<slug>/token` gets a debuggable error rather than
        # a generic "could not read Username".
        from unittest.mock import patch as mock_patch, MagicMock

        runs: list = []

        def fake_run(args, **kwargs):
            runs.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        private_spec = config.RepoSpec(
            slug="geserdugarov/lance-private",
            target_root=Path("/tmp/orchestrator-test-shared-clone"),
            base_branch="cache-branch",
            remote_name="private",
        )
        with mock_patch("subprocess.run", side_effect=fake_run), \
             mock_patch.object(
                 workflow.config, "_resolve_github_token", return_value="",
             ), self.assertLogs(git_plumbing.log, level="ERROR") as cm:
            r = workflow._authed_target_fetch(private_spec, "cache-branch")

        # Failed without ever shelling out.
        self.assertEqual(runs, [])
        self.assertNotEqual(r.returncode, 0)
        # Slug is in the log so the operator knows which token file to fix.
        self.assertTrue(
            any("geserdugarov/lance-private" in line for line in cm.output),
            f"expected slug in log output, got {cm.output!r}",
        )


class ListPollableIssuesIncludesResolvingConflictTest(unittest.TestCase):
    """An external merge can land while the orchestrator is mid-resolution:
    `Resolves #N` closes the issue, but the orchestrator must still poll
    closed-but-`resolving_conflict` issues so `_handle_resolving_conflict`'s
    terminal `pr_status == "merged"` branch can finalize to `done`.
    """

    def test_closed_resolving_conflict_issue_is_polled(self) -> None:
        gh = FakeGitHubClient()
        # Close an issue still labeled `resolving_conflict` (mirrors
        # GitHub auto-closing via `Resolves #N` after a human merge).
        issue = make_issue(900, label="resolving_conflict")
        issue.closed = True
        gh.add_issue(issue)

        polled = list(gh.list_pollable_issues())
        self.assertIn(issue, polled)

    def test_closed_in_review_issue_still_polled(self) -> None:
        # Regression: extending the sweep must NOT drop the existing
        # closed-in_review path.
        gh = FakeGitHubClient()
        issue = make_issue(901, label="in_review")
        issue.closed = True
        gh.add_issue(issue)

        polled = list(gh.list_pollable_issues())
        self.assertIn(issue, polled)

    def test_closed_unrelated_label_is_not_polled(self) -> None:
        # Closed issues with neither `in_review` nor `resolving_conflict`
        # must stay out of the sweep so it does not balloon.
        gh = FakeGitHubClient()
        issue = make_issue(902, label="done")
        issue.closed = True
        gh.add_issue(issue)

        polled = list(gh.list_pollable_issues())
        self.assertNotIn(issue, polled)


class HandleResolvingConflictDispatchTest(unittest.TestCase):
    """The dispatcher must route `resolving_conflict` to the dedicated
    handler -- this is a label-rollout regression check that survives
    the placeholder being replaced by the real implementation."""

    def test_dispatcher_routes_resolving_conflict_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(42, label="resolving_conflict")
        gh.add_issue(issue)

        with patch.object(
            workflow, "_handle_resolving_conflict"
        ) as handler:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)


class HandleResolvingConflictTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Drive `_handle_resolving_conflict` through the rebase / push / cap /
    PR-state branches with `_git`, `_rebase_base_into_worktree`, and the
    push helper mocked so no shell-out happens.
    """

    BRANCH = "orchestrator/issue-200"
    PR_NUMBER = 800

    def _seed(
        self,
        *,
        merge_succeeded: bool = True,
        conflicted_files=(),
        head_shas=("before", "after"),
        push_branch: bool = True,
        run_agent_result=None,
        pr_state: str = "open",
        pr_merged: bool = False,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(200, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=False, check_state="success",
            merged=pr_merged, state=pr_state,
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=2,
            conflict_round=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(200, **state)
        return gh, issue, pr

    def _run_with_merge(
        self,
        gh,
        issue,
        *,
        merge_succeeded: bool,
        conflicted_files=(),
        head_shas=("before", "after"),
        push_branch: bool = True,
        run_agent_result=None,
        fetch_returncode: int = 0,
        dirty_files=(),
        rebase_in_progress: bool = False,
    ):
        from unittest.mock import MagicMock

        agent = run_agent_result or _agent(
            session_id="dev-sess", last_message="resolved",
        )
        merge_mock = MagicMock(
            return_value=(merge_succeeded, list(conflicted_files))
        )
        fetch_result = MagicMock(returncode=fetch_returncode, stdout="", stderr="")
        # `_git_hardened` is what the fetch in `_handle_resolving_conflict`
        # actually calls; `_git` covers the diff helper inside the merge
        # wrapper. Both must be mocked or the real subprocess.run() fires
        # on `_FAKE_WT`.
        git_mock = MagicMock(return_value=fetch_result)
        git_hardened_mock = MagicMock(return_value=fetch_result)
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_hardened_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=agent,
                push_branch=push_branch,
                head_shas=head_shas,
                dirty_files=dirty_files,
                rebase_in_progress=rebase_in_progress,
            )
        return mocks, merge_mock, git_mock

    def test_clean_rebase_pushes_and_flips_to_validating(self) -> None:
        # A clean base rebase that actually moved HEAD pushes the
        # rebased branch and hands straight back to `validating`. Docs
        # do not run here -- the single docs pass runs after reviewer
        # approval before `in_review` via the final-docs handoff.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent must NOT be spawned -- a clean base rebase does not need
        # the dev to do anything.
        mocks["run_agent"].assert_not_called()
        merge_mock.assert_called_once()
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease="beforehead",
        )
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)

    def test_hold_base_sync_label_pauses_resolving_conflict(self) -> None:
        gh, issue, pr = self._seed()
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(200)
        self.assertEqual(data.get("conflict_round"), 0)
        self.assertFalse(data.get("awaiting_human"))

    def test_clean_rebase_already_up_to_date_skips_push_and_ticks_round(
        self,
    ) -> None:
        # When the base hasn't moved (e.g. unmergeability is purely due to
        # branch protection), the rebase is a no-op and there is nothing to
        # push. The handler must still increment `conflict_round` so the
        # cap eventually fires -- otherwise the in_review <-> resolving
        # cycle would loop forever. The label hands back to `validating`
        # so the next reviewer round / in_review tick can re-evaluate;
        # every other resolving_conflict exit also targets `validating`
        # now, so there's no `documenting` detour to skip relative to
        # the pushed paths.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
        )
        mocks["run_agent"].assert_not_called()
        # Nothing to push when base hasn't moved relative to the branch.
        mocks["_push_branch"].assert_not_called()
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)

    def test_no_op_rebase_loops_until_cap_fires(self) -> None:
        # A PR stuck unmergeable purely due to branch protection would
        # bounce between in_review and resolving_conflict with the rebase
        # always a no-op. The cap must fire after MAX_CONFLICT_ROUNDS
        # such no-op rounds.
        gh, issue, pr = self._seed(extra_state={"conflict_round": 2})
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks, merge_mock, git_mock = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,
                head_shas=["samehead", "samehead"],
                push_branch=True,
            )
        # One more no-op round consumed: 2 -> 3.
        self.assertEqual(gh.pinned_data(200).get("conflict_round"), 3)
        # On the next tick we'd be at the cap; simulate by re-running:
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks2, merge_mock2, _ = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,
                head_shas=["samehead", "samehead"],
                push_branch=True,
            )
        merge_mock2.assert_not_called()
        self.assertTrue(gh.pinned_data(200).get("awaiting_human"))

    def test_conflict_resolved_by_agent_pushes_and_flips_to_validating(
        self,
    ) -> None:
        # Agent-resolved conflict push pushes the resolved branch and
        # hands straight back to `validating`. Docs do not run here --
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py", "b.py"],
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent IS spawned with the conflict-resolution prompt.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("a.py", prompt)
        self.assertIn("b.py", prompt)
        self.assertIn("rebase", prompt.lower())
        self.assertIn("git rebase --skip", prompt)
        self.assertIn("git commit --allow-empty", prompt)
        self.assertIn("git rebase --abort", prompt)
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease="beforehead",
        )
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)

    def test_cap_exhausted_parks_awaiting_human(self) -> None:
        # `MAX_CONFLICT_ROUNDS` defaults to 3; once the counter reaches it,
        # the handler must park instead of attempting another round.
        gh, issue, pr = self._seed(extra_state={"conflict_round": 3})
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks, merge_mock, git_mock = self._run_with_merge(
                gh, issue, merge_succeeded=True,
            )
        # Neither merge nor agent runs on the cap branch.
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # Label stays on `resolving_conflict` -- no flip.
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "done"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("MAX_CONFLICT_ROUNDS", last_comment)

    def test_pr_already_merged_externally_finalizes_to_done(self) -> None:
        # Mirror the in_review terminal: a human merged the PR (perhaps
        # after manually resolving conflicts) while we were resolving.
        gh, issue, pr = self._seed(pr_merged=True, pr_state="closed")
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        # No merge / agent / push attempt -- terminal short-circuit.
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((200, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(200))
        self.assertTrue(issue.closed)

    def test_pr_closed_unmerged_finalizes_to_rejected(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed")
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertIn((200, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(200))
        # PR is gone -- the orchestrator-owned branch and worktree must
        # come down on the rejected terminal too, mirroring the merged
        # path. Failure to clean up here is exactly the bug this test
        # guards against.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 200,
        )

    def test_manually_closed_with_open_pr_marks_rejected_without_cleanup(
        self,
    ) -> None:
        # Mirror the in_review counterpart: closing the issue while the
        # PR is still open is a human stop signal. The handler flips the
        # label to `rejected` but deliberately leaves the branch /
        # worktree alone (operator may still want to salvage the PR).
        gh, issue, pr = self._seed(pr_state="open")
        issue.closed = True
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertIn((200, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(200))
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Documented caveat: a subsequent PR close is not observed by
        # the orchestrator -- the closed-issue sweep only covers
        # `in_review` / `resolving_conflict`, and `rejected` is terminal
        # in the dispatcher. Operator must clean up by hand.
        pr.state = "closed"
        pollable_numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertNotIn(
            200, pollable_numbers,
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_agent_timeout_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", timed_out=True,
            ),
        )
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # Label stays on resolving_conflict -- the dispatcher will keep
        # routing here until the operator clears the park.
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("timed out", last_comment)

    def test_agent_left_dirty_worktree_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(False, ["a.py"]))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        # Note: the mixin's `_run` patches `_worktree_dirty_files` itself,
        # so wire dirty_files through the kwarg rather than a separate
        # outer patch (which `_run`'s patch would override).
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(
                    session_id="dev-sess", last_message="halfway there",
                ),
                push_branch=True,
                head_shas=["beforehead", "after"],
                dirty_files=["a.py"],
            )

        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_agent_left_rebase_in_progress_parks_without_push(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            push_branch=True,
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="I resolved one stop but another remains",
            ),
            rebase_in_progress=True,
        )

        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rebase is still in progress", last_comment)
        self.assertIn("I resolved one stop", last_comment)

    def test_push_failure_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "merged"],
            push_branch=False,
        )
        # Agent ran successfully and committed, but the push failed.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # No label flip -- still resolving_conflict.
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_awaiting_human_no_new_comments_is_quiet(self) -> None:
        # Once parked, ticks without a new human reply must not retry --
        # otherwise the cap is meaningless and a poisoned rebase would
        # burn tokens. The parked state stays put.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                # Watermark above any comment so `comments_after` is empty.
                "last_action_comment_id": 999_999,
            },
        )
        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_awaiting_human_with_new_comment_resumes_dev(self) -> None:
        # `_on_question` / `_on_dirty_worktree` parks tell the human
        # "reply with guidance and the orchestrator will resume the
        # session". Honor that contract: a fresh comment past the
        # watermark must resume the dev on the in-progress rebase
        # worktree, NOT keep the issue stuck until a manual relabel.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        # Fresh comment above the watermark.
        issue.comments.append(
            FakeComment(
                id=2000, body="try harder; conflict in foo.py is structural",
                user=FakeUser("alice"),
            )
        )

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,  # unused on resume path
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )

        # Resume runs the agent with the human's text; rebase is NOT
        # re-attempted (the worktree is mid-rebase already).
        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("try harder", prompt)
        merge_mock.assert_not_called()
        # Successful resume pushes the branch and hands straight back
        # to `validating`. Docs do not run here -- the single docs pass
        # runs after reviewer approval before `in_review` via the
        # final-docs handoff.
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease=None,
        )
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 2)
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        # Watermark advanced past the consumed comment.
        self.assertEqual(data.get("last_action_comment_id"), 2000)

    def test_awaiting_human_resume_recovers_from_stale_claude_session(self) -> None:
        # Regression: a `resolving_conflict` issue parked awaiting human
        # whose pinned `dev_session_id` references a Claude transcript that
        # no longer exists. The first `--resume <sid>` call comes back with
        # `No conversation found with session ID` on stderr and empty
        # stdout. Without immediate detection the resume would surface as
        # an `agent_silent` park, the silent-park counter would tick to 1
        # (still below the threshold), and the human would have to comment
        # twice more before recovery. With the fix, `_resume_dev_with_text`
        # transparently retries with a fresh spawn in the same worktree;
        # the rebase commit produced by the retry pushes and the issue
        # flips back to validating in a single tick.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
                "dev_session_id": "poisoned-sess",
            },
        )
        issue.comments.append(
            FakeComment(
                id=2000, body="please retry the conflict resolution",
                user=FakeUser("alice"),
            )
        )

        stale_stderr = "Error: No conversation found with session ID: poisoned-sess"

        calls: list = []

        def fake_run(agent, prompt, wt, *, resume_session_id=None, extra_args=()):
            calls.append(resume_session_id)
            if resume_session_id == "poisoned-sess":
                return _agent(
                    session_id="", last_message="", stderr=stale_stderr,
                )
            return _agent(session_id="fresh-sess", last_message="resolved")

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=fake_run,
                push_branch=True,
                head_shas=["beforehead", "merged"],
            )

        # Two run_agent calls: the poisoned resume + the fresh-spawn retry.
        self.assertEqual(
            calls, ["poisoned-sess", None],
            "stale-session resume must be transparently retried as fresh",
        )
        # Successful retry pushes the branch and hands straight back to
        # `validating` WITHOUT parking agent_silent; the single docs
        # pass is deferred to the post-approval hop.
        mocks["_push_branch"].assert_called_once()
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertFalse(
            data.get("awaiting_human"),
            "awaiting_human must be cleared on a recovered resume",
        )
        self.assertNotEqual(data.get("park_reason"), "agent_silent")
        self.assertEqual(data.get("conflict_round"), 2)
        self.assertEqual(data.get("dev_session_id"), "fresh-sess")

    def test_awaiting_human_resume_with_question_parks_again(self) -> None:
        # Resumed agent that produces no new commit (asks another
        # question) must re-park rather than push or flip the label.
        gh, issue, pr = self._seed(
            extra_state={
                "awaiting_human": True,
                "conflict_round": 1,
                "last_action_comment_id": 1000,
            },
        )
        issue.comments.append(
            FakeComment(
                id=2000, body="try harder",
                user=FakeUser("alice"),
            )
        )

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            # Same SHA before and after -- agent did nothing.
            head_shas=["samehead", "samehead"],
            push_branch=True,
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="I still need clarification on bar.py",
            ),
        )

        mocks["run_agent"].assert_called_once()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        # Re-parked: counter unchanged, no label flip.
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertTrue(data.get("awaiting_human"))

    def test_no_pr_number_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(201, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(201)

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(201).get("awaiting_human"))

    def test_unpushed_local_commits_pushed_on_recovery(self) -> None:
        # Crash recovery: a previous tick committed a conflict resolution
        # but crashed before `_push_branch` returned (or before the post-
        # push state write landed). The next tick must push the local
        # commit and complete the round, NOT treat it as "no work needed"
        # and flip to validating with the resolution unpushed.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                # HEAD ahead of `origin/<branch>` by one commit (the
                # unpushed resolution); not behind.
                branch_ahead_behind=(1, 0),
            )
        # Recovered work pushed; rebase NOT attempted (we already have a
        # resolution waiting to ship).
        mocks["_push_branch"].assert_called_once()
        merge_mock.assert_not_called()
        # No agent spawn -- the recovery is a pure push, the dev already
        # produced the commit on the previous tick.
        mocks["run_agent"].assert_not_called()
        # Round completed: counter incremented, label flipped, marker
        # stamped exactly as on the happy-path resolve. The recovered
        # push hands straight back to `validating`; the single docs
        # pass is deferred to the post-approval hop.
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)

    def test_stale_worktree_parks_awaiting_human(self) -> None:
        # Worktree behind `origin/<branch>` (someone pushed to the PR
        # branch out-of-band). Force-pushing the local state would
        # clobber the real PR head; refuse and park.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(0, 2),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("stale or diverged", last_comment)

    def test_diverged_worktree_parks_awaiting_human(self) -> None:
        # Both ahead and behind: histories diverged. Cannot safely push
        # without rewriting remote history that may have value.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 1),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_unpushed_recovery_push_failure_parks(self) -> None:
        # Recovery push fails (e.g. force-with-lease lease miss because
        # the remote actually moved). Park rather than silently flipping
        # to validating with an unsynced local SHA.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=False,
                branch_ahead_behind=(1, 0),
            )
        mocks["_push_branch"].assert_called_once()
        merge_mock.assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_dirty_recovered_commits_parks_without_push(self) -> None:
        # Crash recovery with leftover dirty files: a previous tick
        # committed a resolution but ALSO left uncommitted edits, then
        # crashed before the dirty check ran. Pushing now would publish
        # a SHA that silently omits the leftover edits, and the reviewer
        # at validating would later run on a tree that does not match
        # the PR. Park instead.
        gh, issue, pr = self._seed()

        from unittest.mock import MagicMock
        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 0),
                dirty_files=["leftover.py"],
            )
        # No push, no merge attempt, no label flip.
        mocks["_push_branch"].assert_not_called()
        merge_mock.assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted", last_comment)

    def test_dirty_clean_rebase_with_new_commit_parks_without_push(self) -> None:
        # Clean rebase produced a new HEAD but the
        # worktree carries pre-existing dirty files. Pushing the merge
        # rebased branch without those edits would publish an incomplete branch.
        gh, issue, pr = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))

    def test_dirty_clean_rebase_no_op_parks_without_flip(self) -> None:
        # Clean no-op rebase (HEAD didn't change because base hadn't
        # moved) but the worktree carries dirty files. The reviewer
        # at validating reads the worktree directly, so flipping with a
        # dirty tree would let the agent vote on something that does NOT
        # match the PR head. Park instead.
        gh, issue, pr = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))


class HandleResolvingConflictHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `resolving_conflict` is dispatched per tick too,
    so a body edit while the dev is resolving conflicts must surface to
    the dev. Mirrors the in_review pattern: post a PR notice and resume."""

    def test_drift_posts_pr_notice_and_resumes_dev(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            500, label="resolving_conflict", body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=5000, head_branch="orchestrator/issue-500")
        gh.add_pr(pr)
        gh.seed_state(
            500,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            conflict_round=0,
            branch="orchestrator/issue-500",
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="resolved with edit"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            # Three SHAs: drift before/after for the post-resume head
            # delta, plus the third for the `conflict_round` audit emit
            # that records the pushed worktree HEAD.
            head_shas=["before", "after", "after"],
        )

        # Pushed drift fix -> hand straight back to `validating`; the
        # single docs pass is deferred to the post-approval hop.
        self.assertIn((500, "validating"), gh.label_history)
        self.assertNotIn((500, "documenting"), gh.label_history)
        # Notice posted on the PR.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_pr_comments
        ))
