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

from orchestrator import agents, config, verify, workflow
from orchestrator.config import _parse_verify_commands as parse_verify_commands
from orchestrator.worktrees import VerifyResult

from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    LABEL_DOCUMENTING,
    LABEL_IN_REVIEW,
    LABEL_VALIDATING,
    REVIEW_APPROVED_MESSAGE,
    REVIEW_CHANGES_REQUESTED_MESSAGE,
    TEST_BASE_BRANCH,
    _PatchedWorkflowMixin,
    _agent,
    _issue_branch,
)


ISSUE = 7
PR_NUMBER = 21
DEV_SESSION = "dev-sess"
REVIEW_SHA = "rev-sha"
VERIFY_PYTEST = "pytest -q"
VERIFY_SLOW = "pytest --slow"
VERIFY_FAILED = "failed"
VERIFY_TIMEOUT = "timeout"
VERIFY_HEAD_CHANGED = "head_changed"
VERIFY_DIRTY = "dirty"
VERIFY_OK = "ok"
PARK_VERIFY_FAILED = "verify_failed"
PARK_VERIFY_TIMEOUT = "verify_timeout"
PARK_VERIFY_HEAD_CHANGED = "verify_head_changed"
PARK_VERIFY_DIRTY = "verify_dirty"
VERIFY_TIMEOUT_SECONDS = 123
OUTPUT_PAYLOAD_SIZE = 10000
OUTPUT_BUDGET = 4096
SECRET_PREFIX_SIZE = 90
REDACTION_PAYLOAD_SIZE = 4200
EXECUTABLE_MODE = 0o755
RUN_VERIFY_COMMANDS = "_run_verify_commands"
AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
VERIFY_COMMANDS_SETTING = "VERIFY_COMMANDS"
GIT_COMMAND = "git"
QUIET_FLAG = "-q"
WORKTREE_FLAG = "-C"
GIT_CONFIG = "config"
SEED_FILE = "seed"
PASSING_COMMAND = "true"
LEFTOVER_FILE = "leftover.txt"


class _RegisteredCommunicate:
    def __init__(self, process, seen):
        self.process = process
        self.seen = seen

    def __call__(self, *_args, **_kwargs):
        with agents._running_procs_lock:
            self.seen["during"] = self.process in agents._running_procs
        return "", ""


def shutil_quote(shell_text: str) -> str:
    """Local shell-quote helper for the truncate test -- avoids importing
    `shlex` at module scope when it is only used by one test."""
    import shlex

    return shlex.quote(shell_text)


class _VerifyGateFixtureMixin(_PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        defaults = dict(
            pr_number=PR_NUMBER,
            branch=_issue_branch(ISSUE),
            codex_session_id=DEV_SESSION,
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(ISSUE, **defaults)
        return gh, issue


class HandleValidatingVerifyGateTest(
    unittest.TestCase,
    _VerifyGateFixtureMixin,
):
    """Run verification only after an approved review verdict."""

    def test_empty_default_is_noop_on_approval(self) -> None:
        # With no `VERIFY_COMMANDS` configured, the gate short-circuits
        # to ok inside the runner; the helper is still called once (so a
        # future config flip toggles the gate without code changes), but
        # the approval / squash / in_review handoff path is unchanged.
        gh, issue = self._seeded()
        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=(REVIEW_SHA,),
        )

        self.assertEqual(mocks[RUN_VERIFY_COMMANDS].call_count, 1)
        # The configured commands tuple was forwarded verbatim --
        # default-empty means the runner sees ().
        call = mocks[RUN_VERIFY_COMMANDS].call_args
        self.assertEqual(call.args[1], config.VERIFY_COMMANDS)
        self.assertEqual(config.VERIFY_COMMANDS, ())
        # Handoff completed normally through the final-docs hop.
        self.assertIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        state = gh.pinned_data(ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))

    def test_config_parses_two_command_separators(self) -> None:
        # `_parse_verify_commands` accepts both `;` and `\n` separators so
        # the value fits on one line in a `.env` file. Blank lines and
        # `#`-commented lines are skipped.

        self.assertEqual(parse_verify_commands(""), ())
        self.assertEqual(
            parse_verify_commands(f"{VERIFY_PYTEST};ruff check ."),
            (VERIFY_PYTEST, "ruff check ."),
        )
        self.assertEqual(
            parse_verify_commands(f"{VERIFY_PYTEST}\nruff check .\n"),
            (VERIFY_PYTEST, "ruff check ."),
        )
        self.assertEqual(
            parse_verify_commands(f"\n#comment\n{VERIFY_PYTEST}\n\n"),
            (VERIFY_PYTEST,),
        )

    def test_verify_success_keeps_approval_flow(self) -> None:
        gh, issue = self._seeded()
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=VerifyResult(status=VERIFY_OK),
            )

        mocks[RUN_VERIFY_COMMANDS].assert_called_once()
        # Approval comment posted; label flipped to `documenting` (the
        # final-docs hop).
        self.assertTrue(
            any(
                ":white_check_mark:" in body
                for _, body in gh.posted_pr_comments
            )
        )
        self.assertIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        state = gh.pinned_data(ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))

    def test_verify_failed_parks(self) -> None:
        gh, issue = self._seeded()
        run = VerifyResult(
            status=VERIFY_FAILED,
            command=VERIFY_PYTEST,
            exit_code=2,
            output="E   AssertionError: bad\nTAIL_MARKER",
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=run,
            )

        state = gh.pinned_data(ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_VERIFY_FAILED)
        # No in_review or documenting handoff -- the verify gate fires
        # BEFORE the approval / squash / final-docs route is reached.
        self.assertNotIn((ISSUE, LABEL_IN_REVIEW), gh.label_history)
        self.assertNotIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        # No approval comment (gate fires BEFORE the approval post).
        self.assertFalse(
            any(
                ":white_check_mark:" in body
                for _, body in gh.posted_pr_comments
            )
        )
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("local verification failed", last_comment)
        self.assertIn(VERIFY_PYTEST, last_comment)
        self.assertIn("exited with code 2", last_comment)
        self.assertIn("TAIL_MARKER", last_comment)

    def test_verify_timeout_parks(self) -> None:
        gh, issue = self._seeded()
        run = VerifyResult(
            status=VERIFY_TIMEOUT,
            command=VERIFY_SLOW,
            exit_code=None,
            output="hanging...",
        )
        with (
            patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_SLOW,)),
            patch.object(config, "VERIFY_TIMEOUT", VERIFY_TIMEOUT_SECONDS),
        ):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=run,
            )

        state = gh.pinned_data(ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_VERIFY_TIMEOUT)
        self.assertNotIn((ISSUE, LABEL_IN_REVIEW), gh.label_history)
        self.assertNotIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(VERIFY_SLOW, last_comment)
        self.assertIn("timed out after 123s", last_comment)


class HandleValidatingVerifyRefusalTest(
    unittest.TestCase,
    _VerifyGateFixtureMixin,
):
    """Park when verification mutates HEAD, dirties the tree, or is premature."""

    def test_verify_head_changed_parks(self) -> None:
        # End-to-end: a verify command that moved HEAD must NOT flow
        # through to `in_review` -- otherwise squash-on-approval would
        # push the unreviewed commit. The handler parks the issue with a
        # distinct `verify_head_changed` reason so the operator can
        # adjudicate whether the auto-commit belongs in the PR.
        gh, issue = self._seeded()
        run = VerifyResult(
            status=VERIFY_HEAD_CHANGED,
            command="sh -c 'git commit -am autofix'",
            exit_code=0,
            output="",
            head_before="aaaa1111",
            head_after="bbbb2222",
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, ("sh -c 'git commit -am autofix'",)):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=run,
            )

        state = gh.pinned_data(ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_VERIFY_HEAD_CHANGED)
        # No in_review / documenting handoff and no approval / squash
        # side effects.
        self.assertNotIn((ISSUE, LABEL_IN_REVIEW), gh.label_history)
        self.assertNotIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        self.assertFalse(
            any(
                ":white_check_mark:" in body
                for _, body in gh.posted_pr_comments
            )
        )
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("moved HEAD", last_comment)
        # Short SHAs are surfaced so the operator can identify the commit.
        self.assertIn("aaaa1111", last_comment)
        self.assertIn("bbbb2222", last_comment)

    def test_verify_dirty_worktree_parks(self) -> None:
        gh, issue = self._seeded()
        run = VerifyResult(
            status=VERIFY_DIRTY,
            command=VERIFY_PYTEST,
            exit_code=0,
            dirty_files=("build/artifact.bin", "tests/cache"),
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=run,
            )

        state = gh.pinned_data(ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_VERIFY_DIRTY)
        self.assertNotIn((ISSUE, LABEL_IN_REVIEW), gh.label_history)
        self.assertNotIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("build/artifact.bin", last_comment)

    def test_changes_requested_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        review = _agent(
            session_id="rev-sess",
            last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
        )
        dev_fix = _agent(session_id=DEV_SESSION, last_message="fixed")
        # The verify mock should not be called -- assert by setting a
        # failing result that would otherwise park the issue.
        verify_fail = VerifyResult(
            status=VERIFY_FAILED,
            command=VERIFY_PYTEST,
            exit_code=1,
            output="bad",
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                verify_result=verify_fail,
            )

        mocks[RUN_VERIFY_COMMANDS].assert_not_called()
        # Standard CHANGES_REQUESTED handling: PR review comment + dev resume.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(gh.pinned_data(ISSUE).get("review_round"), 1)
        state = gh.pinned_data(ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))

    def test_unknown_verdict_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        verify_fail = VerifyResult(
            status=VERIFY_FAILED,
            command=VERIFY_PYTEST,
            exit_code=1,
            output="bad",
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(
                    last_message="I'm not sure what to think.",
                ),
                verify_result=verify_fail,
            )

        mocks[RUN_VERIFY_COMMANDS].assert_not_called()
        state = gh.pinned_data(ISSUE)
        # Park comes from the unknown-verdict path, NOT the verify gate;
        # confirm by checking the comment text (the unknown-verdict park
        # does not persist `park_reason` to pinned state for the
        # non-silent-crash case).
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertNotIn(
            state.get(PARK_REASON),
            (PARK_VERIFY_FAILED, PARK_VERIFY_TIMEOUT, PARK_VERIFY_DIRTY),
        )
        self.assertIn("did not emit a VERDICT line", gh.posted_comments[-1][1])


class _VerifyCommandsFixtureMixin:
    def setUp(self) -> None:
        self.worktree = Path(tempfile.mkdtemp())
        # Initialize a git repo so the dirty-detection branch works.
        subprocess.run(
            [GIT_COMMAND, "init", QUIET_FLAG, "-b", TEST_BASE_BRANCH, str(self.worktree)],
            check=True,
        )
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, str(self.worktree), GIT_CONFIG, "user.email", "t@t"],
            check=True,
        )
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, str(self.worktree), GIT_CONFIG, "user.name", "t"],
            check=True,
        )
        (self.worktree / SEED_FILE).write_text("x")
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, str(self.worktree), "add", "."],
            check=True,
        )
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, str(self.worktree), "commit", QUIET_FLAG, "-m", SEED_FILE],
            check=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.worktree, ignore_errors=True)


class RunVerifyCommandsTest(
    _VerifyCommandsFixtureMixin,
    unittest.TestCase,
):
    """Run commands, enforce timeouts, and report dirty worktrees."""

    def test_empty_commands_short_circuits_to_ok(self) -> None:
        run = workflow._run_verify_commands(self.worktree, (), 60)
        self.assertEqual(run.status, VERIFY_OK)
        self.assertIsNone(run.command)

    def test_all_commands_pass_returns_ok(self) -> None:
        run = workflow._run_verify_commands(
            self.worktree,
            (PASSING_COMMAND, "echo hello"),
            60,
        )
        self.assertEqual(run.status, VERIFY_OK)

    def test_nonzero_names_first_failed_command(self) -> None:
        run = workflow._run_verify_commands(
            self.worktree,
            (PASSING_COMMAND, "sh -c 'echo boom 1>&2; exit 3'", PASSING_COMMAND),
            60,
        )
        self.assertEqual(run.status, VERIFY_FAILED)
        self.assertEqual(run.command, "sh -c 'echo boom 1>&2; exit 3'")
        self.assertEqual(run.exit_code, 3)
        self.assertIn("boom", run.output)

    def test_timeout_keeps_partial_output(self) -> None:
        # `sleep 5` against a 1s timeout fires `TimeoutExpired`.
        run = workflow._run_verify_commands(
            self.worktree,
            ("sleep 5",),
            timeout=1,
        )
        self.assertEqual(run.status, VERIFY_TIMEOUT)
        self.assertEqual(run.command, "sleep 5")
        self.assertIsNone(run.exit_code)

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
        marker = self.worktree / "post_timeout_marker.txt"
        # Background subshell sleeps 2s then touches the marker. Parent
        # shell sleeps 10s so the 1s timeout definitely fires. If the
        # group-kill works, the background subshell dies before its
        # sleep finishes and the marker is never created.
        cmd = f"(sleep 2 && touch {marker}) & sleep 10"
        run = workflow._run_verify_commands(self.worktree, (cmd,), timeout=1)
        self.assertEqual(run.status, VERIFY_TIMEOUT)
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
        run = workflow._run_verify_commands(
            self.worktree,
            ("sh -c 'echo leak > leftover.txt'",),
            60,
        )
        self.assertEqual(run.status, VERIFY_DIRTY)
        self.assertIn(LEFTOVER_FILE, run.dirty_files)

    def test_output_truncated_to_budget(self) -> None:
        padding = "X" * OUTPUT_PAYLOAD_SIZE
        big = f"{padding}TAIL"
        run = workflow._run_verify_commands(
            self.worktree,
            (f"sh -c 'printf %s {shutil_quote(big)}; exit 1'",),
            60,
        )
        self.assertEqual(run.status, VERIFY_FAILED)
        # Tail preserved, leading bulk trimmed.
        self.assertIn("TAIL", run.output)
        self.assertLessEqual(len(run.output), OUTPUT_BUDGET)


class VerifyCommandEnvironmentTest(
    _VerifyCommandsFixtureMixin,
    unittest.TestCase,
):
    """Redact output and strip secrets or credential locators from env."""

    def test_boundary_secret_fully_redacted(self) -> None:
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
        prefix = "P" * SECRET_PREFIX_SIZE
        # total = 4200 → cut at byte 104; secret occupies 90..129, so
        # bytes 14..39 of the secret (`E-0123456789ABCDEF`) would survive
        # a naive truncation.
        suffix_len = REDACTION_PAYLOAD_SIZE - len(prefix) - len(secret)
        suffix = "S" * suffix_len
        payload = prefix + secret + suffix
        self.assertEqual(len(payload), REDACTION_PAYLOAD_SIZE)
        cut = len(payload) - OUTPUT_BUDGET
        self.assertLess(payload.index(secret), cut)
        self.assertGreater(payload.index(secret) + len(secret), cut)

        import os as _os
        import shlex

        cmd = f"sh -c 'printf %s {shlex.quote(payload)}; exit 1'"
        with patch.dict(_os.environ, {"VERIFY_TEST_API_KEY": secret}):
            run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_FAILED)
        # The full secret must be gone -- baseline check.
        self.assertNotIn(secret, run.output)
        # And no 8+ char substring of the secret survives either.
        # Length 8 matches `_REDACT_MIN_VALUE_LEN`: shorter accidental
        # collisions are below the redaction threshold and tolerable.
        for start in range(len(secret) - 7):
            secret_fragment = secret[start:start + 8]
            self.assertNotIn(
                secret_fragment,
                run.output,
                f"partial secret substring leaked: {secret_fragment!r}",
            )
        # And the redaction marker is present (proves the runner
        # actually saw and replaced the secret).
        self.assertIn("***", run.output)

    def test_github_token_stripped_from_env(self) -> None:
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
            # `run.output` either way.
            "sh -c 'echo TOKEN_PRESENT=$([ -n \"$GITHUB_TOKEN\" ] && echo YES || echo NO); exit 1'"
        )
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ghp_ORCHESTRATOR_PAT_SHOULD_NOT_LEAK"},
        ):
            run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_FAILED)
        # The verify environment must NOT carry GITHUB_TOKEN through.
        self.assertIn("TOKEN_PRESENT=NO", run.output)
        # And the original token value must not appear verbatim. (This
        # also catches a regression where redaction were doing the heavy
        # lifting instead of env stripping -- redaction would mask the
        # value with `***`, but the variable would still have been
        # exposed to the verify command.)
        self.assertNotIn("ghp_ORCHESTRATOR_PAT_SHOULD_NOT_LEAK", run.output)

    def test_strips_write_locators_from_env(self) -> None:
        # Issue #213 review: SSH-agent socket, askpass binaries, and
        # `GIT_SSH_COMMAND` are write-credential pointers, not secret-
        # shaped values. Leaving them in the verify shell lets a
        # hostile dependency forward through the operator's loaded
        # ssh-agent (and push to any host whose key is loaded) or
        # invoke the operator's askpass binary in their session.
        cmd = (
            "sh -c '"
            'echo SSH_AUTH=$([ -n "$SSH_AUTH_SOCK" ] && echo YES || echo NO); '
            'echo SSH_ASK=$([ -n "$SSH_ASKPASS" ] && echo YES || echo NO); '
            'echo GIT_ASK=$([ -n "$GIT_ASKPASS" ] && echo YES || echo NO); '
            'echo GIT_SSH=$([ -n "$GIT_SSH_COMMAND" ] && echo YES || echo NO); '
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
            run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_FAILED)
        self.assertIn("SSH_AUTH=NO", run.output)
        self.assertIn("SSH_ASK=NO", run.output)
        self.assertIn("GIT_ASK=NO", run.output)
        self.assertIn("GIT_SSH=NO", run.output)
        # The locator values must not survive verbatim anywhere.
        self.assertNotIn("/tmp/ssh-test/agent.42", run.output)
        self.assertNotIn("/home/op/.ssh/deploy-key", run.output)

    def test_strips_credential_file_locators_from_env(self) -> None:
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
            'echo ORCH_TF=$([ -n "$ORCHESTRATOR_TOKEN_FILE" ] && '
            "echo YES || echo NO); "
            'echo GAC=$([ -n "$GOOGLE_APPLICATION_CREDENTIALS" ] && '
            "echo YES || echo NO); "
            'echo AWS_SCF=$([ -n "$AWS_SHARED_CREDENTIALS_FILE" ] && '
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
            run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_FAILED)
        self.assertIn("ORCH_TF=NO", run.output)
        self.assertIn("GAC=NO", run.output)
        self.assertIn("AWS_SCF=NO", run.output)
        # And the locator path itself must not survive verbatim either
        # (env strip, not redaction-only).
        self.assertNotIn("/etc/secrets/orch-token-path", run.output)
        self.assertNotIn("/etc/secrets/gcp.json", run.output)
        self.assertNotIn("/etc/secrets/aws-creds", run.output)

    def test_strips_production_secret_shapes_from_env(self) -> None:
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
            'sh -c \'echo STRIPE_PRESENT=$([ -n "$STRIPE_API_KEY" ] && '
            "echo YES || echo NO); "
            'echo DBPW_PRESENT=$([ -n "$DATABASE_PASSWORD" ] && '
            "echo YES || echo NO); "
            'echo DEPLOY_PRESENT=$([ -n "$DEPLOY_TOKEN" ] && '
            "echo YES || echo NO); "
            'echo ANTH_PRESENT=$([ -n "$ANTHROPIC_API_KEY" ] && '
            "echo YES || echo NO); "
            'echo OPENAI_PRESENT=$([ -n "$OPENAI_API_KEY" ] && '
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
            run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_FAILED)
        self.assertIn("STRIPE_PRESENT=NO", run.output)
        self.assertIn("DBPW_PRESENT=NO", run.output)
        self.assertIn("DEPLOY_PRESENT=NO", run.output)
        # Provider auth is stripped from the verify env -- stricter
        # than the agent-subprocess case. An operator who legitimately
        # wants to drive the agent from a verify command sets the key
        # inline (`ANTHROPIC_API_KEY=... pytest ...`).
        self.assertIn("ANTH_PRESENT=NO", run.output)
        self.assertIn("OPENAI_PRESENT=NO", run.output)
        # The stripped secret values must not appear verbatim anywhere
        # in the captured output (env strip, not redaction-only).
        self.assertNotIn("sk_live_VERY_SECRET_SHOULD_NOT_LEAK", run.output)
        self.assertNotIn("hunter2_should_not_leak", run.output)
        self.assertNotIn("deploytok_should_not_leak", run.output)
        self.assertNotIn("sk-ant-SHOULD_NOT_LEAK_TO_VERIFY", run.output)
        self.assertNotIn("sk-oai-SHOULD_NOT_LEAK_TO_VERIFY", run.output)


class VerifyCommandMutationTest(
    _VerifyCommandsFixtureMixin,
    unittest.TestCase,
):
    """Report verify-time commits, dirty output, and process registration."""

    def test_commit_command_reports_head_change(self) -> None:
        # Regression: a verify command that runs `git commit` leaves
        # `git status --porcelain` clean and exits 0, so the previous
        # dirty+exit-code-only gate accepted it as "ok". The squash-on-
        # approval + force-push that followed would then publish the
        # unreviewed verify-created commit to the PR branch. Snapshotting
        # HEAD before the loop and refusing any command that moves it
        # closes that hole.
        head_before = subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, str(self.worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Stage and commit a new file inside the verify command itself --
        # exactly the dangerous shape (a verify rule that auto-fixes and
        # commits its own fix).
        cmd = (
            "sh -c 'echo VERIFY_AUTO_FIXED > autofix.txt && "
            "git add autofix.txt && "
            'git commit -q -m "chore: verify-time auto-fix"\''
        )
        run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_HEAD_CHANGED)
        self.assertEqual(run.command, cmd)
        self.assertEqual(run.head_before, head_before)
        self.assertNotEqual(run.head_after, head_before)
        # And the worktree was clean on detection (not the dirty branch).
        self.assertEqual(run.dirty_files, ())

    def test_dirty_result_keeps_command_output(self) -> None:
        # Regression: previously the dirty check ran once at the end of
        # the loop, so a dirty failure always blamed `commands[-1]` and
        # discarded every command's captured output. The fix checks
        # dirtiness AFTER EACH command so the actual command that left
        # the worktree dirty is named, with its own stdout/stderr
        # preserved for the park comment.
        cmds = (
            PASSING_COMMAND,  # clean, exit 0
            "sh -c 'echo BUILD_LOG_LINE; touch leftover.txt'",  # leaves untracked file
            PASSING_COMMAND,  # should never run
        )
        run = workflow._run_verify_commands(self.worktree, cmds, 60)
        self.assertEqual(run.status, VERIFY_DIRTY)
        # Named command is the SECOND command (the one that left the
        # tree dirty), NOT `commands[-1]`.
        self.assertEqual(run.command, cmds[1])
        self.assertEqual(run.exit_code, 0)
        # The dirty file lands in `dirty_files`.
        self.assertIn(LEFTOVER_FILE, run.dirty_files)
        # The command's stdout is preserved for the park comment so the
        # operator can triage what the command actually did.
        self.assertIn("BUILD_LOG_LINE", run.output)

    def test_running_command_registered_for_shutdown(self) -> None:
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

        proc.communicate.side_effect = _RegisteredCommunicate(proc, seen)
        with (
            patch.object(verify.subprocess, "Popen", return_value=proc),
            patch.object(verify, "_worktree_dirty_files", return_value=[]),
            patch.object(verify, "_head_sha", return_value="sha"),
        ):
            run = verify._run_verify_commands(self.worktree, (PASSING_COMMAND,), 60)

        self.assertEqual(run.status, VERIFY_OK)
        self.assertTrue(
            seen.get("during"),
            "verify child not registered during the run",
        )
        with agents._running_procs_lock:
            self.assertNotIn(proc, agents._running_procs)


class DrainVerifyOutputTest(unittest.TestCase):
    """`_drain_verify_output` reads a killed verify shell's buffered output.
    The first bounded drain covers the normal case; if it wedges -- a
    descendant that escaped the group is still holding the pipe fd open -- it
    escalates to `proc.kill()` and one more bounded drain, then gives up with
    empty output. Popen is faked so the wedged path is deterministic.
    """

    def test_first_drain_returns_without_extra_kill(self) -> None:
        proc = MagicMock()
        proc.communicate.return_value = ("out", "err")
        self.assertEqual(verify._drain_verify_output(proc), ("out", "err"))
        proc.kill.assert_not_called()

    def test_wedged_drain_kills_then_returns_output(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="verify", timeout=5),
            ("late-out", "late-err"),
        ]
        self.assertEqual(
            verify._drain_verify_output(proc),
            ("late-out", "late-err"),
        )
        proc.kill.assert_called_once()

    def test_both_drains_time_out_returns_empty(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd="verify",
            timeout=5,
        )
        self.assertEqual(verify._drain_verify_output(proc), ("", ""))
        proc.kill.assert_called_once()


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [GIT_COMMAND, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


class WorktreeDirtyFilesHardeningTest(unittest.TestCase):
    """`_worktree_dirty_files` runs its `git status` probe through the
    hardened git path, so an agent-planted `core.fsmonitor` in the worktree
    config cannot execute with the orchestrator's process environment. Every
    caller passes an agent-writable worktree, so the probe is hardened
    unconditionally. Real modifications are still reported; only fsmonitor
    execution and the global-config trust boundary are dropped.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-dirty-hardening-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self.work = self.tmpdir / "work"
        self.work.mkdir()
        _run_git("init", QUIET_FLAG, "-b", TEST_BASE_BRANCH, cwd=self.work)
        _run_git(GIT_CONFIG, "user.email", "t@t", cwd=self.work)
        _run_git(GIT_CONFIG, "user.name", "t", cwd=self.work)
        (self.work / SEED_FILE).write_text("x\n")
        _run_git("add", ".", cwd=self.work)
        _run_git("commit", QUIET_FLAG, "-m", SEED_FILE, cwd=self.work)

    def test_blocks_planted_fsmonitor_reports_dirty(self) -> None:
        # Hook + marker live outside the worktree so they are not themselves
        # untracked files. The `/`+NUL response is fsmonitor v1 for "assume
        # everything changed" -- a scan hint only, so a clean tree reads clean.
        marker = self.tmpdir / "fsmonitor_ran.txt"
        hook = self.tmpdir / "fsmonitor_hook.sh"
        hook.write_text(
            f"#!/bin/sh\nprintf ran >> '{marker}'\nprintf '/\\000'\n"
        )
        hook.chmod(EXECUTABLE_MODE)
        _run_git(GIT_CONFIG, "core.fsmonitor", str(hook), cwd=self.work)

        (self.work / LEFTOVER_FILE).write_text("leak\n")
        # Prove the planted hook is genuinely honored: a plain, unhardened
        # index refresh fires it. Without this the empty-marker assertion
        # below could pass simply because the hook was never wired.
        _run_git("status", "--porcelain", cwd=self.work)
        self.assertTrue(
            marker.exists() and marker.read_text(),
            "planted fsmonitor never fired for a plain git status; the test cannot detect a regression",
        )
        marker.unlink()

        dirty = verify._worktree_dirty_files(self.work)

        # The real modification is still reported...
        self.assertIn(LEFTOVER_FILE, dirty)
        # ...but the hardened probe never executed the planted helper with
        # our process environment attached.
        self.assertFalse(
            marker.exists() and marker.read_text(),
            "hardened dirty probe executed the planted core.fsmonitor",
        )
