# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import agents, config, verify, workflow

from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


def shutil_quote(s: str) -> str:
    """Local shell-quote helper for the truncate test -- avoids importing
    `shlex` at module scope when it is only used by one test."""
    import shlex
    return shlex.quote(s)


class HandleValidatingVerifyGateTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Local verification gate that runs in the per-issue worktree on
    `VERDICT: APPROVED`, before the issue is labeled `in_review`. Default-
    empty `VERIFY_COMMANDS` keeps the legacy behaviour; a non-empty config
    runs each command sequentially with a bounded timeout and parks the
    issue in `validating` on any failure (non-zero exit, timeout, or a
    dirty tree left behind).
    """

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(7, label="validating")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=21,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-7",
            codex_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(7, **defaults)
        return gh, issue

    def test_default_empty_verify_is_a_noop_on_approval(self) -> None:
        # With no `VERIFY_COMMANDS` configured, the gate short-circuits
        # to ok inside the runner; the helper is still called once (so a
        # future config flip toggles the gate without code changes), but
        # the approval / squash / in_review handoff path is unchanged.
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("rev-sha",),
        )

        self.assertEqual(mocks["_run_verify_commands"].call_count, 1)
        # The configured commands tuple was forwarded verbatim --
        # default-empty means the runner sees ().
        call = mocks["_run_verify_commands"].call_args
        self.assertEqual(call.args[1], config.VERIFY_COMMANDS)
        self.assertEqual(config.VERIFY_COMMANDS, ())
        # Handoff completed normally through the final-docs hop.
        self.assertIn((7, "documenting"), gh.label_history)
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_config_parses_semicolon_and_newline_separated_commands(self) -> None:
        # `_parse_verify_commands` accepts both `;` and `\n` separators so
        # the value fits on one line in a `.env` file. Blank lines and
        # `#`-commented lines are skipped.
        from orchestrator.config import _parse_verify_commands

        self.assertEqual(_parse_verify_commands(""), ())
        self.assertEqual(
            _parse_verify_commands("pytest -q;ruff check ."),
            ("pytest -q", "ruff check ."),
        )
        self.assertEqual(
            _parse_verify_commands("pytest -q\nruff check .\n"),
            ("pytest -q", "ruff check ."),
        )
        self.assertEqual(
            _parse_verify_commands("\n#comment\npytest -q\n\n"),
            ("pytest -q",),
        )

    def test_verify_success_keeps_existing_approval_flow(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=VerifyResult(status="ok"),
            )

        mocks["_run_verify_commands"].assert_called_once()
        # Approval comment posted; label flipped to `documenting` (the
        # final-docs hop).
        self.assertTrue(any(
            ":white_check_mark:" in body
            for _, body in gh.posted_pr_comments
        ))
        self.assertIn((7, "documenting"), gh.label_history)
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))

    def test_verify_failed_parks_with_verify_failed_reason(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="failed",
            command="pytest -q",
            exit_code=2,
            output="E   AssertionError: bad\nTAIL_MARKER",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_failed")
        # No in_review or documenting handoff -- the verify gate fires
        # BEFORE the approval / squash / final-docs route is reached.
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        # No approval comment (gate fires BEFORE the approval post).
        self.assertFalse(any(
            ":white_check_mark:" in body
            for _, body in gh.posted_pr_comments
        ))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("local verification failed", last_comment)
        self.assertIn("pytest -q", last_comment)
        self.assertIn("exited with code 2", last_comment)
        self.assertIn("TAIL_MARKER", last_comment)

    def test_verify_timeout_parks_with_verify_timeout_reason(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="timeout",
            command="pytest --slow",
            exit_code=None,
            output="hanging...",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest --slow",)), \
             patch.object(config, "VERIFY_TIMEOUT", 123):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_timeout")
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("pytest --slow", last_comment)
        self.assertIn("timed out after 123s", last_comment)

    def test_verify_head_changed_parks_with_verify_head_changed_reason(self) -> None:
        # End-to-end: a verify command that moved HEAD must NOT flow
        # through to `in_review` -- otherwise squash-on-approval would
        # push the unreviewed commit. The handler parks the issue with a
        # distinct `verify_head_changed` reason so the operator can
        # adjudicate whether the auto-commit belongs in the PR.
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="head_changed",
            command="sh -c 'git commit -am autofix'",
            exit_code=0,
            output="",
            head_before="aaaa1111",
            head_after="bbbb2222",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("sh -c 'git commit -am autofix'",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_head_changed")
        # No in_review / documenting handoff and no approval / squash
        # side effects.
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        self.assertFalse(any(
            ":white_check_mark:" in body
            for _, body in gh.posted_pr_comments
        ))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("moved HEAD", last_comment)
        # Short SHAs are surfaced so the operator can identify the commit.
        self.assertIn("aaaa1111", last_comment)
        self.assertIn("bbbb2222", last_comment)

    def test_verify_dirty_worktree_parks(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify = VerifyResult(
            status="dirty",
            command="pytest -q",
            exit_code=0,
            dirty_files=("build/artifact.bin", "tests/cache"),
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
                head_shas=("rev-sha",),
                verify_result=verify,
            )

        data = gh.pinned_data(7)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "verify_dirty")
        self.assertNotIn((7, "in_review"), gh.label_history)
        self.assertNotIn((7, "documenting"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("build/artifact.bin", last_comment)

    def test_changes_requested_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        review = _agent(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")
        # The verify mock should not be called -- assert by setting a
        # failing result that would otherwise park the issue.
        verify_fail = VerifyResult(
            status="failed", command="pytest -q", exit_code=1, output="bad",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                verify_result=verify_fail,
            )

        mocks["_run_verify_commands"].assert_not_called()
        # Standard CHANGES_REQUESTED handling: PR review comment + dev resume.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(gh.pinned_data(7).get("review_round"), 1)
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))

    def test_unknown_verdict_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        from orchestrator.worktrees import VerifyResult
        verify_fail = VerifyResult(
            status="failed", command="pytest -q", exit_code=1, output="bad",
        )
        with patch.object(config, "VERIFY_COMMANDS", ("pytest -q",)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    last_message="I'm not sure what to think.",
                ),
                verify_result=verify_fail,
            )

        mocks["_run_verify_commands"].assert_not_called()
        data = gh.pinned_data(7)
        # Park comes from the unknown-verdict path, NOT the verify gate;
        # confirm by checking the comment text (the unknown-verdict park
        # does not persist `park_reason` to pinned state for the
        # non-silent-crash case).
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn(data.get("park_reason"), ("verify_failed", "verify_timeout", "verify_dirty"))
        self.assertIn("did not emit a VERDICT line", gh.posted_comments[-1][1])


class RunVerifyCommandsTest(unittest.TestCase):
    """Direct tests for the verify-command runner against a real shell."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Initialize a git repo so the dirty-detection branch works.
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.tmp)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.email", "t@t"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.name", "t"],
            check=True,
        )
        (self.tmp / "seed").write_text("x")
        subprocess.run(
            ["git", "-C", str(self.tmp), "add", "."], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "commit", "-q", "-m", "seed"],
            check=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_commands_short_circuits_to_ok(self) -> None:
        r = workflow._run_verify_commands(self.tmp, (), 60)
        self.assertEqual(r.status, "ok")
        self.assertIsNone(r.command)

    def test_all_commands_pass_returns_ok(self) -> None:
        r = workflow._run_verify_commands(
            self.tmp, ("true", "echo hello"), 60,
        )
        self.assertEqual(r.status, "ok")

    def test_non_zero_exit_returns_failed_with_first_failing_command(self) -> None:
        r = workflow._run_verify_commands(
            self.tmp,
            ("true", "sh -c 'echo boom 1>&2; exit 3'", "true"),
            60,
        )
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.command, "sh -c 'echo boom 1>&2; exit 3'")
        self.assertEqual(r.exit_code, 3)
        self.assertIn("boom", r.output)

    def test_timeout_returns_timeout_with_partial_output(self) -> None:
        # `sleep 5` against a 1s timeout fires `TimeoutExpired`.
        r = workflow._run_verify_commands(
            self.tmp, ("sleep 5",), timeout=1,
        )
        self.assertEqual(r.status, "timeout")
        self.assertEqual(r.command, "sleep 5")
        self.assertIsNone(r.exit_code)

    def test_timeout_kills_full_process_group(self) -> None:
        # Regression: `subprocess.run(..., shell=True, timeout=...)`
        # only SIGKILLs the shell, leaving its background descendants
        # (`& subshells`, `make -j` workers, pytest-xdist forkers...)
        # alive to keep mutating the worktree after `_run_verify_commands`
        # has already returned `verify_timeout` and the orchestrator has
        # parked the issue. The runner now puts each command in its own
        # process group via `start_new_session=True` and `killpg`s the
        # group on timeout. Verified by having the verify command spawn
        # a background process that would touch a sentinel file AFTER
        # the timeout would have fired -- with the group-kill it never
        # gets to.
        marker = self.tmp / "post_timeout_marker.txt"
        # Background subshell sleeps 2s then touches the marker. Parent
        # shell sleeps 10s so the 1s timeout definitely fires. If the
        # group-kill works, the background subshell dies before its
        # sleep finishes and the marker is never created.
        cmd = (
            f"(sleep 2 && touch {marker}) & sleep 10"
        )
        r = workflow._run_verify_commands(self.tmp, (cmd,), timeout=1)
        self.assertEqual(r.status, "timeout")
        # Wait well past when the background touch would have fired.
        # 3s gives the background its full 2s + 1s of slack.
        import time
        time.sleep(3)
        self.assertFalse(
            marker.exists(),
            f"background process survived timeout-kill; {marker} was created",
        )

    def test_dirty_tree_after_success_returns_dirty(self) -> None:
        # Command exits 0 but leaves an untracked file behind.
        r = workflow._run_verify_commands(
            self.tmp, ("sh -c 'echo leak > leftover.txt'",), 60,
        )
        self.assertEqual(r.status, "dirty")
        self.assertIn("leftover.txt", r.dirty_files)

    def test_output_truncated_to_budget(self) -> None:
        big = "X" * 10000 + "TAIL"
        r = workflow._run_verify_commands(
            self.tmp,
            (f"sh -c 'printf %s {shutil_quote(big)}; exit 1'",),
            60,
        )
        self.assertEqual(r.status, "failed")
        # Tail preserved, leading bulk trimmed.
        self.assertIn("TAIL", r.output)
        self.assertLessEqual(len(r.output), 4096)

    def test_secret_straddling_truncation_boundary_is_fully_redacted(self) -> None:
        # Regression: `_redact_secrets` does `str.replace(value, "***")`
        # on the full value, so a secret whose bytes straddle the
        # truncation cut would no longer match a post-truncation replace
        # and would leak a partial value verbatim in the park comment.
        # The fix runs the redact pass BEFORE truncating so any matched
        # secret collapses to `***` before its bytes can be sliced.
        secret = "SUPERSECRET-TOKEN-VALUE-0123456789ABCDEF"  # 40 chars
        # Engineer the payload so the truncation cut (last 4096 bytes)
        # falls inside the secret rather than before it. Budget = 4096;
        # we want secret_start < (total - 4096) < secret_end so the
        # naive "truncate-then-redact" path would leak the secret's tail.
        prefix = "P" * 90
        # total = 4200 → cut at byte 104; secret occupies 90..129, so
        # bytes 14..39 of the secret (`E-0123456789ABCDEF`) would survive
        # a naive truncation.
        suffix_len = 4200 - len(prefix) - len(secret)
        suffix = "S" * suffix_len
        payload = prefix + secret + suffix
        self.assertEqual(len(payload), 4200)
        cut = len(payload) - 4096
        self.assertLess(payload.index(secret), cut)
        self.assertGreater(payload.index(secret) + len(secret), cut)

        import os as _os
        import shlex
        cmd = f"sh -c 'printf %s {shlex.quote(payload)}; exit 1'"
        with patch.dict(_os.environ, {"VERIFY_TEST_API_KEY": secret}):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        # The full secret must be gone -- baseline check.
        self.assertNotIn(secret, r.output)
        # And no 8+ char substring of the secret survives either.
        # Length 8 matches `_REDACT_MIN_VALUE_LEN`: shorter accidental
        # collisions are below the redaction threshold and tolerable.
        for start in range(len(secret) - 7):
            self.assertNotIn(
                secret[start:start + 8], r.output,
                f"partial secret substring leaked: {secret[start:start + 8]!r}",
            )
        # And the redaction marker is present (proves the runner
        # actually saw and replaced the secret).
        self.assertIn("***", r.output)

    def test_github_token_stripped_from_verify_environment(self) -> None:
        # Regression: verify commands run in the per-issue worktree
        # against code the implementer agent just produced. If the
        # runner inherited the orchestrator's process env, a prompt-
        # injected `pytest` plugin (or a hostile dependency) could read
        # `$GITHUB_TOKEN` and push or call the GitHub API as us. The
        # runner now strips via `_filter_agent_env`, mirroring what
        # `_agent_env` does for the implementer / reviewer subprocesses.
        cmd = (
            # `printenv GITHUB_TOKEN` prints the value if the var is in
            # the child env and exits 0; if unset, it prints nothing and
            # exits 1. We pipe both branches through `exit 1` so the
            # runner reports the verify as failed and we can inspect
            # `r.output` either way.
            "sh -c 'echo TOKEN_PRESENT=$([ -n \"$GITHUB_TOKEN\" ] && "
            "echo YES || echo NO); exit 1'"
        )
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ghp_ORCHESTRATOR_PAT_SHOULD_NOT_LEAK"},
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        # The verify environment must NOT carry GITHUB_TOKEN through.
        self.assertIn("TOKEN_PRESENT=NO", r.output)
        # And the original token value must not appear verbatim. (This
        # also catches a regression where redaction were doing the heavy
        # lifting instead of env stripping -- redaction would mask the
        # value with `***`, but the variable would still have been
        # exposed to the verify command.)
        self.assertNotIn("ghp_ORCHESTRATOR_PAT_SHOULD_NOT_LEAK", r.output)

    def test_write_credential_locators_stripped_from_verify_environment(self) -> None:
        # Issue #213 review: SSH-agent socket, askpass binaries, and
        # `GIT_SSH_COMMAND` are write-credential pointers, not secret-
        # shaped values. Leaving them in the verify shell lets a
        # hostile dependency forward through the operator's loaded
        # ssh-agent (and push to any host whose key is loaded) or
        # invoke the operator's askpass binary in their session.
        cmd = (
            "sh -c '"
            "echo SSH_AUTH=$([ -n \"$SSH_AUTH_SOCK\" ] && echo YES || echo NO); "
            "echo SSH_ASK=$([ -n \"$SSH_ASKPASS\" ] && echo YES || echo NO); "
            "echo GIT_ASK=$([ -n \"$GIT_ASKPASS\" ] && echo YES || echo NO); "
            "echo GIT_SSH=$([ -n \"$GIT_SSH_COMMAND\" ] && echo YES || echo NO); "
            "exit 1'"
        )
        with patch.dict(
            os.environ,
            {
                "SSH_AUTH_SOCK": "/tmp/ssh-test/agent.42",
                "SSH_ASKPASS": "/usr/lib/ssh/ssh-askpass",
                "GIT_ASKPASS": "/usr/share/git/askpass-helper",
                "GIT_SSH_COMMAND": "ssh -i /home/op/.ssh/deploy-key",
            },
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        self.assertIn("SSH_AUTH=NO", r.output)
        self.assertIn("SSH_ASK=NO", r.output)
        self.assertIn("GIT_ASK=NO", r.output)
        self.assertIn("GIT_SSH=NO", r.output)
        # The locator values must not survive verbatim anywhere.
        self.assertNotIn("/tmp/ssh-test/agent.42", r.output)
        self.assertNotIn("/home/op/.ssh/deploy-key", r.output)

    def test_credential_file_locators_stripped_from_verify_environment(self) -> None:
        # Issue #213 review: credential-file LOCATORS (env vars whose
        # value is a path to a file holding the secret) must also be
        # stripped. The verify shell runs as the same OS user as the
        # orchestrator, so leaving `ORCHESTRATOR_TOKEN_FILE` /
        # `GOOGLE_APPLICATION_CREDENTIALS` / `AWS_SHARED_CREDENTIALS_FILE`
        # in the child env lets a hostile dependency simply `cat` the
        # pointer's target. The `ORCHESTRATOR_TOKEN_FILE` strip is the
        # most important case: it points at the orchestrator's own
        # write-credential file.
        cmd = (
            "sh -c '"
            "echo ORCH_TF=$([ -n \"$ORCHESTRATOR_TOKEN_FILE\" ] && "
            "echo YES || echo NO); "
            "echo GAC=$([ -n \"$GOOGLE_APPLICATION_CREDENTIALS\" ] && "
            "echo YES || echo NO); "
            "echo AWS_SCF=$([ -n \"$AWS_SHARED_CREDENTIALS_FILE\" ] && "
            "echo YES || echo NO); "
            "exit 1'"
        )
        with patch.dict(
            os.environ,
            {
                "ORCHESTRATOR_TOKEN_FILE": "/etc/secrets/orch-token-path",
                "GOOGLE_APPLICATION_CREDENTIALS": "/etc/secrets/gcp.json",
                "AWS_SHARED_CREDENTIALS_FILE": "/etc/secrets/aws-creds",
            },
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        self.assertIn("ORCH_TF=NO", r.output)
        self.assertIn("GAC=NO", r.output)
        self.assertIn("AWS_SCF=NO", r.output)
        # And the locator path itself must not survive verbatim either
        # (env strip, not redaction-only).
        self.assertNotIn("/etc/secrets/orch-token-path", r.output)
        self.assertNotIn("/etc/secrets/gcp.json", r.output)
        self.assertNotIn("/etc/secrets/aws-creds", r.output)

    def test_production_secret_shapes_stripped_from_verify_environment(self) -> None:
        # Issue #213: GitHub-token aliases are not the only credential
        # shape that should not be inherited by operator-configured
        # verify shell. Production-secret-shaped variables (suffix or
        # bare-name matches) must be stripped too. The verify runner
        # ALSO strips the agent's provider-auth keys
        # (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) -- unlike the agent
        # subprocess case, where the allowlist preserves them, the
        # verify shell executes untrusted agent-produced code and a
        # hostile dependency reading `$ANTHROPIC_API_KEY` would gain
        # billable access to the operator's model account.
        cmd = (
            "sh -c 'echo STRIPE_PRESENT=$([ -n \"$STRIPE_API_KEY\" ] && "
            "echo YES || echo NO); "
            "echo DBPW_PRESENT=$([ -n \"$DATABASE_PASSWORD\" ] && "
            "echo YES || echo NO); "
            "echo DEPLOY_PRESENT=$([ -n \"$DEPLOY_TOKEN\" ] && "
            "echo YES || echo NO); "
            "echo ANTH_PRESENT=$([ -n \"$ANTHROPIC_API_KEY\" ] && "
            "echo YES || echo NO); "
            "echo OPENAI_PRESENT=$([ -n \"$OPENAI_API_KEY\" ] && "
            "echo YES || echo NO); exit 1'"
        )
        with patch.dict(
            os.environ,
            {
                "STRIPE_API_KEY": "sk_live_VERY_SECRET_SHOULD_NOT_LEAK",
                "DATABASE_PASSWORD": "hunter2_should_not_leak",
                "DEPLOY_TOKEN": "deploytok_should_not_leak",
                "ANTHROPIC_API_KEY": "sk-ant-SHOULD_NOT_LEAK_TO_VERIFY",
                "OPENAI_API_KEY": "sk-oai-SHOULD_NOT_LEAK_TO_VERIFY",
            },
        ):
            r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "failed")
        self.assertIn("STRIPE_PRESENT=NO", r.output)
        self.assertIn("DBPW_PRESENT=NO", r.output)
        self.assertIn("DEPLOY_PRESENT=NO", r.output)
        # Provider auth is stripped from the verify env -- stricter
        # than the agent-subprocess case. An operator who legitimately
        # wants to drive the agent from a verify command sets the key
        # inline (`ANTHROPIC_API_KEY=... pytest ...`).
        self.assertIn("ANTH_PRESENT=NO", r.output)
        self.assertIn("OPENAI_PRESENT=NO", r.output)
        # The stripped secret values must not appear verbatim anywhere
        # in the captured output (env strip, not redaction-only).
        self.assertNotIn("sk_live_VERY_SECRET_SHOULD_NOT_LEAK", r.output)
        self.assertNotIn("hunter2_should_not_leak", r.output)
        self.assertNotIn("deploytok_should_not_leak", r.output)
        self.assertNotIn("sk-ant-SHOULD_NOT_LEAK_TO_VERIFY", r.output)
        self.assertNotIn("sk-oai-SHOULD_NOT_LEAK_TO_VERIFY", r.output)

    def test_command_that_commits_is_caught_as_head_changed(self) -> None:
        # Regression: a verify command that runs `git commit` leaves
        # `git status --porcelain` clean and exits 0, so the previous
        # dirty+exit-code-only gate accepted it as "ok". The squash-on-
        # approval + force-push that followed would then publish the
        # unreviewed verify-created commit to the PR branch. Snapshotting
        # HEAD before the loop and refusing any command that moves it
        # closes that hole.
        head_before = subprocess.run(
            ["git", "-C", str(self.tmp), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Stage and commit a new file inside the verify command itself --
        # exactly the dangerous shape (a verify rule that auto-fixes and
        # commits its own fix).
        cmd = (
            "sh -c 'echo VERIFY_AUTO_FIXED > autofix.txt && "
            "git add autofix.txt && "
            "git commit -q -m \"chore: verify-time auto-fix\"'"
        )
        r = workflow._run_verify_commands(self.tmp, (cmd,), 60)
        self.assertEqual(r.status, "head_changed")
        self.assertEqual(r.command, cmd)
        self.assertEqual(r.head_before, head_before)
        self.assertNotEqual(r.head_after, head_before)
        # And the worktree was clean on detection (not the dirty branch).
        self.assertEqual(r.dirty_files, ())

    def test_dirty_attribution_names_responsible_command_and_keeps_output(self) -> None:
        # Regression: previously the dirty check ran once at the end of
        # the loop, so a dirty failure always blamed `commands[-1]` and
        # discarded every command's captured output. The fix checks
        # dirtiness AFTER EACH command so the actual command that left
        # the worktree dirty is named, with its own stdout/stderr
        # preserved for the park comment.
        cmds = (
            "true",                                              # clean, exit 0
            "sh -c 'echo BUILD_LOG_LINE; touch leftover.txt'",   # leaves untracked file
            "true",                                              # should never run
        )
        r = workflow._run_verify_commands(self.tmp, cmds, 60)
        self.assertEqual(r.status, "dirty")
        # Named command is the SECOND command (the one that left the
        # tree dirty), NOT `commands[-1]`.
        self.assertEqual(r.command, cmds[1])
        self.assertEqual(r.exit_code, 0)
        # The dirty file lands in `dirty_files`.
        self.assertIn("leftover.txt", r.dirty_files)
        # The command's stdout is preserved for the park comment so the
        # operator can triage what the command actually did.
        self.assertIn("BUILD_LOG_LINE", r.output)

    def test_running_command_registered_for_shutdown_sweep(self) -> None:
        # The shutdown sweep (`agents.terminate_all_running`) only reaches
        # process groups registered in `agents._running_procs`. A verify
        # command must be registered for the lifetime of its run -- otherwise
        # the watchdog's `os._exit` leaves a slow command running and
        # mutating the worktree after the orchestrator has stopped -- and
        # cleared in the `finally` afterward so a finished command does not
        # leak into the registry. Popen is faked so the registry can be
        # inspected mid-run deterministically.
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = 0
        seen: dict[str, bool] = {}

        def check_registered(*_a, **_k):
            with agents._running_procs_lock:
                seen["during"] = proc in agents._running_procs
            return ("", "")

        proc.communicate.side_effect = check_registered
        with patch.object(verify.subprocess, "Popen", return_value=proc), \
             patch.object(verify, "_worktree_dirty_files", return_value=[]), \
             patch.object(verify, "_head_sha", return_value="sha"):
            r = verify._run_verify_commands(self.tmp, ("true",), 60)

        self.assertEqual(r.status, "ok")
        self.assertTrue(
            seen.get("during"), "verify child not registered during the run",
        )
        with agents._running_procs_lock:
            self.assertNotIn(proc, agents._running_procs)
