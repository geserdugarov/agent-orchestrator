# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import (
    analytics,
    base_sync,
    config,
    workflow,
    worktree_lifecycle,
    worktrees,
)
from orchestrator.agents import AgentResult
from orchestrator.github import BACKLOG_LABEL, BASE_SYNC_HOLD_LABEL
from orchestrator.workflow import _parse_documentation_verdict, _parse_review_verdict

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakePRReview,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _as_mock,
)




class ParseReviewVerdictTest(unittest.TestCase):
    def test_approved_alone_on_line(self) -> None:
        self.assertEqual(
            _parse_review_verdict("Looks good.\n\nVERDICT: APPROVED"),
            ("approved", "Looks good."),
        )

    def test_changes_requested_with_numbered_list(self) -> None:
        msg = "1. Fix typo in README\n2. Add a test for the empty case\n\nVERDICT: CHANGES_REQUESTED"
        verdict, body = _parse_review_verdict(msg)
        self.assertEqual(verdict, "changes_requested")
        self.assertIn("1. Fix typo in README", body)
        self.assertNotIn("VERDICT", body)

    def test_inline_marker_is_accepted(self) -> None:
        self.assertEqual(
            _parse_review_verdict("All good. VERDICT: APPROVED"),
            ("approved", "All good."),
        )

    def test_case_insensitive(self) -> None:
        verdict, _ = _parse_review_verdict("verdict: approved")
        self.assertEqual(verdict, "approved")

    def test_last_marker_wins(self) -> None:
        msg = "I considered VERDICT: APPROVED but a test fails.\nVERDICT: CHANGES_REQUESTED"
        verdict, _ = _parse_review_verdict(msg)
        self.assertEqual(verdict, "changes_requested")

    def test_no_marker_returns_unknown(self) -> None:
        self.assertEqual(
            _parse_review_verdict("looks fine to me"),
            ("unknown", "looks fine to me"),
        )

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_review_verdict(""), ("unknown", ""))


class ParseDocumentationVerdictTest(unittest.TestCase):
    """Documentation stage outputs one of three observable outcomes:

      * Valid 'updated' -- the agent committed a `docs:` change. The
        parser does NOT see this; the stage handler detects it from the
        new commit. The case here is that a message describing the
        update but lacking the no-change marker must still return
        'unknown' so a forgotten commit can't be misread as no-change.
      * Valid 'no_change' -- the explicit `DOCS: NO_CHANGE` marker.
      * Invalid -- ambiguous text without the marker, including
        plausible-but-unstructured 'no changes needed' phrasing that
        must NOT be accepted as success.
    """

    def test_no_change_marker_alone_on_line(self) -> None:
        self.assertEqual(
            _parse_documentation_verdict(
                "Diff is internal-only; nothing user-visible changed.\n\nDOCS: NO_CHANGE"
            ),
            ("no_change", "Diff is internal-only; nothing user-visible changed."),
        )

    def test_no_change_marker_case_insensitive(self) -> None:
        verdict, _ = _parse_documentation_verdict("docs: no_change")
        self.assertEqual(verdict, "no_change")

    def test_last_marker_wins(self) -> None:
        # Mirrors `_parse_review_verdict`'s "last marker wins" rule so a
        # template/sample reference earlier in the body loses to the
        # concluding line.
        msg = (
            "I almost wrote DOCS: NO_CHANGE but actually the README is "
            "stale, so I'll commit a fix.\n\nDOCS: NO_CHANGE"
        )
        verdict, _ = _parse_documentation_verdict(msg)
        self.assertEqual(verdict, "no_change")

    def test_ambiguous_no_change_text_is_not_accepted(self) -> None:
        # Plain prose that sounds like a no-change result must NOT pass
        # without the explicit marker -- otherwise an agent that forgot
        # to commit a real docs update would silently close the stage.
        verdict, body = _parse_documentation_verdict(
            "Looks like no docs changes needed."
        )
        self.assertEqual(verdict, "unknown")
        self.assertIn("no docs changes needed", body)

    def test_update_description_without_marker_is_unknown(self) -> None:
        # The 'updated' outcome is signalled by the new commit on the
        # branch, not by the parser. A message describing an update but
        # lacking the no-change marker must therefore stay 'unknown' so
        # the no-commit branch (parser-only) cannot silently accept it.
        verdict, _ = _parse_documentation_verdict(
            "Updated README.md with the new flag."
        )
        self.assertEqual(verdict, "unknown")

    def test_inline_marker_in_prose_is_unknown(self) -> None:
        # The marker must start its own line. An inline reference
        # embedded in a sentence -- e.g. "I cannot conclude DOCS:
        # NO_CHANGE because the README is stale" -- is exactly the kind
        # of ambiguous no-commit text the issue forbids accepting.
        verdict, _ = _parse_documentation_verdict(
            "I cannot conclude DOCS: NO_CHANGE because README is stale."
        )
        self.assertEqual(verdict, "unknown")

    def test_non_final_marker_followed_by_text_is_unknown(self) -> None:
        # The marker must be the FINAL non-whitespace content. A marker
        # line followed by an unresolved question must be rejected so an
        # agent's follow-up clarification can't silently close the stage.
        verdict, _ = _parse_documentation_verdict(
            "DOCS: NO_CHANGE\nBut I have a question about the API."
        )
        self.assertEqual(verdict, "unknown")

    def test_marker_with_trailing_punctuation_is_unknown(self) -> None:
        # `DOCS: NO_CHANGE.` (trailing punctuation) is rejected; the
        # contract is a machine-readable marker, not a sentence. Without
        # this, a markdown-trained agent's habit of ending sentences
        # with periods would silently mask the stricter rule.
        verdict, _ = _parse_documentation_verdict("All clear.\n\nDOCS: NO_CHANGE.")
        self.assertEqual(verdict, "unknown")

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_documentation_verdict(""), ("unknown", ""))


class BuildDocumentationPromptTest(unittest.TestCase):
    """The documentation prompt is what teaches the agent the contract
    the parser relies on. Verify the contract is actually communicated:
    diff vs README/docs/plans, `docs:` commit subject for the update
    branch, explicit `DOCS: NO_CHANGE` marker for the no-update branch,
    and a refusal to accept ambiguous phrasing.
    """

    def _build(self) -> str:
        return workflow._build_documentation_prompt(
            _TEST_SPEC,
            make_issue(67100, title="add foo flag", body="users want a foo flag"),
            comments_text="",
        )

    def test_instructs_diff_against_readme_docs_plans(self) -> None:
        prompt = self._build()
        self.assertIn("README.md", prompt)
        self.assertIn("docs/", prompt)
        self.assertIn("plans/", prompt)
        base_ref = f"{_TEST_SPEC.remote_name}/{_TEST_SPEC.base_branch}"
        self.assertIn(f"git diff {base_ref}...HEAD", prompt)

    def test_instructs_docs_commit_subject_for_updated_case(self) -> None:
        prompt = self._build()
        self.assertIn("docs:", prompt)
        self.assertIn('git commit -m "docs: <subject>"', prompt)

    def test_specifies_machine_readable_no_change_marker(self) -> None:
        prompt = self._build()
        self.assertIn("DOCS: NO_CHANGE", prompt)

    def test_warns_against_ambiguous_no_change_text(self) -> None:
        # The prompt itself must tell the agent that prose like
        # 'no changes needed' will be parked, mirroring the parser's
        # refusal to accept it.
        prompt = self._build()
        self.assertIn("'no changes needed'", prompt)

    def test_includes_issue_title_and_number(self) -> None:
        prompt = self._build()
        self.assertIn("#67100", prompt)
        self.assertIn("add foo flag", prompt)


class RedactSecretsTest(unittest.TestCase):
    """The agent retains its provider auth (ANTHROPIC_API_KEY etc.) so that
    its CLI can talk to the model. Anything we surface from its stderr to
    GitHub must scrub those values first; otherwise a prompt-injected agent
    that echoed its key onto stderr would leak it into a public issue.
    """

    def _patched_env(self, **values: str):
        return patch.dict(os.environ, values, clear=False)

    def test_redacts_provider_api_key(self) -> None:
        with self._patched_env(ANTHROPIC_API_KEY="sk-ant-supersecretvalue123"):
            out = workflow._redact_secrets(
                "Traceback ...\n  401 sk-ant-supersecretvalue123 invalid"
            )
        self.assertNotIn("sk-ant-supersecretvalue123", out)
        self.assertIn("***", out)

    def test_redacts_github_token_by_exact_name(self) -> None:
        # GITHUB_TOKEN itself doesn't end in any of the suffixes we strip,
        # but it's the orchestrator's own creds for git/gh subprocesses --
        # cover it explicitly via _SECRET_KEY_NAMES.
        with self._patched_env(GITHUB_TOKEN="ghp_thisisthetokenvalue"):
            out = workflow._redact_secrets("remote: bad credential ghp_thisisthetokenvalue")
        self.assertNotIn("ghp_thisisthetokenvalue", out)

    def test_redacts_github_token_loaded_from_file(self) -> None:
        # Token-file path (ORCHESTRATOR_TOKEN_FILE / default
        # ~/.config/<repo>/token) populates config.GITHUB_TOKEN without
        # touching os.environ. The env-loop alone would miss it, so we
        # also pass config.GITHUB_TOKEN explicitly. Regression: without
        # that pass, agent stderr that cat'd the token file would leak
        # the credential into the park comment.
        token = "ghp_filebackedtokenvalue9876"
        # Ensure the env path wouldn't catch it on its own.
        env_without_token = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        with patch.dict(os.environ, env_without_token, clear=True), \
                patch.object(config, "GITHUB_TOKEN", token):
            out = workflow._redact_secrets(f"cat ran: {token} got captured")
        self.assertNotIn(token, out)
        self.assertIn("***", out)

    def test_redacts_arbitrary_provider_via_suffix(self) -> None:
        # The suffix list is what catches the long tail (HF_TOKEN,
        # GEMINI_API_KEY, ...) without us enumerating every provider.
        with self._patched_env(GEMINI_API_KEY="ya29.deadbeefdeadbeef"):
            out = workflow._redact_secrets("got ya29.deadbeefdeadbeef back")
        self.assertNotIn("ya29.deadbeefdeadbeef", out)

    def test_redacts_bare_name_secret(self) -> None:
        # Bare names like `TOKEN` or `PASSWORD` don't end in `_TOKEN` etc.,
        # so the suffix predicate misses them. _agent_env only strips
        # GitHub-aliased tokens, so a bare $TOKEN passes through to the
        # agent and would leak unredacted if echoed to stderr.
        with self._patched_env(TOKEN="ghp_barenametokenvalue123"):
            out = workflow._redact_secrets("auth failed for ghp_barenametokenvalue123")
        self.assertNotIn("ghp_barenametokenvalue123", out)
        with self._patched_env(PASSWORD="hunter2isthepasswordvalue"):
            out = workflow._redact_secrets("login: hunter2isthepasswordvalue rejected")
        self.assertNotIn("hunter2isthepasswordvalue", out)

    def test_leaves_short_values_alone(self) -> None:
        # A 4-char throwaway value would mask incidental substrings. The
        # min-length floor protects regular english text in stderr.
        with self._patched_env(DEV_KEY="true"):
            out = workflow._redact_secrets("status was true and the build ran")
        self.assertEqual(out, "status was true and the build ran")

    def test_leaves_non_secret_keys_alone(self) -> None:
        with self._patched_env(BUILD_NUMBER="this-string-is-long-enough"):
            out = workflow._redact_secrets("BUILD this-string-is-long-enough done")
        self.assertIn("this-string-is-long-enough", out)

    def test_empty_input_passthrough(self) -> None:
        self.assertEqual(workflow._redact_secrets(""), "")

    def test_diagnostics_block_redacts_before_truncation(self) -> None:
        # Park comments cap the surfaced tail at 1KB. If we redacted after
        # slicing, a key that spans the cut would survive in the visible
        # tail. Pad noise so the secret would otherwise straddle the cap.
        secret = "sk-ant-spanningthecutboundary123"
        with self._patched_env(ANTHROPIC_API_KEY=secret):
            stderr = ("X" * (workflow._STDERR_TAIL_BUDGET - 8)) + secret + " trailing"
            block = workflow._format_stderr_diagnostics(
                AgentResult(
                    session_id="s", last_message="", exit_code=1,
                    timed_out=False, stdout="", stderr=stderr,
                ),
                "Agent",
            )
        self.assertNotIn(secret, block)
        self.assertIn("***", block)
        # The tail budget is still honored on the *redacted* string.
        self.assertIn("trailing", block)

    def test_log_tail_redacts(self) -> None:
        with self._patched_env(OPENAI_API_KEY="sk-proj-loglinevaluexyz"):
            tail = workflow._stderr_log_tail(
                AgentResult(
                    session_id="s", last_message="", exit_code=1,
                    timed_out=False, stdout="",
                    stderr="auth failed for sk-proj-loglinevaluexyz",
                ),
            )
        self.assertNotIn("sk-proj-loglinevaluexyz", tail)

    def test_diagnostics_redacts_multiline_secret_at_eof(self) -> None:
        # Regression for the rstrip-before-redact ordering bug: a
        # multi-line secret whose env value itself ends in `\n` (e.g. a
        # PEM/SSH key) echoed at the end of stderr. If rstrip ran first,
        # the trailing newline would be eaten and `str.replace(value,
        # "***")` would no longer match the env value verbatim, leaking
        # the secret into the park comment.
        secret = "-----BEGIN PRIVATE KEY-----\nAAAABBBBCCCCDDDD\n-----END PRIVATE KEY-----\n"
        with self._patched_env(SSH_PRIVATE_KEY=secret):
            block = workflow._format_stderr_diagnostics(
                AgentResult(
                    session_id="s", last_message="", exit_code=1,
                    timed_out=False, stdout="",
                    stderr="boom: " + secret,
                ),
                "Agent",
            )
        self.assertNotIn("AAAABBBBCCCCDDDD", block)
        self.assertIn("***", block)

    def test_log_tail_redacts_multiline_secret_at_eof(self) -> None:
        secret = "line1-of-secret-value\nline2-of-secret-value\n"
        with self._patched_env(API_TOKEN=secret):
            tail = workflow._stderr_log_tail(
                AgentResult(
                    session_id="s", last_message="", exit_code=1,
                    timed_out=False, stdout="",
                    stderr="leaked: " + secret,
                ),
            )
        self.assertNotIn("line2-of-secret-value", tail)


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
        from unittest.mock import MagicMock

        r = MagicMock()
        r.returncode = 0
        r.stdout = stdout
        r.stderr = stderr
        return r

    @staticmethod
    def _fail(stderr: str = "boom") -> "object":
        from unittest.mock import MagicMock

        r = MagicMock()
        r.returncode = 128
        r.stdout = ""
        r.stderr = stderr
        return r

    def _patch(self, run_results: list) -> "tuple":
        from unittest.mock import MagicMock

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

    def test_existing_remote_branch_force_with_lease_uses_observed_sha(
        self,
    ) -> None:
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
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

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


class HandlePickupTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_pickup_with_decompose_off_routes_straight_to_implementing(
        self,
    ) -> None:
        # Legacy path retained behind the DECOMPOSE kill switch: an
        # unlabeled issue still goes straight to implementing without a
        # decomposer round, so operators can disable decomposition without
        # redeploying old binaries.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertTrue(
            any(":robot: orchestrator picking this up" in body
                for _, body in gh.posted_comments)
        )
        # Pickup flips the label to implementing; downstream handler may park
        # on awaiting_human but does not re-label.
        self.assertEqual(gh.label_history[0], (1, "implementing"))
        self.assertIn("created_at", gh.pinned_data(1))
        # _handle_implementing was actually entered (codex spawned).
        mocks["run_agent"].assert_called_once()

    def test_pickup_skips_issue_from_non_allowed_author(self) -> None:
        # A populated ALLOWED_ISSUE_AUTHORS allowlist must drop unlabeled
        # issues from outside that list silently -- no comment, no label,
        # no pinned state. This is the abuse guard: a stranger filing
        # issues on a public repo cannot make the orchestrator spawn agents.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="stranger")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)):
            mocks = self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="should not run"),
                has_new_commits=False,
            )

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.pinned_data(1), {})
        mocks["run_agent"].assert_not_called()

    def test_pickup_proceeds_for_allowed_author(self) -> None:
        # Sanity: when the author IS in the list, pickup behaves exactly
        # like the unguarded path -- this guard is purely a triage filter.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="alice")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("alice", "bob")), \
             patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertIn((1, "implementing"), gh.label_history)
        self.assertIn("created_at", gh.pinned_data(1))

    def test_pickup_matches_author_case_insensitively(self) -> None:
        # GitHub logins are case-insensitive: "Alice" and "alice" resolve
        # to the same account. The allowlist must accept either casing on
        # both sides so a maintainer's mixed-case configuration doesn't
        # silently reject legitimate issues.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="Alice")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("alice",)), \
             patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertIn((1, "implementing"), gh.label_history)

    def test_empty_allowlist_lets_anyone_through(self) -> None:
        # Default config: empty tuple disables the filter so existing
        # single-user setups (and any deployment that hasn't opted in)
        # keep their current "anyone can trigger" behavior.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="random-user")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ()), \
             patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertIn((1, "implementing"), gh.label_history)


class StageEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`set_workflow_label` is the single chokepoint for stage transitions,
    so a hook there gives every workflow handler a `stage_enter` event for
    free. The fake mirrors the real client's `recorded_events` capture and
    JSONL sink so workflow tests can assert on either surface.
    """

    def test_set_workflow_label_records_stage_enter_event(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        gh.set_workflow_label(issue, "implementing")
        self.assertEqual(len(gh.recorded_events), 1)
        ev = gh.recorded_events[0]
        self.assertEqual(ev["event"], "stage_enter")
        self.assertEqual(ev["stage"], "implementing")
        self.assertEqual(ev["issue"], 1)
        self.assertEqual(ev["repo"], "geserdugarov/agent-orchestrator")
        self.assertIn("ts", ev)
        # UTC timestamp, ISO 8601 with offset.
        datetime.fromisoformat(ev["ts"])

    def test_none_label_does_not_emit(self) -> None:
        # Clearing the workflow label is not a stage; the helper must
        # short-circuit so downstream consumers don't see a phantom
        # `stage_enter` with stage=None.
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        gh.set_workflow_label(issue, None)
        self.assertEqual(gh.recorded_events, [])

    def test_pickup_emits_decomposing_stage_enter(self) -> None:
        # The hook is centralized: a real handler call (no manual label
        # flip in the test) still produces the event because
        # `_handle_pickup` routes through `gh.set_workflow_label`.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )
        stages = [e["stage"] for e in gh.recorded_events if e["event"] == "stage_enter"]
        self.assertIn("decomposing", stages)

    def test_event_log_path_writes_one_jsonl_object_per_line(self) -> None:
        # End-to-end: a configured EVENT_LOG_PATH receives one parseable
        # JSONL object per transition, with the documented schema.
        with tempfile.TemporaryDirectory(prefix="evlog-") as td:
            path = Path(td) / "events.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(7)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, "implementing")
                gh.set_workflow_label(issue, "validating")
                gh.set_workflow_label(issue, "in_review")
            # File closed on context exit -- read it back, parse line by line.
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                [r["stage"] for r in records],
                ["implementing", "validating", "in_review"],
            )
            for r in records:
                self.assertEqual(r["event"], "stage_enter")
                self.assertEqual(r["issue"], 7)
                self.assertEqual(r["repo"], "geserdugarov/agent-orchestrator")
                # ts must be a valid ISO-8601 UTC timestamp.
                ts = datetime.fromisoformat(r["ts"])
                self.assertEqual(ts.tzinfo, timezone.utc)
            # JSONL invariant: exactly one object per line, no blank lines.
            for line in lines:
                self.assertTrue(line.strip())
                self.assertFalse(line.startswith(" "))

    def test_event_log_path_unset_writes_no_file(self) -> None:
        # The legacy behavior is that no event file exists; flipping a
        # label must not create one when EVENT_LOG_PATH is unset.
        with tempfile.TemporaryDirectory(prefix="evlog-off-") as td:
            sentinel = Path(td) / "should-not-be-created.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", None):
                gh = FakeGitHubClient()
                issue = make_issue(1)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, "implementing")
            self.assertFalse(sentinel.exists())
            # In-memory capture still works even with the file sink disabled,
            # so tests don't need a temp file to inspect transitions.
            self.assertEqual(len(gh.recorded_events), 1)


class AgentLifecycleEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_run_agent_tracked` bookends every agent invocation with
    `agent_spawn` / `agent_exit` events carrying the role, stage, session
    id, duration, and timeout/exit metadata. Optional context fields
    (review_round, retry_count) are recorded when present.

    These tests exercise the in-memory `recorded_events` capture on the
    fake; the same records are written to disk when EVENT_LOG_PATH is set
    (the StageEventEmissionTest covers the on-disk surface).
    """

    @staticmethod
    def _events(gh, event_name: str) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == event_name]

    def test_fresh_developer_spawn_emits_paired_lifecycle_events(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-dev", last_message="q?"),
            has_new_commits=False,
        )
        spawns = self._events(gh, "agent_spawn")
        exits = self._events(gh, "agent_exit")
        self.assertEqual(len(spawns), 1)
        self.assertEqual(len(exits), 1)
        spawn = spawns[0]
        ex = exits[0]
        self.assertEqual(spawn["stage"], "implementing")
        self.assertEqual(spawn["agent_role"], "developer")
        self.assertEqual(spawn["agent"], config.DEV_AGENT)
        self.assertNotIn("session_id", spawn)  # fresh spawn -- no resume id
        self.assertEqual(ex["session_id"], "sess-dev")
        self.assertEqual(ex["exit_code"], 0)
        self.assertFalse(ex["timed_out"])
        self.assertIn("duration_s", ex)
        self.assertGreaterEqual(ex["duration_s"], 0)
        # retry_count is incremented to 1 by `_check_and_increment_retry_budget`
        # BEFORE the spawn, so the recorded value is what the agent ran under.
        self.assertEqual(spawn["retry_count"], 1)
        self.assertEqual(ex["retry_count"], 1)

    def test_reviewer_spawn_carries_review_round_and_retry_count(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=42,
            head_branch="orchestrator/issue-2",
            base_branch="main",
            mergeable=True,
            check_state="success",
            approved=False,
        )
        gh.add_pr(pr)
        # Seed both `review_round` and `retry_count` so both optional
        # context fields ride along on the reviewer's spawn/exit events.
        gh.seed_state(2, pr_number=42, review_round=1, retry_count=2)
        # Patch _latest_pr_comment_ids so it doesn't touch real GitHub.
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-review", last_message="VERDICT: APPROVED",
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        spawns = self._events(gh, "agent_spawn")
        exits = self._events(gh, "agent_exit")
        reviewer_spawns = [s for s in spawns if s["agent_role"] == "reviewer"]
        reviewer_exits = [e for e in exits if e["agent_role"] == "reviewer"]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(len(reviewer_exits), 1)
        self.assertEqual(reviewer_spawns[0]["stage"], "validating")
        self.assertEqual(reviewer_spawns[0]["agent"], config.REVIEW_AGENT)
        self.assertEqual(reviewer_spawns[0]["review_round"], 1)
        self.assertEqual(reviewer_spawns[0]["retry_count"], 2)
        self.assertEqual(reviewer_exits[0]["review_round"], 1)
        self.assertEqual(reviewer_exits[0]["retry_count"], 2)
        self.assertEqual(reviewer_exits[0]["session_id"], "sess-review")

    def test_dev_resume_spawn_carries_session_id(self) -> None:
        # A resume hands the spawn event the existing session id; the exit
        # event records the (same) live id from the AgentResult.
        gh = FakeGitHubClient()
        issue = make_issue(3, label="implementing")
        issue.comments.append(FakeComment(id=2000, body="please retry"))
        gh.add_issue(issue)
        gh.seed_state(
            3, awaiting_human=True, last_action_comment_id=1500,
            dev_agent="codex", dev_session_id="sess-resume",
        )
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-resume", last_message="q?"),
            has_new_commits=False,
        )
        spawns = self._events(gh, "agent_spawn")
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]["agent_role"], "developer")
        self.assertEqual(spawns[0]["session_id"], "sess-resume")

    def test_timeout_records_timed_out_flag_on_exit(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True, last_message=""),
            has_new_commits=False,
        )
        exits = self._events(gh, "agent_exit")
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["timed_out"])
        self.assertEqual(exits[0]["exit_code"], -1)


def _codex_stdout_no_model(
    *,
    input_tokens: int = 2000,
    cached: int = 500,
    output_tokens: int = 800,
) -> str:
    """Build a codex --json stdout with usage frames but NO model field.

    Reproduces the case the reviewer flagged: codex sometimes emits a
    usage frame on resume / minimal completions whose `model` is
    missing. Without `fallback_model` the parser tags the run
    `unknown-price` with `models=[]`; with the fallback it should
    populate `models` with the configured model and -- when priced --
    produce an `estimated` cost.
    """
    return json.dumps({
        "type": "turn_complete",
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
        },
    })


def _claude_stdout(
    *,
    msg_id: str = "msg-1",
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1234,
    output_tokens: int = 567,
    cache_read: int = 100,
    cache_write_5m: int = 80,
    total_cost_usd: Optional[float] = None,
    num_turns: int = 2,
) -> str:
    """Build a minimal claude stream-json stdout the usage parser understands.

    Mirrors the shape `parse_claude_usage` reads: one assistant frame with
    `message.usage` and one terminal `result` frame carrying `num_turns`
    (and `total_cost_usd` when the agent self-reports it).
    """
    assistant = {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write_5m,
            },
        },
    }
    result_frame = {"type": "result", "num_turns": num_turns}
    if total_cost_usd is not None:
        result_frame["total_cost_usd"] = total_cost_usd
    return "\n".join([json.dumps(assistant), json.dumps(result_frame)])


class AgentAnalyticsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_run_agent_tracked` appends a single analytics record per agent
    exit, carrying the configured spec, resume/session context, retry
    budget, reviewer round, duration, exit metadata, parsed token
    counts, model list, cost, and cost_source -- and never the prompt,
    raw stdout, stderr, or any auth header. The existing audit
    `agent_spawn` / `agent_exit` events must continue to fire unchanged.
    """

    @staticmethod
    def _exit_records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_implementing_spawn_appends_analytics_record(self) -> None:
        # End-to-end: an implementing tick spawns the dev agent, the
        # wrapper parses usage from a realistic claude stream-json stdout
        # and appends one well-formed JSONL line to the configured sink.
        with tempfile.TemporaryDirectory(prefix="analytics-impl-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout(total_cost_usd=0.0123)
            gh = FakeGitHubClient()
            issue = make_issue(101, label="implementing")
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-impl",
                    last_message="open question?",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            # Audit context — same shape `agent_exit` uses, so an
            # operator can correlate sinks one-to-one.
            self.assertEqual(rec["event"], "agent_exit")
            self.assertEqual(rec["repo"], "geserdugarov/agent-orchestrator")
            self.assertEqual(rec["issue"], 101)
            self.assertEqual(rec["stage"], "implementing")
            self.assertEqual(rec["agent_role"], "developer")
            self.assertEqual(rec["backend"], config.DEV_AGENT)
            # Configured spec: implementing's fresh-spawn branch persists
            # DEV_AGENT_SPEC in pinned state before invoking the wrapper.
            self.assertEqual(rec["agent_spec"], config.DEV_AGENT_SPEC)
            self.assertEqual(rec["session_id"], "sess-impl")
            self.assertNotIn("resume_session_id", rec)  # fresh spawn
            self.assertEqual(rec["exit_code"], 0)
            self.assertFalse(rec["timed_out"])
            self.assertGreaterEqual(rec["duration_s"], 0)
            # Parsed usage from the synthetic claude stream-json stdout.
            self.assertEqual(rec["input_tokens"], 1234)
            self.assertEqual(rec["output_tokens"], 567)
            self.assertEqual(rec["cache_read_tokens"], 100)
            self.assertEqual(rec["cache_write_tokens"], 80)
            self.assertEqual(rec["models"], ["claude-sonnet-4-6"])
            self.assertEqual(rec["turns"], 2)
            # Reported cost wins over the price-table estimate.
            self.assertEqual(rec["cost_source"], "reported")
            self.assertAlmostEqual(rec["cost_usd"], 0.0123)
            # retry_count was incremented to 1 by the budget check
            # before the spawn (the spawn ran under retry budget #1).
            self.assertEqual(rec["retry_count"], 1)

    def test_record_excludes_prompt_stdout_stderr_and_secrets(self) -> None:
        # The sink is a usage/cost surface, not a debugging mirror.
        # `result.stdout` may contain user-issue text and we must never
        # store it (nor the prompt the agent was sent, nor stderr which
        # can leak token-shaped strings from CLI banners).
        with tempfile.TemporaryDirectory(prefix="analytics-redaction-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout()
            secret_marker = "ghp_DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEAD"
            stderr_marker = f"WARN missing scope for {secret_marker}"
            gh = FakeGitHubClient()
            issue = make_issue(
                102,
                label="implementing",
                body=f"please use token {secret_marker}",
            )
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-redact",
                    last_message="q?",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr=stderr_marker,
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            blob = json.dumps(records[0])
            # The configured token, the prompt body, the stderr tail, and
            # the raw stdout must all stay out of the record.
            self.assertNotIn(secret_marker, blob)
            self.assertNotIn("please use token", blob)
            self.assertNotIn("missing scope", blob)
            self.assertNotIn(stdout, blob)
            # Prompt-shaped fields must be absent.
            for forbidden in (
                "prompt", "stdout", "stderr", "last_message", "cwd",
            ):
                self.assertNotIn(forbidden, records[0])

    def test_reviewer_record_carries_review_round_and_resume_context(
        self,
    ) -> None:
        # Reviewer spawn carries `agent_spec=REVIEW_AGENT_SPEC` and the
        # current review_round / retry_count; the wrapper records both
        # `resume_session_id` (None for the fresh reviewer) and the
        # `session_id` the AgentResult surfaced.
        with tempfile.TemporaryDirectory(prefix="analytics-review-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout(msg_id="msg-review")
            gh = FakeGitHubClient()
            issue = make_issue(103, label="validating")
            gh.add_issue(issue)
            pr = FakePR(
                number=44,
                head_branch="orchestrator/issue-103",
                base_branch="main",
                mergeable=True,
                check_state="success",
                approved=False,
            )
            gh.add_pr(pr)
            gh.seed_state(103, pr_number=44, review_round=2, retry_count=3)
            with patch.object(
                workflow, "_latest_pr_comment_ids",
                return_value=(None, None),
            ):
                self._run(
                    lambda: workflow._handle_validating(
                        gh, _TEST_SPEC, issue,
                    ),
                    run_agent=AgentResult(
                        session_id="sess-review",
                        last_message="VERDICT: APPROVED",
                        exit_code=0,
                        timed_out=False,
                        stdout=stdout,
                        stderr="",
                    ),
                    head_shas=[pr.head.sha, pr.head.sha],
                    analytics_log_path=path,
                )

            records = self._exit_records(path)
            reviewer = [
                r for r in records if r.get("agent_role") == "reviewer"
            ]
            self.assertEqual(len(reviewer), 1)
            rec = reviewer[0]
            self.assertEqual(rec["stage"], "validating")
            self.assertEqual(rec["backend"], config.REVIEW_AGENT)
            self.assertEqual(rec["agent_spec"], config.REVIEW_AGENT_SPEC)
            self.assertEqual(rec["review_round"], 2)
            self.assertEqual(rec["retry_count"], 3)
            self.assertEqual(rec["session_id"], "sess-review")
            # Reviewer always spawns fresh; the wrapper drops None-valued
            # extras so `resume_session_id` is absent (not stored as null).
            self.assertNotIn("resume_session_id", rec)

    def test_timeout_records_exit_metadata_and_no_cost(self) -> None:
        # A timed-out agent has empty stdout; the parser yields the
        # `no-usage` sentinel and `cost_usd` stays unset rather than
        # being stored as null. The exit metadata still rides along.
        with tempfile.TemporaryDirectory(prefix="analytics-timeout-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(104, label="implementing")
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id=None,
                    last_message="",
                    exit_code=-1,
                    timed_out=True,
                    stdout="",
                    stderr="",
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["exit_code"], -1)
            self.assertTrue(rec["timed_out"])
            self.assertEqual(rec["cost_source"], "no-usage")
            self.assertNotIn("cost_usd", rec)
            self.assertEqual(rec["input_tokens"], 0)
            self.assertEqual(rec["output_tokens"], 0)

    def test_audit_events_unchanged_alongside_analytics_record(self) -> None:
        # Preserving the existing audit schema is a hard requirement:
        # one `agent_spawn` + one `agent_exit` per invocation, both
        # appearing in the in-memory capture even though the analytics
        # sink also writes a single record to disk.
        with tempfile.TemporaryDirectory(prefix="analytics-audit-") as td:
            path = Path(td) / "analytics.jsonl"
            stdout = _claude_stdout()
            gh = FakeGitHubClient()
            issue = make_issue(105, label="implementing")
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-x",
                    last_message="q?",
                    exit_code=0,
                    timed_out=False,
                    stdout=stdout,
                    stderr="",
                ),
                has_new_commits=False,
                analytics_log_path=path,
            )

            spawns = [
                e for e in gh.recorded_events if e["event"] == "agent_spawn"
            ]
            exits = [
                e for e in gh.recorded_events if e["event"] == "agent_exit"
            ]
            self.assertEqual(len(spawns), 1)
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0]["session_id"], "sess-x")
            self.assertEqual(exits[0]["exit_code"], 0)
            # And exactly one analytics record for the same invocation.
            self.assertEqual(len(self._exit_records(path)), 1)

    def test_disabled_sink_writes_no_analytics_file(self) -> None:
        # `ANALYTICS_LOG_PATH=None` is the documented disable knob;
        # `_run_agent_tracked` must still fire the audit events but the
        # sink path must not be created. The `_run` default already
        # patches `ANALYTICS_LOG_PATH=None`, so the sentinel must stay
        # absent without any opt-in from this test.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-exist.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(106, label="implementing")
            gh.add_issue(issue)
            self._run(
                lambda: workflow._handle_implementing(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=AgentResult(
                    session_id="sess-off",
                    last_message="q?",
                    exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout(),
                    stderr="",
                ),
                has_new_commits=False,
            )
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])
            # Audit events are still captured in memory.
            self.assertIn(
                "agent_exit",
                {e["event"] for e in gh.recorded_events},
            )

    def test_codex_stream_without_model_uses_spec_fallback(self) -> None:
        # Reviewer-flagged regression: a codex run whose stdout includes
        # usage frames but omits the `model` field used to record
        # `models=[]` and `cost_source="unknown-price"` even when the
        # configured spec named a priced model. `_run_agent_tracked`
        # must pull the model out of `extra_args` (`-m gpt-5-codex`)
        # and pass it to `usage.parse_agent_usage` as `fallback_model`
        # so the spec-known model both labels the record and enables
        # the price-table estimate.
        with tempfile.TemporaryDirectory(prefix="analytics-codex-fallback-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "run_agent") as run_mock:
                run_mock.return_value = AgentResult(
                    session_id="sess-codex",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_codex_stdout_no_model(),
                    stderr="",
                )
                gh = FakeGitHubClient()
                workflow._run_agent_tracked(
                    gh, 107,
                    agent_role="developer",
                    stage="implementing",
                    backend="codex",
                    prompt="ignored",
                    cwd=_FAKE_WT,
                    agent_spec="codex -m gpt-5-codex",
                    extra_args=("-m", "gpt-5-codex"),
                    retry_count=1,
                )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec["backend"], "codex")
            self.assertEqual(rec["agent_spec"], "codex -m gpt-5-codex")
            # Fallback wired the configured model into both the model
            # list and the cost estimate.
            self.assertEqual(rec["models"], ["gpt-5-codex"])
            self.assertEqual(rec["cost_source"], "estimated")
            self.assertIn("cost_usd", rec)
            self.assertGreater(rec["cost_usd"], 0)
            # Parsed counts come from the codex usage frame verbatim.
            self.assertEqual(rec["input_tokens"], 2000)
            self.assertEqual(rec["cached_tokens"], 500)
            self.assertEqual(rec["output_tokens"], 800)

    def test_claude_stream_with_model_ignores_spec_fallback(self) -> None:
        # Companion guard: when the stream itself carries a model
        # (claude always does, codex usually does), the spec fallback
        # must not override it. The configured spec names a different
        # model than the stream's `message.model`; the record should
        # reflect the stream-reported model, not the fallback.
        with tempfile.TemporaryDirectory(prefix="analytics-claude-fallback-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "run_agent") as run_mock:
                run_mock.return_value = AgentResult(
                    session_id="sess-claude",
                    last_message="",
                    exit_code=0,
                    timed_out=False,
                    stdout=_claude_stdout(model="claude-sonnet-4-6"),
                    stderr="",
                )
                gh = FakeGitHubClient()
                workflow._run_agent_tracked(
                    gh, 108,
                    agent_role="developer",
                    stage="implementing",
                    backend="claude",
                    prompt="ignored",
                    cwd=_FAKE_WT,
                    agent_spec="claude --model claude-opus-4-7",
                    extra_args=("--model", "claude-opus-4-7"),
                    retry_count=1,
                )

            records = self._exit_records(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["models"], ["claude-sonnet-4-6"])


class ConfiguredModelExtractionTest(unittest.TestCase):
    """`_configured_model` is the tiny shim that converts an `extra_args`
    tuple into the model fallback `usage.parse_agent_usage` consumes.
    Both the split (`-m gpt-5`) and `=`-glued (`--model=opus-4`) shapes
    must survive because `shlex.split` produces either depending on the
    operator's quoting.
    """

    def test_codex_dash_m_split_form(self) -> None:
        self.assertEqual(
            workflow._configured_model("codex", ("-m", "gpt-5-codex")),
            "gpt-5-codex",
        )

    def test_codex_dash_m_equals_form(self) -> None:
        self.assertEqual(
            workflow._configured_model("codex", ("-m=gpt-5-codex",)),
            "gpt-5-codex",
        )

    def test_claude_long_flag_split_form(self) -> None:
        self.assertEqual(
            workflow._configured_model(
                "claude", ("--model", "claude-opus-4-7"),
            ),
            "claude-opus-4-7",
        )

    def test_claude_long_flag_equals_form(self) -> None:
        self.assertEqual(
            workflow._configured_model(
                "claude", ("--model=claude-opus-4-7",),
            ),
            "claude-opus-4-7",
        )

    def test_returns_none_when_flag_absent(self) -> None:
        # No `-m` / `--model` in the spec -- the parser keeps its
        # "unknown" handling rather than receiving an empty string.
        self.assertIsNone(workflow._configured_model("codex", ()))
        self.assertIsNone(
            workflow._configured_model("claude", ("--effort", "high")),
        )

    def test_codex_ignores_claude_flag(self) -> None:
        # `--model` is a claude flag; for a codex spec the helper must
        # not pick it up. If an operator typed the wrong flag for the
        # wrong backend, the analytics fallback stays empty rather than
        # mislabeling.
        self.assertIsNone(
            workflow._configured_model(
                "codex", ("--model", "gpt-5-codex"),
            ),
        )

    def test_trailing_flag_without_value_returns_none(self) -> None:
        # Defensive: a stray `-m` at the end of extra_args (which a
        # bad spec could produce) must not raise.
        self.assertIsNone(workflow._configured_model("codex", ("-m",)))


class ReviewVerdictEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_handle_validating` emits a `review_verdict` event after parsing the
    reviewer agent's final message, so an operator tailing the JSONL sink
    sees approve/changes-requested decisions inline with the rest of the
    workflow trace.
    """

    def _seeded(self, last_message: str):
        gh = FakeGitHubClient()
        issue = make_issue(5, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=99,
            head_branch="orchestrator/issue-5",
            base_branch="main",
            mergeable=True,
            check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(5, pr_number=99, review_round=0)
        return gh, issue, pr, last_message

    def _run_validating(self, gh, issue, pr, last_message: str):
        # Enough head_shas to cover both the approved branch (reviewed_sha +
        # squash inputs) and the changes_requested branch (before/after the
        # dev fix). Identical SHAs across the sequence mean the dev fix is
        # treated as a no-op question (we only care about the verdict event).
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-review", last_message=last_message,
                ),
                head_shas=[pr.head.sha] * 6,
            )

    def test_approved_verdict_emits_event(self) -> None:
        gh, issue, pr, last = self._seeded("LGTM\n\nVERDICT: APPROVED")
        self._run_validating(gh, issue, pr, last)
        verdicts = [e for e in gh.recorded_events if e["event"] == "review_verdict"]
        self.assertEqual(len(verdicts), 1)
        v = verdicts[0]
        self.assertEqual(v["verdict"], "approved")
        self.assertEqual(v["stage"], "validating")
        self.assertEqual(v["review_round"], 0)
        self.assertEqual(v["pr_number"], 99)
        self.assertEqual(v["session_id"], "sess-review")

    def test_changes_requested_verdict_emits_event(self) -> None:
        gh, issue, pr, last = self._seeded(
            "1. Add a test\n\nVERDICT: CHANGES_REQUESTED",
        )
        self._run_validating(gh, issue, pr, last)
        verdicts = [e for e in gh.recorded_events if e["event"] == "review_verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "changes_requested")

    def test_unknown_verdict_emits_event(self) -> None:
        gh, issue, pr, last = self._seeded("no marker here")
        self._run_validating(gh, issue, pr, last)
        verdicts = [e for e in gh.recorded_events if e["event"] == "review_verdict"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "unknown")


class ParkAwaitingHumanEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Every park path (the shared `_park_awaiting_human` helper plus the
    inline `_on_question` / `_on_dirty_worktree` helpers) emits a
    `park_awaiting_human` event tagged with the current stage and an
    optional `reason` so the JSONL sink mirrors the durable `park_reason`
    field for the operator.
    """

    @staticmethod
    def _parks(gh) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == "park_awaiting_human"]

    def test_agent_question_emits_park_event_with_reason_and_stage(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(6, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="please clarify the scope"),
            has_new_commits=False,
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "implementing")
        self.assertEqual(parks[0]["reason"], "agent_question")

    def test_agent_silent_emits_park_event_with_reason(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(7, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="", exit_code=1),
            has_new_commits=False,
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["reason"], "agent_silent")

    def test_reviewer_timeout_emits_park_event_with_reason(self) -> None:
        # Reviewer agent timeout during validating routes through
        # `_park_awaiting_human(reason="reviewer_timeout")` directly.
        gh = FakeGitHubClient()
        issue = make_issue(8, label="validating")
        gh.add_issue(issue)
        gh.seed_state(8, pr_number=42, review_round=1)
        pr = FakePR(
            number=42, head_branch="orchestrator/issue-8",
            base_branch="main", mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(timed_out=True, last_message=""),
                head_shas=[pr.head.sha],
            )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "validating")
        self.assertEqual(parks[0]["reason"], "reviewer_timeout")

    def test_shared_helper_park_carries_reason_for_review_cap(self) -> None:
        # `_handle_validating`'s review-cap exhaustion calls
        # `_park_awaiting_human(reason="review_cap")` directly -- a pure
        # shared-helper park path (no transient `state.set("park_reason",
        # ...)` follow-up like the timeout sites have). The emitted event
        # must still carry the reason.
        gh = FakeGitHubClient()
        issue = make_issue(10, label="validating")
        gh.add_issue(issue)
        # Seed review_round at the cap so the very first tick parks.
        gh.seed_state(
            10, pr_number=33, review_round=config.MAX_REVIEW_ROUNDS,
        )
        pr = FakePR(
            number=33, head_branch="orchestrator/issue-10",
            base_branch="main", mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "validating")
        self.assertEqual(parks[0]["reason"], "review_cap")

    def test_push_failed_in_on_commits_carries_reason(self) -> None:
        # `_on_commits` is reached via `_handle_implementing` after the
        # agent committed; a failing push routes through
        # `_park_awaiting_human(reason="push_failed")`. Representative
        # test for a helper-only park outside the validating handler.
        gh = FakeGitHubClient()
        issue = make_issue(11, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-x", last_message="done"),
            has_new_commits=True,
            push_branch=False,  # simulate push failure
        )
        parks = self._parks(gh)
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0]["stage"], "implementing")
        self.assertEqual(parks[0]["reason"], "push_failed")

    def test_no_park_event_when_run_does_not_park(self) -> None:
        # A clean approval run flips to in_review without parking; no
        # `park_awaiting_human` event should be recorded.
        gh = FakeGitHubClient()
        issue = make_issue(9, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=11, head_branch="orchestrator/issue-9",
            base_branch="main", mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(9, pr_number=11, review_round=0)
        with patch.object(
            workflow, "_latest_pr_comment_ids", return_value=(None, None)
        ):
            self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="sess-r", last_message="ok\n\nVERDICT: APPROVED",
                ),
                head_shas=[pr.head.sha, pr.head.sha],
            )
        self.assertEqual(self._parks(gh), [])


class PrLifecycleEventEmissionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`pr_opened`, `merge_attempt`, `conflict_round`, `pr_merged`, and
    `pr_closed_without_merge` are emitted from the in_review and
    resolving_conflict handlers so an operator tailing the JSONL sink sees
    the PR-side of each issue's lifecycle (open / conflict round /
    terminal external merge / terminal reject) without scraping the
    orchestrator log. `merge_attempt` is only emitted by
    `_handle_resolving_conflict` for the base rebase; the in_review
    handler is permanently manual-merge-only and never emits it.
    """

    BRANCH = "orchestrator/issue-50"
    PR_NUMBER = 500

    @staticmethod
    def _events_of(gh, event_name: str) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == event_name]

    def _open_pr(self, **kwargs):
        defaults = dict(
            number=self.PR_NUMBER,
            head_branch=self.BRANCH,
            head=FakePRRef(sha="abc12345"),
        )
        defaults.update(kwargs)
        return FakePR(**defaults)

    def _seed_in_review(self, issue_number=50, *, pr=None, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label="in_review")
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state = dict(
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
        )
        if pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def test_pr_opened_event_on_fresh_pr_open(self) -> None:
        # _handle_implementing -> _on_commits opens a new PR and emits
        # `pr_opened` with the pr number and branch.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="implementing")
        gh.add_issue(issue)
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First call: recovered-worktree check (False) -> agent runs;
            # second call: post-agent _has_new_commits check (True) -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )
        opened = self._events_of(gh, "pr_opened")
        self.assertEqual(len(opened), 1)
        ev = opened[0]
        self.assertEqual(ev["stage"], "implementing")
        self.assertEqual(ev["issue"], 50)
        self.assertEqual(ev["pr_number"], gh.opened_prs[0].number)
        self.assertEqual(ev["branch"], "orchestrator/issue-50")
        # `sha` carries the PR head sha from `pr.head.sha` so the audit
        # sink can correlate the open event with later merge / review IDs.
        self.assertEqual(ev["sha"], gh.opened_prs[0].head.sha)

    def test_pr_opened_not_emitted_when_reusing_existing_pr(self) -> None:
        # Recovery path: an existing open PR is reused rather than opened
        # again. The PR was already announced on its earlier tick, so no
        # `pr_opened` event should fire here.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="implementing")
        gh.add_issue(issue)
        existing = FakePR(number=123, head_branch="orchestrator/issue-51")
        gh.existing_open_pr["orchestrator/issue-51"] = existing
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            has_new_commits=[False, True],
            push_branch=True,
        )
        self.assertEqual(self._events_of(gh, "pr_opened"), [])

    def test_in_review_mergeable_does_not_emit_merge_events(self) -> None:
        # The orchestrator is manual-merge-only: a mergeable PR in_review
        # never produces a `merge_attempt` or orchestrator-initiated
        # `pr_merged` event. The HITL ping is observable instead.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed_in_review(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertEqual(self._events_of(gh, "merge_attempt"), [])
        self.assertEqual(self._events_of(gh, "pr_merged"), [])
        # And no orchestrator-driven label flip to `done`.
        self.assertNotIn((50, "done"), gh.label_history)

    def test_pr_merged_event_on_external_merge_terminal(self) -> None:
        # A human (or another bot) merged the PR while we were in_review.
        # The terminal handler stamps `merged_at` and emits `pr_merged`
        # with `merge_method=external`.
        pr = self._open_pr(merged=True, state="closed")
        gh, issue = self._seed_in_review(
            pr=pr, extra_state={"conflict_round": 2},
        )
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        merged = self._events_of(gh, "pr_merged")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["merge_method"], "external")
        self.assertEqual(merged[0]["pr_number"], self.PR_NUMBER)
        self.assertEqual(merged[0]["sha"], "abc12345")
        # In-review terminals carry the round counters from state so an
        # operator tailing the sink can attribute merges to the round count
        # that produced them, not just the issue number.
        self.assertEqual(merged[0]["review_round"], 1)
        self.assertEqual(merged[0]["conflict_round"], 2)
        # The orchestrator is permanently manual-merge-only and never
        # emits `merge_attempt` from in_review.
        self.assertEqual(self._events_of(gh, "merge_attempt"), [])

    def test_pr_closed_without_merge_event_on_terminal(self) -> None:
        pr = self._open_pr(merged=False, state="closed")
        gh, issue = self._seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        closed = self._events_of(gh, "pr_closed_without_merge")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["stage"], "in_review")
        self.assertEqual(closed[0]["pr_number"], self.PR_NUMBER)

    def test_in_review_unmergeable_does_not_emit_conflict_round(self) -> None:
        # The orchestrator no longer routes from in_review to
        # `resolving_conflict` on an unmergeable gate. An unmergeable PR
        # parks awaiting human, so no `conflict_round` event is emitted
        # from this stage.
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed_in_review(pr=pr)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertEqual(self._events_of(gh, "conflict_round"), [])
        self.assertNotIn((50, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(50).get("awaiting_human"))


class EventEmissionDisabledTest(unittest.TestCase, _PatchedWorkflowMixin):
    """When EVENT_LOG_PATH is unset (the default), no JSONL file is opened
    and the orchestrator's observable behavior -- comments posted, labels
    set, pinned state written -- is identical to a deployment without the
    audit sink. The in-memory `recorded_events` capture is always populated
    so workflow tests can assert on it without configuring a sink.
    """

    def test_disabled_sink_does_not_change_behavior(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evlog-disabled-") as td:
            sentinel = Path(td) / "should-not-exist.jsonl"
            with patch.object(config, "EVENT_LOG_PATH", None):
                gh = FakeGitHubClient()
                issue = make_issue(20, label="implementing")
                gh.add_issue(issue)
                self._run(
                    lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                    run_agent=_agent(last_message="q?"),
                    has_new_commits=False,
                )
            # Disk file is never created.
            self.assertFalse(sentinel.exists())
            # Behavior unchanged: a comment was posted, awaiting_human set,
            # and the various lifecycle events captured in-memory.
            self.assertEqual(len(gh.posted_comments), 1)
            self.assertTrue(gh.pinned_data(20).get("awaiting_human"))
            event_names = {e["event"] for e in gh.recorded_events}
            self.assertIn("agent_spawn", event_names)
            self.assertIn("agent_exit", event_names)
            self.assertIn("park_awaiting_human", event_names)


class ListPollableIssuesTest(unittest.TestCase):
    """Closed-but-`in_review` issues must still be picked up so external
    manual merges (which auto-close the linked issue via "Resolves #N") get
    finalized to `done` instead of being silently dropped."""

    def test_open_only_when_no_in_review_closed(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        gh.add_issue(make_issue(2, label="validating"))
        out = list(gh.list_pollable_issues())
        self.assertEqual({i.number for i in out}, {1, 2})

    def test_includes_closed_in_review_for_external_merge_finalization(self) -> None:
        gh = FakeGitHubClient()
        open_issue = make_issue(1, label="implementing")
        closed_in_review = make_issue(7, label="in_review")
        closed_in_review.closed = True
        # Closed but no in_review label: must be skipped (already finalized).
        closed_done = make_issue(8, label="done")
        closed_done.closed = True
        for i in (open_issue, closed_in_review, closed_done):
            gh.add_issue(i)
        out = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(out, {1, 7})

    def test_includes_closed_question_for_terminal_cleanup(self) -> None:
        # A human closing a `question`-labeled Q&A issue is the terminal
        # signal `_handle_question` consumes to finalize the issue to
        # `done` and clean up the per-issue worktree/branch. Without the
        # closed-issue sweep including `question`, the dispatcher would
        # never re-visit the closed issue and the worktree would linger.
        gh = FakeGitHubClient()
        open_issue = make_issue(1, label="implementing")
        closed_question = make_issue(9, label="question")
        closed_question.closed = True
        for i in (open_issue, closed_question):
            gh.add_issue(i)
        out = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(out, {1, 9})


class WorktreePathSlugNamespaceTest(unittest.TestCase):
    """Two repos with the same issue number must produce distinct worktree
    paths, otherwise simultaneous orchestration of both would have them
    fighting over the same `WORKTREES_DIR/issue-N` checkout. The slug
    sanitizer also has to produce a single filesystem-safe segment
    (no `/`, no leading `.`) since it becomes a directory name.
    """

    def _spec(self, slug: str) -> config.RepoSpec:
        return config.RepoSpec(
            slug=slug,
            target_root=Path(f"/tmp/{workflow._sanitize_slug(slug)}-target"),
            base_branch="main",
        )

    def test_same_issue_number_different_slugs_no_collision(self) -> None:
        spec_a = self._spec("alice/repo")
        spec_b = self._spec("bob/repo")
        path_a = workflow._worktree_path(spec_a, 7)
        path_b = workflow._worktree_path(spec_b, 7)

        self.assertNotEqual(path_a, path_b)
        # Both must live under WORKTREES_DIR with the issue-N leaf.
        self.assertEqual(path_a.name, "issue-7")
        self.assertEqual(path_b.name, "issue-7")
        self.assertEqual(path_a.parent.parent, config.WORKTREES_DIR)
        self.assertEqual(path_b.parent.parent, config.WORKTREES_DIR)

    def test_decompose_path_also_namespaced_by_slug(self) -> None:
        spec_a = self._spec("alice/repo")
        spec_b = self._spec("bob/repo")
        self.assertNotEqual(
            workflow._decompose_worktree_path(spec_a, 7),
            workflow._decompose_worktree_path(spec_b, 7),
        )

    def test_implement_and_decompose_share_repo_namespace(self) -> None:
        # `WORKTREES_DIR/<slug>/issue-N` and `WORKTREES_DIR/<slug>/decompose-N`
        # share the per-repo subdirectory so cleanup on the parent dir
        # also reaps the decomposer scratch.
        spec = self._spec("owner/name")
        impl = workflow._worktree_path(spec, 11)
        dec = workflow._decompose_worktree_path(spec, 11)
        self.assertEqual(impl.parent, dec.parent)

    def test_sanitize_slug_replaces_owner_separator(self) -> None:
        self.assertEqual(workflow._sanitize_slug("owner/name"), "owner__name")

    def test_sanitize_slug_is_a_single_segment(self) -> None:
        # A directory name with `/` would split into nested directories,
        # defeating the point of namespacing.
        for raw in (
            "owner/name",
            "deep/owner/name",
            "name-only",
            "weird name with spaces",
        ):
            cleaned = workflow._sanitize_slug(raw)
            self.assertNotIn("/", cleaned, f"slug={raw!r} -> {cleaned!r}")

    def test_sanitize_slug_no_leading_dot(self) -> None:
        # Hidden directories (.foo) hide the worktree from a casual
        # operator inspection; escape leading dots.
        self.assertFalse(workflow._sanitize_slug(".dotfile/repo").startswith("."))
        self.assertFalse(workflow._sanitize_slug("./repo").startswith("."))

    def test_sanitize_slug_strips_unsafe_chars(self) -> None:
        cleaned = workflow._sanitize_slug("owner@#$/name with spaces")
        # No path separator, no shell-special chars; only [A-Za-z0-9_.-]
        for ch in cleaned:
            self.assertTrue(
                ch.isalnum() or ch in "_.-",
                f"unexpected char {ch!r} in {cleaned!r}",
            )

    def test_sanitize_slug_empty_input_falls_back(self) -> None:
        # Empty would collapse `WORKTREES_DIR/<slug>/issue-N` into
        # `WORKTREES_DIR/issue-N`, reintroducing the cross-repo collision.
        self.assertNotEqual(workflow._sanitize_slug(""), "")
        self.assertNotEqual(workflow._sanitize_slug(""), ".")

    def test_default_repo_spec_path_format(self) -> None:
        # Anchor the documented `<owner>__<name>/issue-N` layout.
        spec = config.RepoSpec(
            slug="geserdugarov/agent-orchestrator",
            target_root=Path("/tmp/x"),
            base_branch="main",
        )
        path = workflow._worktree_path(spec, 9)
        self.assertEqual(
            path,
            config.WORKTREES_DIR / "geserdugarov__agent-orchestrator" / "issue-9",
        )


class CleanupTerminalBranchTest(unittest.TestCase):
    """Direct coverage of `_cleanup_terminal_branch`. The handler-level
    tests patch this helper out so they only check it was invoked; here we
    run the real implementation with `_git` mocked to verify the worktree
    removal, local branch delete, and remote branch delete each fire (and
    that an absent worktree is silently skipped instead of erroring). Also
    verifies the helper never raises on subprocess / remote failures, so
    a cleanup hiccup cannot block the terminal label flip in the caller.
    """

    ISSUE_NUMBER = 99
    BRANCH = "orchestrator/issue-99"

    def _run_helper(
        self,
        *,
        worktree_exists: bool,
        local_branch_exists: bool,
    ):
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        rev_parse_rc = 0 if local_branch_exists else 1

        def fake_git(*args, cwd):
            cmd = args[0]
            if cmd == "worktree":
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd == "rev-parse":
                return MagicMock(returncode=rev_parse_rc, stderr="", stdout="")
            if cmd == "branch":
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        git_mock = MagicMock(side_effect=fake_git)

        # `_worktree_path` returns a Path that may or may not exist on disk;
        # patch its existence check rather than touching the real filesystem.
        wt_path = MagicMock()
        wt_path.exists.return_value = worktree_exists
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupTerminalBranchTest.ISSUE_NUMBER}"

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            workflow._cleanup_terminal_branch(gh, _TEST_SPEC, self.ISSUE_NUMBER)
        return gh, git_mock

    def test_full_cleanup_runs_all_three_steps(self) -> None:
        gh, git_mock = self._run_helper(
            worktree_exists=True, local_branch_exists=True,
        )

        # Worktree remove issued first, then rev-parse to probe the local
        # branch, then `branch -D`. The remote-side delete recorder confirms
        # gh.delete_remote_branch was called with the per-issue branch.
        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertEqual(
            cmds[:3],
            ["worktree", "rev-parse", "branch"],
        )
        # The branch -D invocation targets the per-issue branch by name.
        branch_call = next(
            c for c in git_mock.call_args_list if c.args[0] == "branch"
        )
        self.assertEqual(branch_call.args[1], "-D")
        self.assertEqual(branch_call.args[2], self.BRANCH)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_skips_worktree_remove_when_worktree_absent(self) -> None:
        # Worktree may already be gone if the operator cleaned it up by hand
        # or a prior tick removed it. Helper should still drop the local
        # branch and request the remote delete instead of erroring out.
        gh, git_mock = self._run_helper(
            worktree_exists=False, local_branch_exists=True,
        )

        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertNotIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_skips_local_delete_when_branch_absent(self) -> None:
        # Branch may already be gone if a previous cleanup partly succeeded
        # or the operator pruned it. We must not run `branch -D` (it would
        # fail loudly), but must still request the remote delete.
        gh, git_mock = self._run_helper(
            worktree_exists=True, local_branch_exists=False,
        )

        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertNotIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_swallows_all_failures(self) -> None:
        # Every step is best-effort: worktree-remove failure, branch -D
        # failure, and a raising remote-delete must all be absorbed so a
        # cleanup hiccup cannot block the caller (which has already
        # written the terminal pinned state). Regression guard for the
        # "no runtime exception should escape cleanup" contract.
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        def fake_git(*args, cwd):
            cmd = args[0]
            # rev-parse returns 0 so we proceed to `branch -D`; both the
            # worktree and branch deletions return non-zero stderr so we
            # exercise both warning paths.
            if cmd == "rev-parse":
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=1, stderr="boom", stdout="")

        git_mock = MagicMock(side_effect=fake_git)

        def raising_delete(branch):  # noqa: ARG001
            raise RuntimeError("api went away")

        gh.delete_remote_branch = raising_delete

        wt_path = MagicMock()
        wt_path.exists.return_value = True
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupTerminalBranchTest.ISSUE_NUMBER}"

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            # Must NOT raise even though every sub-step failed.
            workflow._cleanup_terminal_branch(
                gh, _TEST_SPEC, self.ISSUE_NUMBER,
            )

    def test_swallows_git_subprocess_exceptions(self) -> None:
        # `_git` can raise (missing `spec.target_root`, missing `git`
        # binary, OSError) rather than returning a non-zero result. The
        # helper must swallow those too so that a worktree-remove or
        # rev-parse raise cannot skip the remote-delete step, which is
        # what the operator actually sees in the repo's branch list.
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        git_mock = MagicMock(side_effect=OSError("git not found"))

        wt_path = MagicMock()
        wt_path.exists.return_value = True
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupTerminalBranchTest.ISSUE_NUMBER}"

        with patch.object(worktree_lifecycle, "_git", git_mock), \
             patch.object(worktree_lifecycle, "_worktree_path", return_value=wt_path):
            # Must NOT raise even though every `_git` invocation throws.
            workflow._cleanup_terminal_branch(
                gh, _TEST_SPEC, self.ISSUE_NUMBER,
            )

        # The remote-delete still ran -- a local-side raise must not
        # block tidying the GitHub side.
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])


class DeleteRemoteBranchTest(unittest.TestCase):
    """`GitHubClient.delete_remote_branch` is idempotent against a 404
    because the repo's "auto-delete head branches" setting may have
    already removed the ref as part of the merge. Other failures log
    and return False so the caller can keep going.
    """

    def _client_with_ref(self, *, raise_status):
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        if raise_status is None:
            client.repo.get_git_ref.return_value = MagicMock()
        else:
            err = GithubException(status=raise_status, data={"message": "x"})
            client.repo.get_git_ref.return_value = MagicMock()
            client.repo.get_git_ref.return_value.delete.side_effect = err
        return client

    def test_success(self) -> None:
        client = self._client_with_ref(raise_status=None)
        self.assertTrue(client.delete_remote_branch("orchestrator/issue-1"))
        client.repo.get_git_ref.assert_called_once_with(
            "heads/orchestrator/issue-1"
        )

    def test_404_treated_as_success(self) -> None:
        client = self._client_with_ref(raise_status=404)
        self.assertTrue(client.delete_remote_branch("orchestrator/issue-1"))

    def test_other_error_returns_false(self) -> None:
        client = self._client_with_ref(raise_status=403)
        self.assertFalse(client.delete_remote_branch("orchestrator/issue-1"))


class RefreshBaseAndWorktreesUnitTest(unittest.TestCase):
    """Unit-level coverage for the per-tick base refresh helper. Real-git
    integration coverage lives in `RefreshBaseAndWorktreesRealGitTest` below.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-unit-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self.target_root = self.tmpdir / "target"
        self.target_root.mkdir()
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=self.target_root,
            base_branch="main",
        )
        self.gh = FakeGitHubClient()

    def test_returns_early_when_base_fetch_fails(self) -> None:
        from unittest.mock import MagicMock
        fetch_fail = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=1, stdout="", stderr="boom",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_fail), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_returns_early_when_repo_worktrees_root_missing(self) -> None:
        from unittest.mock import MagicMock
        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root",
                return_value=self.tmpdir / "missing",
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_iterates_only_issue_dirs(self) -> None:
        from unittest.mock import MagicMock
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        # Two valid issue worktrees, one decompose dir (skipped), one stray
        # file (skipped), one malformed (skipped).
        (wt_root / "issue-7").mkdir()
        (wt_root / "issue-42").mkdir()
        (wt_root / "decompose-7").mkdir()
        (wt_root / "issue-bogus").mkdir()
        (wt_root / "stray.txt").write_text("x")

        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root", return_value=wt_root,
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        called_numbers = sorted(c.args[3] for c in sync.call_args_list)
        self.assertEqual(called_numbers, [7, 42])

    def test_per_worktree_exception_is_swallowed(self) -> None:
        from unittest.mock import MagicMock
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        (wt_root / "issue-1").mkdir()
        (wt_root / "issue-2").mkdir()
        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock(side_effect=[RuntimeError("kaboom"), None])
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root", return_value=wt_root,
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # Both worktrees attempted despite the first raising.
        self.assertEqual(sync.call_count, 2)

    def test_base_fetch_uses_per_spec_authed_helper(self) -> None:
        # The base refresh must go through `_authed_target_fetch` (which
        # resolves the per-spec token and uses the spec's `remote_name`
        # for refs/remotes/<remote_name>/<branch>), NOT plain
        # `_git("fetch", ...)`. Without this, a multi-remote spec where
        # `remote_name != origin` falls back to the ambient git
        # credential helper -- which fails under systemd with
        # `terminal prompts disabled`.
        from unittest.mock import MagicMock

        private_spec = config.RepoSpec(
            slug="acme/widget-private",
            target_root=self.target_root,
            base_branch="cache-main",
            remote_name="private",
        )
        fetch_calls: list[tuple] = []

        def fake_fetch(spec, branch):
            fetch_calls.append((spec, branch))
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )

        # Block any plain-git fetch to assert it never runs.
        plain_git_calls: list[tuple] = []

        def fake_git(*args, cwd):
            plain_git_calls.append(args)
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )

        with patch.object(base_sync, "_authed_target_fetch", side_effect=fake_fetch), \
             patch.object(base_sync, "_git", side_effect=fake_git), \
             patch.object(
                base_sync, "_repo_worktrees_root",
                return_value=self.tmpdir / "missing",
             ):
            workflow._refresh_base_and_worktrees(self.gh, private_spec)

        self.assertEqual(
            fetch_calls, [(private_spec, "cache-main")],
            "base refresh must route through `_authed_target_fetch` with "
            "the spec's base branch",
        )
        # No plain-git fetch was issued -- otherwise the multi-remote
        # token-selection regression resurfaces.
        for args in plain_git_calls:
            self.assertNotEqual(
                args[0] if args else "", "fetch",
                f"plain `_git(\"fetch\", ...)` leaked: {args!r}",
            )


class SyncWorktreeWithBaseUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/refresh-target"),
            base_branch="main",
        )
        self.wt = Path("/tmp/refresh-wt")
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="implementing"))

    def _git_result(self, *, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr="",
        )

    def _add_pr(
        self,
        *,
        pr_number: int = 42,
        head_branch: str = "orchestrator/issue-7",
        merged: bool = False,
        state: str = "open",
    ) -> FakePR:
        pr = FakePR(
            number=pr_number, head_branch=head_branch,
            merged=merged, state=state,
        )
        self.gh.add_pr(pr)
        return pr

    def test_pr_having_in_review_behind_routes_to_resolving_conflict(
        self,
    ) -> None:
        # A local-only base update on a worktree whose branch has already
        # been pushed diverges local HEAD from `pr.head.sha` and breaks
        # `_squash_and_force_push`'s `--force-with-lease=<original_head>`
        # check (the remote is still at the un-merged tip). The fix is to
        # detour the issue to `resolving_conflict` so the existing handler
        # does rebase + push + relabel-to-validating in one consistent flow.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        merge = MagicMock()
        # Behind base by 3 commits.
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Local base update MUST NOT have happened on the PR worktree.
        merge.assert_not_called()
        # Label flipped to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        # PR comment posted.
        self.assertEqual(len(self.gh.posted_pr_comments), 1)
        self.assertEqual(self.gh.posted_pr_comments[0][0], 42)
        self.assertIn("auto-resolution", self.gh.posted_pr_comments[0][1])
        # `conflict_round` initialized to 0 (the cap counter).
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("conflict_round"), 0)

    def test_pr_having_validating_behind_also_routes(self) -> None:
        # Validating is a long-lived label too (the reviewer hasn't approved
        # yet). The detour fires here so the reviewer doesn't run on a
        # stale-base local HEAD.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="validating"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)

    def test_pr_having_documenting_behind_also_routes(self) -> None:
        # `documenting` is the brief final-docs hop between reviewer
        # approval and `in_review`. The handler only checks ahead/behind
        # against the PR branch, not the base, so a sibling-PR merge
        # during the docs pass must be caught by the pre-tick detour --
        # otherwise the docs commit would land on a stale base and only
        # the next in_review tick would auto-rebase it. `hold_base_sync`
        # must remain the only label that gates this auto-rebase.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="documenting"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)

    def test_hold_base_sync_label_skips_pr_refresh_detour(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_hold_base_sync_label_skips_pre_pr_base_rebase(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="implementing")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_backlog_label_skips_pr_refresh_detour(self) -> None:
        # `backlog` is a hard skip: the refresh path must not relabel the
        # issue to `resolving_conflict` or post a PR notice while the
        # operator has the issue postponed.
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_backlog_label_skips_pre_pr_base_rebase(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_pr_having_resolving_conflict_label_does_not_re_route(self) -> None:
        # The handler runs this tick anyway and will do the rebase -- a
        # second label flip is pointless and would re-post the PR notice.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="resolving_conflict"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No new label flip (the issue was already labeled
        # `resolving_conflict` at fixture time, not by us).
        self.assertEqual(self.gh.label_history, [])
        # No duplicate PR notice.
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_having_up_to_date_does_not_route(self) -> None:
        # behind = 0 short-circuits: nothing to refresh, no detour.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_preserves_existing_conflict_round(self) -> None:
        # On re-entry from a previous resolving_conflict round, the cap
        # counter must NOT reset to 0 -- mirrors `_handle_in_review`'s
        # "set when absent" semantics so a perpetually-stuck PR can't
        # ping-pong forever.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7", conflict_round=2,
        )
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="1\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("conflict_round"), 2)

    def test_pr_route_skips_merged_pr(self) -> None:
        # Regression: a just-merged PR advances `origin/<base>`, so the
        # still-in_review worktree pointed at the now-stale branch is
        # naturally behind. Without the PR-state gate the refresh would
        # post an "auto-resolution" notice and relabel the issue to
        # `resolving_conflict` on a PR the next handler call would
        # finalize to `done`. Leaving the label alone lets the existing
        # in_review terminal handler (or the closed-issue sweep variant)
        # do its job.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr(merged=True, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_closed_unmerged_pr(self) -> None:
        # Same regression for the rejected terminal: a closed-without-merge
        # PR that happens to be behind base must not be relabeled to
        # `resolving_conflict`. The handler will finalize to `rejected`.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr(merged=False, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_when_get_pr_fails(self) -> None:
        # Defensive: if PR state cannot be determined this tick, leave the
        # label alone -- the handler can retry from a stable label rather
        # than racing a half-known state.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        # No PR added -- get_pr will raise KeyError on the FakeGitHubClient.
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_does_not_bump_in_review_watermark(self) -> None:
        # Regression: the refresh-time detour runs BEFORE any handler scans
        # comments. Bumping `pr_last_comment_id` past `latest_comment_id`
        # would silently mark unread human "do not merge" / fix-request
        # comments as consumed; the next `_handle_in_review` scan would
        # then skip them and the in_review HITL ready-ping could
        # advertise the PR as ready for human merge over the human
        # signal. The watermark must be left alone here -- the next
        # in_review scan will pick the human comments up correctly, and
        # the orchestrator's own PR notice is filtered via
        # `orchestrator_comment_ids` so it does not replay either.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7",
            pr_last_comment_id=100,
        )
        self._add_pr()
        # An UNREAD human comment landed AFTER the current watermark of 100.
        # If we bump the watermark to `latest_comment_id` (max id seen, which
        # would include this human comment), it gets silently consumed.
        self.gh._issues[7].comments.append(FakeComment(
            id=500, body="do not merge yet", user=FakeUser("human"),
        ))
        git_mock = MagicMock(return_value=self._git_result(stdout="1\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        # Watermark stayed at 100 -- the unread human comment at id=500 is
        # still ahead of it and the next in_review scan will pick it up.
        self.assertEqual(data.get("pr_last_comment_id"), 100)

    def test_pr_route_skips_when_awaiting_human(self) -> None:
        # Regression: a parked PR (`awaiting_human=True`) must not be
        # detoured. `_handle_resolving_conflict`'s awaiting-human branch
        # returns early without rebasing unless a new human comment arrives,
        # so relabeling here would silently hide the existing park behind a
        # `resolving_conflict` label without making any progress -- including
        # the documented `in_review` unmergeable park path. Leaving the
        # park intact preserves its visibility and the human-driven recovery
        # path the park already invited.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7",
            awaiting_human=True, park_reason="unmergeable",
        )
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No relabel: park left intact.
        self.assertEqual(self.gh.label_history, [])
        # No PR notice posted (would have been duplicate noise on a parked
        # issue that already has an HITL ping).
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_skips_dirty_worktree(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=["a.py"],
        ), patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_skips_when_already_up_to_date(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_skips_when_rev_list_fails(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(returncode=128))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_clean_rebase_when_behind(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock(return_value=(True, []))
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_called_once()
        # No abort issued on success.
        self.assertFalse(
            any(c.args[:1] == ("rebase",) for c in hardened.call_args_list)
        )

    def test_conflict_aborts_and_swallows(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock(return_value=(False, ["a.py", "b.py"]))
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Abort issued exactly once.
        abort_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("rebase", "--abort")
        ]
        self.assertEqual(len(abort_calls), 1)

    def test_missing_issue_is_swallowed(self) -> None:
        # An orphan worktree (issue deleted on GitHub side, or fetch error)
        # must not crash the refresh -- skip silently.
        from unittest.mock import MagicMock
        merge = MagicMock()
        with patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 9999)
        merge.assert_not_called()


class TickInvokesBaseRefreshTest(unittest.TestCase):
    """`workflow.tick` must drive `_refresh_base_and_worktrees` before any
    issue is processed -- otherwise an in-flight worktree would still be
    anchored at the base SHA from when it was first added.
    """

    def test_refresh_called_once_before_issues(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        refresh = MagicMock()
        process = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_process_issue", process):
            workflow.tick(gh, _TEST_SPEC)
        refresh.assert_called_once_with(gh, _TEST_SPEC, scheduler=None)
        process.assert_called_once()

    def test_refresh_exception_does_not_block_issue_processing(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        refresh = MagicMock(side_effect=RuntimeError("fetch boom"))
        process = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_process_issue", process):
            workflow.tick(gh, _TEST_SPEC)
        process.assert_called_once()


class TickPerRepoParallelLimitTest(unittest.TestCase):
    """`workflow.tick` must respect `spec.parallel_limit` when fanning per-issue
    work out: a repo configured with `parallel_limit=N` may run up to N
    issues' `_process_issue` calls concurrently, no more, and a single
    failing issue must not stop other eligible issues. The legacy
    `parallel_limit=1` keeps the sequential in-thread behavior so existing
    deployments are unaffected.
    """

    def _spec(self, parallel_limit: int) -> config.RepoSpec:
        return config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
            parallel_limit=parallel_limit,
        )

    def test_limit_one_processes_sequentially_in_caller_thread(self) -> None:
        # parallel_limit=1 must keep the legacy in-thread iteration: no
        # overlap, declared issue order preserved, and the call happens on
        # the same thread `tick` was invoked on (no ThreadPoolExecutor).
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        caller_thread = threading.get_ident()
        in_flight = 0
        max_in_flight = 0
        order: list[int] = []
        worker_threads: set[int] = set()
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                order.append(issue.number)
                worker_threads.add(threading.get_ident())
            time.sleep(0.01)
            with lock:
                in_flight -= 1

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(order, [1, 2, 3])
        self.assertEqual(worker_threads, {caller_thread})

    def test_limit_caps_concurrent_in_flight(self) -> None:
        # With parallel_limit=2 and 4 eligible issues, the executor must
        # admit at most 2 simultaneously. A blocking fake holds each thread
        # until released so we can observe the steady-state concurrency.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3, 4):
            gh.add_issue(make_issue(n, label="implementing"))
        in_flight = 0
        max_in_flight = 0
        # Each enter() ticks the counter and waits up to a bounded timeout
        # so a regression that admitted more than the cap surfaces here
        # rather than deadlocking the suite.
        lock = threading.Lock()
        admitted = threading.Semaphore(0)
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            admitted.release()
            # Hold the thread so up to `limit` workers pile up before any
            # of them exits and frees a slot.
            release.wait(timeout=5.0)
            with lock:
                in_flight -= 1

        def release_when_two_admitted() -> None:
            # Wait until exactly 2 workers are in-flight, hold briefly to
            # let the executor try (and fail) to admit a third, then let
            # all workers drain.
            for _ in range(2):
                self.assertTrue(
                    admitted.acquire(timeout=5.0),
                    "fake_process never admitted 2 workers within timeout",
                )
            time.sleep(0.1)
            release.set()

        releaser = threading.Thread(target=release_when_two_admitted)
        releaser.start()
        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(parallel_limit=2))
        finally:
            release.set()
            releaser.join(timeout=5.0)

        self.assertEqual(max_in_flight, 2)

    def test_limit_allows_full_concurrency_up_to_cap(self) -> None:
        # With parallel_limit=3 and 3 eligible issues, ALL three must be
        # able to run concurrently. A `threading.Barrier(3)` synchronizes
        # the three workers: if only fewer-than-cap were admitted the
        # barrier would block forever and the test would time out. The
        # bounded `wait` makes that failure mode surface as an assertion.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        barrier = threading.Barrier(3)
        passed: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            barrier.wait(timeout=5.0)
            with lock:
                passed.append(issue.number)

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        self.assertEqual(sorted(passed), [1, 2, 3])

    def test_failing_issue_does_not_stop_other_issues(self) -> None:
        # The exception isolation invariant must hold under the parallel
        # path too: one raising issue must not prevent the other eligible
        # issues from completing.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        processed: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            if issue.number == 2:
                raise RuntimeError("simulated issue #2 failure")
            with lock:
                processed.append(issue.number)

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        self.assertEqual(sorted(processed), [1, 3])

    def test_refresh_runs_once_before_parallel_fanout(self) -> None:
        # The pre-tick base refresh must still happen exactly once per
        # tick, before any issue handler runs, even on the parallel path.
        # Otherwise concurrent worktree fanout could race the still-stale
        # base SHA into the per-issue merges.
        import threading
        from unittest.mock import MagicMock

        gh = FakeGitHubClient()
        for n in (1, 2):
            gh.add_issue(make_issue(n, label="implementing"))
        refresh_seen_by_worker: list[int] = []
        refresh = MagicMock()
        lock = threading.Lock()

        def fake_process(_gh, _spec, _issue) -> None:
            with lock:
                refresh_seen_by_worker.append(refresh.call_count)

        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=2))

        refresh.assert_called_once_with(
            gh, self._spec(parallel_limit=2), scheduler=None,
        )
        # Every worker observed refresh.call_count == 1 -- i.e. the refresh
        # completed BEFORE any `_process_issue` started.
        self.assertEqual(refresh_seen_by_worker, [1, 1])

    def test_family_aware_stages_never_overlap_with_each_other(self) -> None:
        # Family-aware labels (decomposing, blocked, umbrella, and unlabeled
        # pickup) write across parent/child boundaries -- the parent's
        # `_handle_decomposing` recovery seeds `parent_number` on each
        # recorded child, while `_handle_blocked` would otherwise park the
        # same child as `blocked_no_children`. Running two of these
        # concurrently raced the writes (the child's late
        # `awaiting_human=True` write clobbered the parent's just-seeded
        # `parent_number`). `tick()` must therefore hold a tick-local
        # lock around the family-aware handlers so no two run at the same
        # time -- AND must let non-family-aware workers run alongside,
        # so a slow decomposing handler does not block unrelated
        # implementing / validating work in the same tick.
        #
        # `ready` is deliberately NOT family-aware (it only writes its own
        # state and recurses into `_handle_implementing`) -- the separate
        # `test_ready_issues_fan_out_concurrently` test pins that
        # contract down.
        import threading
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))
        gh.add_issue(make_issue(4, label="umbrella"))
        # An unlabeled issue routes through `_handle_pickup` -> decomposer
        # and is therefore family-aware too.
        gh.add_issue(make_issue(5, label=None))
        # A non-family-aware label that MUST fan out to a worker thread
        # AND must be allowed to overlap with the family-aware bucket.
        gh.add_issue(make_issue(99, label="implementing"))

        family_in_flight = 0
        family_max_in_flight = 0
        fanout_in_flight = 0
        # `overlap_seen` flips True if a family handler observed a fanout
        # handler in flight (or vice versa) at any moment. With workers
        # sized to fit every submission and a short sleep on each
        # handler, the family lock's `holding` handler is virtually
        # guaranteed to overlap with the (independently scheduled)
        # fanout worker. If `tick()` regressed to "drain family
        # synchronously before fanout starts" this would stay False and
        # the assertion fails.
        overlap_seen = False
        family_count = 0
        fanout_count = 0
        counter_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal family_in_flight, family_max_in_flight
            nonlocal fanout_in_flight, overlap_seen
            nonlocal family_count, fanout_count
            family = issue.number != 99
            if family:
                with counter_lock:
                    family_in_flight += 1
                    family_max_in_flight = max(
                        family_max_in_flight, family_in_flight,
                    )
                    family_count += 1
                    if fanout_in_flight > 0:
                        overlap_seen = True
                time.sleep(0.05)
                with counter_lock:
                    family_in_flight -= 1
            else:
                with counter_lock:
                    fanout_in_flight += 1
                    fanout_count += 1
                    if family_in_flight > 0:
                        overlap_seen = True
                time.sleep(0.05)
                with counter_lock:
                    fanout_in_flight -= 1

        # parallel_limit=5 and no `global_semaphore` means every submission
        # gets its own worker thread; the family lock is the ONLY thing
        # preventing family-aware handlers from overlapping with each
        # other, and the fanout worker is free to run alongside whichever
        # family handler currently holds the lock.
        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=5))

        # Four family-aware issues observed; the family lock kept them
        # from overlapping with each other.
        self.assertEqual(family_count, 4)
        self.assertEqual(family_max_in_flight, 1)
        self.assertEqual(fanout_count, 1)
        # Fanout handler ran concurrently with at least one family
        # handler. Without the overlap fix (family draining before
        # fanout starts), `overlap_seen` would stay False.
        self.assertTrue(
            overlap_seen,
            "family bucket and fanout bucket did not overlap -- regression "
            "to draining family synchronously before the executor starts?",
        )

    def test_ready_issues_fan_out_concurrently(self) -> None:
        # `ready` is NOT family-aware -- `_handle_ready` only writes its
        # own pinned state, then recurses into `_handle_implementing`
        # which runs the long-running dev-agent work. Putting `ready` in
        # the family bucket would force every ready->implementing job to
        # run sequentially on the caller thread, defeating the
        # `parallel_limit > 1` concurrency goal of issue #115. This test
        # pins that contract: with three `ready` issues and
        # `parallel_limit=3`, all three must be able to enter
        # `_process_issue` concurrently.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="ready"))

        caller_thread = threading.get_ident()
        barrier = threading.Barrier(3, timeout=5.0)
        passed: list[tuple[int, int]] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            # If the partition wrongly put `ready` in the family bucket,
            # only one of these would ever run at a time and the barrier
            # would time out (TimeoutError surfaces as test failure).
            barrier.wait()
            with lock:
                passed.append((issue.number, threading.get_ident()))

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        self.assertEqual(sorted(n for n, _ in passed), [1, 2, 3])
        # All three ran on worker threads, not the caller thread.
        for _n, tid in passed:
            self.assertNotEqual(
                tid, caller_thread,
                "ready issues must fan out to worker threads, not the caller",
            )

    def test_label_read_failure_does_not_abort_other_issues(self) -> None:
        # Per-issue exception isolation must extend to the partition's
        # label read. The reviewer's reproducer: if `gh.workflow_label`
        # raises on one issue while classifying for parallel fanout, the
        # partition loop aborts and EVERY other eligible issue this tick
        # goes unprocessed -- a regression of the existing per-issue
        # isolation invariant. The fix catches the read, logs it, and
        # routes the offending issue into the family bucket where the
        # per-issue try/except picks up any sustained failure.
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        original_workflow_label = FakeGitHubClient.workflow_label
        # Raise for issue #2 only; #1 and #3 return their real labels.
        def flaky_workflow_label(self, issue):
            if getattr(issue, "number", None) == 2:
                raise RuntimeError("simulated label-read failure")
            return original_workflow_label(issue)

        processed: list[int] = []
        # Issue #2 still ends up in `_process_issue` via the family
        # bucket (the partition routes label-read failures there) so the
        # fake_process gets called for it too -- but ALSO for #1 and #3,
        # proving the other issues weren't aborted.
        def fake_process(_gh, _spec, issue) -> None:
            processed.append(issue.number)

        with patch.object(FakeGitHubClient, "workflow_label", flaky_workflow_label), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        # All three issues were attempted -- the partition did not abort
        # after the bad label read on #2.
        self.assertEqual(sorted(processed), [1, 2, 3])

    def test_family_bucket_occupies_one_slot_under_tight_limit(self) -> None:
        # Reviewer's exact reproducer: with `parallel_limit=2`, two
        # family-aware issues, and one fanout issue, an earlier draft
        # that submitted per-family-issue futures plus a shared lock
        # let the slow family handler hold one worker slot while the
        # second family future occupied the OTHER worker slot blocking
        # on the lock -- the fanout issue stayed queued until the slow
        # family handler exited. The drain-task design folds the whole
        # family bucket into one future so it consumes exactly one
        # executor slot regardless of how many family-aware issues are
        # pending, leaving the other limit-1 slots free for fanout.
        #
        # The test holds the FIRST family handler inside `_process_issue`
        # until the fanout handler completes; without the drain-task fix
        # the fanout handler would be queued and never run, the wait
        # below would time out, and the assertion would fire.
        import threading
        gh = FakeGitHubClient()
        # Two family-aware issues. The first is slow; the second
        # must wait for the first because the family bucket runs them
        # sequentially in one drain task.
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))
        # One fanout issue that MUST advance while the slow family
        # handler is still inside `_process_issue`.
        gh.add_issue(make_issue(99, label="implementing"))

        slow_family_holding = threading.Event()
        slow_family_release = threading.Event()
        fanout_done = threading.Event()
        observed_order: list[int] = []
        observed_lock = threading.Lock()
        # Errors raised in the releaser sub-thread are captured and
        # re-raised from the test thread; otherwise an AssertionError
        # inside `releaser` would only print and the test would
        # spuriously report success.
        releaser_error: list[BaseException] = []

        def fake_process(_gh, _spec, issue) -> None:
            with observed_lock:
                observed_order.append(issue.number)
            if issue.number == 1:
                slow_family_holding.set()
                # Hold until fanout completes. If the drain-task fix
                # regressed and the family bucket occupied >1 worker
                # slots, fanout would queue and `slow_family_release`
                # would only be set by the test's finally below (after
                # the join times out) -- the wait below would NOT
                # surface that directly; the releaser's assertions are
                # what actually fail.
                slow_family_release.wait(timeout=5.0)
            elif issue.number == 99:
                fanout_done.set()
            # Family issue #2 runs to completion immediately (no hold);
            # it should only run AFTER family #1 exits.

        def releaser() -> None:
            try:
                self.assertTrue(
                    slow_family_holding.wait(timeout=5.0),
                    "slow family handler never entered _process_issue",
                )
                # Crucially: fanout must complete WHILE the family
                # bucket is still mid-flight on issue #1. If the
                # family bucket occupied both worker slots, fanout
                # would be queued and `fanout_done` would never get
                # set in this window.
                self.assertTrue(
                    fanout_done.wait(timeout=5.0),
                    "fanout did not run concurrently with the slow "
                    "family handler; family bucket likely consumed "
                    "multiple slots",
                )
            except BaseException as e:  # noqa: BLE001 -- re-raised below
                releaser_error.append(e)
            finally:
                # Always release so the test thread can join cleanly
                # even when the releaser's assertions fire.
                slow_family_release.set()

        t = threading.Thread(target=releaser)
        t.start()
        try:
            # parallel_limit=2 + 3 submissions total. Family bucket =
            # one drain task = one slot. Fanout = one task = one slot.
            # The second family issue stays inside the drain task (not
            # a separate executor slot), so the fanout's slot is free
            # while issue #1 is held.
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(parallel_limit=2))
        finally:
            slow_family_release.set()
            t.join(timeout=5.0)

        if releaser_error:
            raise releaser_error[0]

        # All three issues handled.
        self.assertEqual(sorted(observed_order), [1, 2, 99])
        # Family #2 ran AFTER family #1 (drain task is sequential).
        idx_1 = observed_order.index(1)
        idx_2 = observed_order.index(2)
        self.assertLess(idx_1, idx_2, observed_order)
        # And the fanout entered `_process_issue` BEFORE family #1
        # exited (the releaser only released after `fanout_done` was
        # set, which the fanout handler sets on entry).
        idx_99 = observed_order.index(99)
        self.assertLess(idx_99, idx_2, observed_order)

    def test_slow_family_handler_does_not_block_fanout_workers(self) -> None:
        # Reviewer's reproducer: a single long decomposing / unlabeled-
        # pickup agent run must NOT block the other workers in the same
        # tick. With the family lock holding the family bucket on one
        # worker, the other (limit-1) workers must still be able to
        # advance unrelated implementing / validating issues -- otherwise
        # a mixed-stage tick collapses back to serial in practice.
        import threading
        gh = FakeGitHubClient()
        # One slow family-aware issue. The handler holds inside
        # `_process_issue` until released by the test; without the
        # overlap fix this would freeze every other worker.
        gh.add_issue(make_issue(1, label="decomposing"))
        # Several fanout issues that MUST advance while the family
        # handler is still running.
        for n in (10, 11, 12):
            gh.add_issue(make_issue(n, label="implementing"))

        family_holding = threading.Event()
        family_release = threading.Event()
        fanout_done: list[int] = []
        fanout_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            if issue.number == 1:
                family_holding.set()
                # Hold the family lock until the test confirms fanout
                # workers all finished. Time-bounded so a regression
                # surfaces as an assertion rather than a hang.
                self.assertTrue(
                    family_release.wait(timeout=5.0),
                    "family handler released by timeout, not by test",
                )
                return
            # Fanout handler.
            with fanout_lock:
                fanout_done.append(issue.number)

        def releaser() -> None:
            self.assertTrue(
                family_holding.wait(timeout=5.0),
                "family handler never entered _process_issue",
            )
            # Wait until every fanout handler completed BEFORE letting
            # the family handler exit. If fanout was blocked by the
            # family lock, this loop would time out.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with fanout_lock:
                    if len(fanout_done) == 3:
                        break
                time.sleep(0.01)
            family_release.set()

        t = threading.Thread(target=releaser)
        t.start()
        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(parallel_limit=4))
        finally:
            family_release.set()
            t.join(timeout=5.0)

        # All three fanout issues completed while the family handler
        # was still inside `_process_issue` -- exactly the property the
        # reviewer asked for. Without the overlap fix, this list would
        # be empty (or only one entry, the lucky fanout that grabbed
        # the caller thread).
        self.assertEqual(sorted(fanout_done), [10, 11, 12])

    def test_concurrent_decomposing_and_blocked_do_not_race_child_state(
        self,
    ) -> None:
        # Regression for the reproducer the reviewer flagged: a parent
        # `decomposing` recovery seeded `parent_number` on a child while a
        # concurrent `blocked` tick on the same child cleared it and
        # wrote `awaiting_human=True` + `park_reason=blocked_no_children`.
        # With the tick-local family lock in place, the two family-aware
        # handlers cannot overlap regardless of which worker picks each
        # one up -- whichever runs first, the parent's repair is the
        # final word and the child's pinned state retains `parent_number`
        # without the stale park flags.
        gh = FakeGitHubClient()
        # Parent #10 carries the half-finished-decomposition recovery
        # markers (`expected_children_count=1`, `children=[20]`) so its
        # `_handle_decomposing` enters the repair branch and seeds the
        # child's state. Child #20 is labeled `blocked` with empty pinned
        # state, so its `_handle_blocked` would normally park
        # `blocked_no_children` and clobber the parent's seed.
        gh.add_issue(make_issue(10, label="decomposing"))
        gh.add_issue(make_issue(20, label="blocked"))
        gh.seed_state(
            10,
            expected_children_count=1,
            children=[20],
            umbrella=None,
        )

        # Bare-bones substitute for `_process_issue` that exercises just
        # the cross-issue write path the bug lives in. The real handlers
        # call into worktree / agent code that needs more scaffolding;
        # this distilled version reproduces the data-race scenario
        # exactly: parent reads child state, sets fields, writes back;
        # child reads its own state, parks on missing parent_number.

        def fake_process(client, _spec, issue) -> None:
            if issue.number == 10:
                # Parent's repair branch: read each recorded child,
                # set parent_number, clear park flags, write back.
                state = client.read_pinned_state(issue)
                for child_n in state.get("children") or []:
                    child = client.get_issue(int(child_n))
                    cs = client.read_pinned_state(child)
                    if not cs.get("parent_number"):
                        cs.set("parent_number", issue.number)
                        cs.set("awaiting_human", False)
                        cs.set("park_reason", None)
                        client.write_pinned_state(child, cs)
                client.set_workflow_label(issue, "blocked")
                client.write_pinned_state(issue, state)
                return
            if issue.number == 20:
                cs = client.read_pinned_state(issue)
                if cs.get("parent_number"):
                    return
                if cs.get("awaiting_human"):
                    return
                cs.set("awaiting_human", True)
                cs.set("park_reason", "blocked_no_children")
                client.write_pinned_state(issue, cs)
                return

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=4))

        # Child's final state retains the parent's seed and is not parked.
        # The family lock guarantees the two handlers ran sequentially
        # in some order; either order produces this final state because
        # the parent's repair either runs first (child sees parent_number
        # set and returns early) or last (parent's write is final).
        child_state = gh.pinned_data(20)
        self.assertEqual(child_state.get("parent_number"), 10)
        self.assertFalse(child_state.get("awaiting_human"))
        self.assertIsNone(child_state.get("park_reason"))

    def test_no_eligible_issues_is_a_noop(self) -> None:
        # An empty pollable list must not spin up worker threads or raise.
        gh = FakeGitHubClient()
        from unittest.mock import MagicMock
        process = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", process):
            workflow.tick(gh, self._spec(parallel_limit=4))
        process.assert_not_called()

    def test_global_semaphore_clamps_concurrent_in_flight(self) -> None:
        # The `global_semaphore` parameter is the host-wide ceiling threaded
        # in by `main._run_tick`. It must clamp concurrent `_process_issue`
        # calls regardless of how high `spec.parallel_limit` was
        # configured: a spec with parallel_limit=4 plus a semaphore sized
        # 2 must never have more than 2 issues in flight at once, even
        # though the per-repo executor admits 4 worker threads.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3, 4):
            gh.add_issue(make_issue(n, label="implementing"))
        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()
        admitted = threading.Semaphore(0)
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            admitted.release()
            release.wait(timeout=5.0)
            with lock:
                in_flight -= 1

        def release_when_two_admitted() -> None:
            for _ in range(2):
                self.assertTrue(
                    admitted.acquire(timeout=5.0),
                    "fake_process never admitted 2 workers within timeout",
                )
            time.sleep(0.1)
            release.set()

        releaser = threading.Thread(target=release_when_two_admitted)
        releaser.start()
        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(
                    gh,
                    self._spec(parallel_limit=4),
                    global_semaphore=threading.BoundedSemaphore(2),
                )
        finally:
            release.set()
            releaser.join(timeout=5.0)

        # Even though parallel_limit=4 would otherwise let 4 issues run in
        # parallel, the semaphore cap of 2 must hold.
        self.assertEqual(max_in_flight, 2)

    def test_global_semaphore_size_one_serializes_processing(self) -> None:
        # With a size-1 semaphore the `_process_issue` calls must run one
        # at a time regardless of `parallel_limit`. This is the workflow-
        # level guarantee that backs `MAX_PARALLEL_ISSUES_GLOBAL=1`: even
        # with multiple worker threads spun up, only one is ever inside
        # `_process_issue`.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.02)
            with lock:
                in_flight -= 1

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(
                gh,
                self._spec(parallel_limit=5),
                global_semaphore=threading.BoundedSemaphore(1),
            )

        self.assertEqual(max_in_flight, 1)

    def test_parallel_path_uses_per_worker_clients_and_refetches_issues(
        self,
    ) -> None:
        # PyGithub's `Requester` is not documented thread-safe; sharing a
        # single client across worker threads can interleave concurrent
        # request setup. The parallel path must therefore (a) call
        # `gh._for_worker_thread()` once per submitted issue so each
        # worker gets its own client, and (b) refetch the Issue via the
        # WORKER'S client so the Issue's parent requester chain matches
        # the thread that actually drives it.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        # Each `_for_worker_thread()` call mints a distinct client object,
        # so a workflow regression that reused the parent client across
        # threads would fail the `is`-identity check below.
        cloned_clients: list[FakeGitHubClient] = []
        clone_lock = threading.Lock()

        def fake_clone() -> FakeGitHubClient:
            twin = FakeGitHubClient()
            # Mirror the parent's issues so `get_issue` on the worker
            # client resolves against the same issue numbers the test
            # seeded.
            for n in (1, 2, 3):
                twin.add_issue(make_issue(n, label="implementing"))
            with clone_lock:
                cloned_clients.append(twin)
            return twin

        seen: list[tuple[int, int]] = []  # (issue_number, id(worker_gh))
        get_issue_calls: list[tuple[int, int]] = []
        seen_lock = threading.Lock()

        original_get_issue = FakeGitHubClient.get_issue

        def tracking_get_issue(self, number):
            with seen_lock:
                get_issue_calls.append((number, id(self)))
            return original_get_issue(self, number)

        def fake_process(worker_gh, _spec, issue) -> None:
            with seen_lock:
                seen.append((issue.number, id(worker_gh)))

        with patch.object(gh, "_for_worker_thread", fake_clone), \
             patch.object(FakeGitHubClient, "get_issue", tracking_get_issue), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        # Every submitted issue produced exactly one worker-client clone.
        self.assertEqual(len(cloned_clients), 3)
        # Every worker client is a fresh object (no two share identity).
        self.assertEqual(len({id(c) for c in cloned_clients}), 3)
        # The parent client is NOT one of the worker clients: tick must
        # not hand the shared parent to any worker.
        self.assertNotIn(id(gh), {id(c) for c in cloned_clients})
        # Each worker called `get_issue` on its OWN client (not the parent),
        # so the refetch resolves against that client's Requester.
        parent_id = id(gh)
        for _number, client_id in get_issue_calls:
            self.assertNotEqual(client_id, parent_id)
        # And each `_process_issue` invocation saw an issue paired with the
        # same worker client that fetched it (no cross-thread Issue handoff).
        for issue_number, process_client_id in seen:
            fetch_clients = [
                cid for n, cid in get_issue_calls if n == issue_number
            ]
            self.assertIn(process_client_id, fetch_clients)
        self.assertEqual(sorted(n for n, _ in seen), [1, 2, 3])

    def test_limit_one_does_not_clone_per_issue(self) -> None:
        # Sequential mode runs on the caller thread; the PyGithub thread
        # safety rationale does not apply, so the legacy path must not
        # call `_for_worker_thread()` (avoids an unnecessary token + repo
        # round-trip for every issue on every tick).
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        clone = MagicMock(side_effect=lambda: self.fail(
            "_for_worker_thread must not be called on the sequential path"
        ))
        with patch.object(gh, "_for_worker_thread", clone), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue"):
            workflow.tick(gh, self._spec(parallel_limit=1))
        clone.assert_not_called()

    def test_limit_one_streams_and_processes_pre_failure_issues(self) -> None:
        # Legacy invariant: with parallel_limit=1, the loop iterates the
        # generator directly so any issue yielded BEFORE an enumeration
        # failure (PyGithub pagination error, closed-issue sweep raise) is
        # still processed. Materializing the iterator upfront would lose
        # those already-yielded issues. Generator-style fake raises
        # mid-iteration to pin the streaming contract down.
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        def flaky_list_pollable_issues():
            yield gh.get_issue(1)
            yield gh.get_issue(2)
            raise RuntimeError("simulated pagination failure")

        processed: list[int] = []

        def fake_process(_gh, _spec, issue) -> None:
            processed.append(issue.number)

        with patch.object(gh, "list_pollable_issues", flaky_list_pollable_issues), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            # The enumeration failure is not caught inside `tick` (it lives
            # at the per-repo boundary in `main._run_tick`), but the issues
            # yielded BEFORE the raise must still have been processed.
            with self.assertRaises(RuntimeError):
                workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(processed, [1, 2])


class TickViaSchedulerTest(unittest.TestCase):
    """`workflow.tick` accepts an optional `IssueScheduler` that takes
    over per-issue dispatch entirely: each polling pass enumerates the
    pollable issues, classifies family-aware vs fan-out work, and
    submits a per-issue callable to the scheduler. The submit path is
    nonblocking -- a duplicate active issue, a per-repo or global cap
    hit, or a family slot already held is simply skipped this tick and
    a later polling pass retries against the live scheduler state.

    Tests use a real `IssueScheduler` (not a mock) so the in-flight
    state across multiple polling passes is the same state the
    production scheduler would expose, and they gate workers with
    `threading.Event` so concurrency is observable without sleep-and-
    pray timing.
    """

    def _spec(self, parallel_limit: int = 5) -> config.RepoSpec:
        return config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
            parallel_limit=parallel_limit,
        )

    def _wait_idle(self, sched, repo_slug: str, deadline_s: float = 5.0) -> None:
        """Block until the scheduler reports zero active workers for `repo_slug`.

        The done-callback releases the in-flight markers from a background
        thread, so a brief poll prevents the next tick from observing the
        old marker. Time-bounded so a regression fails the test instead of
        hanging the suite.
        """
        import threading as _threading
        deadline = _threading.Event()
        timer = _threading.Timer(deadline_s, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while sched.active_count(repo_slug) > 0 and not deadline.is_set():
                pass
        finally:
            timer.cancel()
        self.assertEqual(
            sched.active_count(repo_slug), 0,
            f"scheduler still has active workers on {repo_slug}",
        )

    def test_active_issue_is_skipped_until_completion(self) -> None:
        # Tick 1 accepts the issue and the worker holds inside
        # `_process_issue`. Tick 2 must NOT submit the same issue
        # again while it is still in flight -- the scheduler's
        # duplicate-active-issue gate keeps a second submit out so the
        # handler isn't entered twice concurrently. After the worker
        # exits, a third tick may submit it again.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label="implementing"))

        start = threading.Event()
        release = threading.Event()
        call_count = 0
        count_lock = threading.Lock()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal call_count
            with count_lock:
                call_count += 1
            start.set()
            release.wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: accept and dispatch.
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    start.wait(timeout=2.0),
                    "worker never entered _process_issue after first tick",
                )
                self.assertTrue(sched.is_active("acme/widget", 7))

                # Tick 2: while the worker is still in flight, the
                # duplicate-active gate must reject the resubmit. The
                # handler must NOT be called a second time.
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Brief breathing room: any in-flight executor task
                # would have invoked the fake by now.
                time.sleep(0.1)
                with count_lock:
                    self.assertEqual(call_count, 1)
                self.assertTrue(sched.is_active("acme/widget", 7))

                # Release the worker and let it complete.
                release.set()
            self._wait_idle(sched, "acme/widget")

            # Tick 3: completion cleared the marker so the same issue
            # is accepted again.
            second_start = threading.Event()

            def fake_process_2(_gh, _spec, _issue) -> None:
                nonlocal call_count
                with count_lock:
                    call_count += 1
                second_start.set()

            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process_2):
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    second_start.wait(timeout=2.0),
                    "worker never re-entered _process_issue after first completed",
                )
        finally:
            release.set()

        with count_lock:
            self.assertEqual(call_count, 2)

    def test_same_repo_fanout_proceeds_when_limits_allow(self) -> None:
        # Three non-family issues on the same repo with the scheduler's
        # per-repo cap set wide enough to admit all three. The dispatch
        # loop must submit each one and the scheduler must let all three
        # workers run concurrently -- the per-repo cap is the only gate
        # that could keep them apart.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=3)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        barrier = threading.Barrier(3, timeout=5.0)
        passed: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            # If any submission was rejected, fewer than three workers
            # would arrive at the barrier and `wait` would raise
            # BrokenBarrierError on timeout -- the test then fails on
            # the unrejected workers' unhandled exception.
            barrier.wait()
            with lock:
                passed.append(issue.number)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Wait for all three to pass through the barrier and
                # record their issue numbers. The barrier guarantees
                # they're all in `_process_issue` at the same time;
                # this loop just waits for the final list write.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with lock:
                        if len(passed) == 3:
                            break
                    time.sleep(0.01)
            self._wait_idle(sched, "acme/widget")
        finally:
            # Defensive: a barrier broken by an early failure could
            # leave a worker pinned; releasing the underlying scheduler
            # is enough because `addCleanup(sched.shutdown)` waits for
            # workers to exit.
            pass

        self.assertEqual(sorted(passed), [1, 2, 3])

    def test_per_repo_cap_skips_overflow_until_a_slot_frees(self) -> None:
        # With `parallel_limit=2` and three eligible non-family issues,
        # the first two are accepted and the third is skipped this
        # tick. After one of the in-flight workers exits, a follow-up
        # tick admits the previously-skipped issue.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        for n in (10, 11, 12):
            gh.add_issue(make_issue(n, label="implementing"))

        starts: dict[int, threading.Event] = {
            10: threading.Event(),
            11: threading.Event(),
            12: threading.Event(),
        }
        releases: dict[int, threading.Event] = {
            10: threading.Event(),
            11: threading.Event(),
            12: threading.Event(),
        }
        seen: list[int] = []
        seen_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            starts[issue.number].set()
            with seen_lock:
                seen.append(issue.number)
            releases[issue.number].wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: parallel_limit=2 caps to two accepted submits.
                workflow.tick(
                    gh, self._spec(parallel_limit=2), scheduler=sched,
                )
                # Two workers must enter; the third must NOT (per-repo
                # cap holds it back).
                accepted = [
                    n for n, ev in starts.items() if ev.wait(timeout=2.0)
                ]
                self.assertEqual(len(accepted), 2, accepted)
                time.sleep(0.1)
                rejected_numbers = [n for n in (10, 11, 12) if n not in accepted]
                self.assertEqual(len(rejected_numbers), 1)
                rejected_number = rejected_numbers[0]
                self.assertFalse(
                    starts[rejected_number].is_set(),
                    f"#{rejected_number} should have been skipped by per-repo cap",
                )

                # Release one of the two accepted workers and wait for
                # the scheduler to reflect the freed slot.
                drained = accepted[0]
                releases[drained].set()
                deadline = threading.Event()
                timer = threading.Timer(2.0, deadline.set)
                timer.daemon = True
                timer.start()
                try:
                    while (
                        sched.is_active("acme/widget", drained)
                        and not deadline.is_set()
                    ):
                        pass
                finally:
                    timer.cancel()
                self.assertFalse(sched.is_active("acme/widget", drained))

                # The handler stub does not flip labels, so close the
                # FakeIssue directly to model "advanced past this
                # stage" -- in production the drained worker would
                # have relabeled / closed the issue and the next
                # enumeration would skip it. Without this, the next
                # tick would re-admit the drained issue and take the
                # newly freed slot back, starving the previously-
                # skipped one.
                gh._issues[drained].closed = True

                # Tick 2: previously-skipped issue is now admitted.
                workflow.tick(
                    gh, self._spec(parallel_limit=2), scheduler=sched,
                )
                self.assertTrue(
                    starts[rejected_number].wait(timeout=2.0),
                    f"#{rejected_number} not admitted after a slot freed up",
                )
        finally:
            for ev in releases.values():
                ev.set()

        # All three issues eventually ran exactly once between the two ticks.
        self.assertEqual(sorted(seen), [10, 11, 12])

    def test_family_aware_drains_sequentially_within_one_bucket(self) -> None:
        # All family-aware issues this tick are folded into ONE bucket
        # task that drains them sequentially. The bucket holds the family
        # slot for the whole drain so a concurrent tick mid-drain cannot
        # squeeze a second family worker past the gate, and at no point
        # do two family-aware handlers run concurrently. Crucially, the
        # drain advances to the next family issue within the SAME tick's
        # bucket task -- no extra polling pass needed -- which is the
        # issue #326 fix: a backlog/blocked child can no longer take the
        # family slot and starve the parent umbrella issue.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))

        family_in_flight = 0
        family_max_in_flight = 0
        family_starts: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        family_releases: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        order: list[int] = []
        counter_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal family_in_flight, family_max_in_flight
            with counter_lock:
                family_in_flight += 1
                family_max_in_flight = max(
                    family_max_in_flight, family_in_flight,
                )
                order.append(issue.number)
            family_starts[issue.number].set()
            family_releases[issue.number].wait(timeout=5.0)
            with counter_lock:
                family_in_flight -= 1

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: one bucket task submitted, drain enters its
                # first family-aware issue.
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    family_starts[1].wait(timeout=2.0),
                    "drain did not enter the first family-aware issue",
                )
                time.sleep(0.1)
                self.assertFalse(
                    family_starts[2].is_set(),
                    "drain entered second family-aware issue before "
                    "releasing the first -- bucket must process "
                    "sequentially",
                )
                with counter_lock:
                    self.assertEqual(family_in_flight, 1)

                # Tick 2 BEFORE releasing the first handler: the family
                # slot is still held by the bucket task, so a second
                # bucket submit must be skipped. This is the "do not
                # overlap across polling passes" property: a polling
                # pass that observes a family worker mid-flight cannot
                # squeeze a second one past the gate.
                workflow.tick(gh, self._spec(), scheduler=sched)
                time.sleep(0.1)
                self.assertFalse(
                    family_starts[2].is_set(),
                    "family-slot leak: second family worker started "
                    "while the first was still in flight",
                )
                with counter_lock:
                    self.assertEqual(family_in_flight, 1)

                # Release #1. The SAME bucket task advances to #2
                # without needing another tick -- that's the bug-fix
                # contract: a no-op family child cannot block the next
                # family issue (e.g. the parent umbrella) from running.
                family_releases[1].set()
                self.assertTrue(
                    family_starts[2].wait(timeout=2.0),
                    "drain did not advance to second family issue "
                    "after first one was released",
                )
                family_releases[2].set()
            self._wait_idle(sched, "acme/widget")
        finally:
            for ev in family_releases.values():
                ev.set()

        # At no point did two family-aware handlers run concurrently.
        self.assertEqual(family_max_in_flight, 1)
        # Both issues ran exactly once -- and within ticks 1's bucket.
        self.assertEqual(sorted(order), [1, 2])

    def test_family_bucket_skip_is_logged(self) -> None:
        # The dispatch layer logs a "family bucket (...) not submitted
        # this tick" line when the previous tick's bucket is still
        # draining, so an operator can correlate "umbrella not
        # advancing" with the slot still being held. The underlying
        # scheduler also logs the per-submit `reason=family_slot_held`
        # skip; this test asserts the higher-level dispatch context
        # makes it into the log too.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))

        start = threading.Event()
        release = threading.Event()
        entered: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            with lock:
                entered.append(issue.number)
            start.set()
            release.wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(start.wait(timeout=2.0))

                # The drain is parked on the first family issue; a
                # follow-up tick must observe the bucket skip and log
                # it with the count of pending family issues.
                with self.assertLogs(
                    "orchestrator.workflow", level=logging.INFO,
                ) as cm:
                    workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    any(
                        "family bucket" in msg and "not submitted" in msg
                        for msg in cm.output
                    ),
                    cm.output,
                )
        finally:
            release.set()
        self._wait_idle(sched, "acme/widget")

    def test_family_drain_marks_in_progress_issue_as_active(self) -> None:
        # The bucket task wraps each per-issue iteration in
        # `scheduler.track_active` so `is_active(repo, n)` reports True
        # for the issue currently being processed inside the bucket.
        # Without this, the pre-tick base refresh would not skip the
        # in-flight family issue's worktree and could race the agent.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(42, label="decomposing"))

        start = threading.Event()
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            start.set()
            release.wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(start.wait(timeout=2.0))
                # The bucket's sentinel key (issue 0) IS active and the
                # currently-processed family issue #42 is ALSO marked
                # active so the refresh-skip contract holds.
                self.assertTrue(sched.is_active("acme/widget", 42))
        finally:
            release.set()
        self._wait_idle(sched, "acme/widget")
        # After completion, #42's per-iteration claim is released.
        self.assertFalse(sched.is_active("acme/widget", 42))

    def test_family_drain_skips_issue_already_in_flight(self) -> None:
        # Cross-tick race: tick N classifies #50 as fanout (e.g.
        # `implementing`) and submits it. Before that worker finishes,
        # something relabels #50 into a family-aware state and tick N+1
        # folds it into the family bucket. The bucket's drain reaches
        # #50, sees `track_active` cannot claim (fanout worker still
        # holds the active marker), and must SKIP `_process_issue` for
        # that iteration -- two workers running the same handler
        # concurrently would race the worktree and pinned state.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(50, label="implementing"))

        # Simulate the fanout worker holding (acme/widget, 50) via a
        # direct scheduler.submit that parks until released.
        fanout_start = threading.Event()
        fanout_release = threading.Event()

        def _fanout_worker() -> None:
            fanout_start.set()
            fanout_release.wait(timeout=5.0)

        process_calls: list[int] = []
        process_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            with process_lock:
                process_calls.append(issue.number)

        try:
            # Plant the fanout worker on #50.
            self.assertTrue(
                sched.submit("acme/widget", 50, _fanout_worker),
            )
            self.assertTrue(fanout_start.wait(timeout=2.0))

            # Relabel #50 to a family-aware state so the next tick
            # folds it into the family bucket.
            gh._issues[50].labels = [FakeLabel("blocked")]

            with self.assertLogs(
                "orchestrator.workflow", level=logging.INFO,
            ) as cm, \
                 patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Wait for the bucket drain to attempt #50 and skip it.
                deadline = time.monotonic() + 2.0
                skipped = False
                while time.monotonic() < deadline and not skipped:
                    skipped = any(
                        "already in flight" in m and "#50" in m
                        for m in cm.output
                    )
                    time.sleep(0.01)
                self.assertTrue(skipped, cm.output)
            # The fanout worker is the ONLY one that processed #50;
            # the drain refused to enter a second concurrent handler.
            with process_lock:
                self.assertNotIn(50, process_calls)
        finally:
            fanout_release.set()
        self._wait_idle(sched, "acme/widget")

    def test_unlabeled_pickup_is_treated_as_family_aware(self) -> None:
        # An unlabeled issue routes through `_handle_pickup`, which can
        # create children and seed their pinned state -- a cross-issue
        # write, same as decomposing/blocked/umbrella. Dispatch must
        # therefore fold it into the family bucket alongside the
        # explicit family labels and process it sequentially under the
        # one family slot, never as a fanout submit.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label=None))

        family_in_flight = 0
        family_max_in_flight = 0
        starts: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        releases: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        order: list[int] = []
        counter_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal family_in_flight, family_max_in_flight
            with counter_lock:
                family_in_flight += 1
                family_max_in_flight = max(
                    family_max_in_flight, family_in_flight,
                )
                order.append(issue.number)
            starts[issue.number].set()
            releases[issue.number].wait(timeout=5.0)
            with counter_lock:
                family_in_flight -= 1

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Drain enters its first family-aware issue (could be
                # either depending on enumeration order). The other
                # must NOT be entered until the first is released --
                # the bucket drains sequentially.
                started_first = None
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and started_first is None:
                    for n, ev in starts.items():
                        if ev.is_set():
                            started_first = n
                            break
                    time.sleep(0.01)
                self.assertIsNotNone(started_first)
                time.sleep(0.1)
                second = 2 if started_first == 1 else 1
                self.assertFalse(
                    starts[second].is_set(),
                    "second family-aware issue must wait for the first "
                    "to release inside the drain",
                )

                # Release the first; the SAME bucket task advances to
                # the second family-aware issue.
                releases[started_first].set()
                self.assertTrue(
                    starts[second].wait(timeout=2.0),
                    "unlabeled-pickup issue did not run inside the "
                    "family bucket after the first family issue released",
                )
                releases[second].set()
        finally:
            for ev in releases.values():
                ev.set()
        self._wait_idle(sched, "acme/widget")

        # Both ran exactly once, sequentially, in the same bucket.
        self.assertEqual(family_max_in_flight, 1)
        self.assertEqual(sorted(order), [1, 2])

    def test_legacy_path_used_when_scheduler_is_none(self) -> None:
        # `scheduler=None` must keep the existing synchronous in-thread
        # behavior intact. The legacy path runs `_process_issue` on the
        # caller thread for `parallel_limit=1`, never touches the
        # scheduler, and -- crucially -- never calls `_for_worker_thread`
        # on that path.
        import threading
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))

        caller_thread = threading.get_ident()
        worker_threads: list[int] = []

        def fake_process(_gh, _spec, _issue) -> None:
            worker_threads.append(threading.get_ident())

        clone = MagicMock(side_effect=lambda: self.fail(
            "_for_worker_thread must not be called on the legacy path"
        ))
        with patch.object(gh, "_for_worker_thread", clone), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(worker_threads, [caller_thread])
        clone.assert_not_called()

    def test_refresh_skips_active_issue_on_next_tick(self) -> None:
        # The "active issues are skipped until completion" requirement
        # has to hold for the pre-tick base refresh too, not just the
        # scheduler.submit gate. The refresh iterates per-issue
        # worktrees and either rebases (pre-PR) or relabels /
        # state-mutates (PR-having); racing that against a still-
        # running handler corrupts the worktree under the agent or
        # clobbers pinned state mid-write.
        #
        # Drive two ticks: tick 1 dispatches the issue and the worker
        # holds inside `_process_issue`. Tick 2 calls the refresh
        # helper -- but because the scheduler reports the issue as
        # active, the refresh must skip its per-worktree sync. This
        # test inspects how `_refresh_base_and_worktrees` (the real
        # one, not a mock) treats the active-issue case by patching
        # only the inner `_sync_worktree_with_base` step, which is
        # what would actually mutate the worktree / pinned state.
        import threading
        from orchestrator import worktrees
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label="implementing"))

        start = threading.Event()
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            start.set()
            release.wait(timeout=5.0)

        # Stub fetch + iterdir so the real `_refresh_base_and_worktrees`
        # runs but never touches the filesystem or the network. The
        # scheduler-aware skip lives in the per-worktree loop; if it
        # regressed, `sync` would be called for the still-active
        # issue.
        sync_calls: list[int] = []
        sync_lock = threading.Lock()

        def fake_sync(_gh, _spec, _wt, issue_number) -> None:
            with sync_lock:
                sync_calls.append(issue_number)

        class _FakeWtDir:
            def __init__(self, name: str) -> None:
                self.name = name
            def is_dir(self) -> bool:
                return True

        fake_fetch_result = type("R", (), {"returncode": 0, "stderr": ""})()
        fake_root = type(
            "Root", (),
            {
                "exists": lambda self: True,
                "iterdir": lambda self: [_FakeWtDir("issue-7")],
            },
        )()

        try:
            with patch.object(
                base_sync, "_authed_target_fetch",
                return_value=fake_fetch_result,
            ), patch.object(
                base_sync, "_repo_worktrees_root", return_value=fake_root,
            ), patch.object(
                base_sync, "_sync_worktree_with_base", side_effect=fake_sync,
            ), patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: handler is dispatched and parks in fake_process.
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    start.wait(timeout=2.0),
                    "worker never entered _process_issue",
                )
                self.assertTrue(sched.is_active("acme/widget", 7))
                # The first tick's refresh ran while issue #7 was NOT
                # yet active in the scheduler (the worker is dispatched
                # AFTER refresh completes), so `_sync_worktree_with_base`
                # may or may not have been called depending on ordering.
                # Reset the call log before the second tick so the
                # assertion below isolates the "active issue skip"
                # property.
                with sync_lock:
                    sync_calls.clear()

                # Tick 2: scheduler still reports #7 as active. The
                # refresh helper must observe that and skip the
                # per-worktree sync entirely.
                workflow.tick(gh, self._spec(), scheduler=sched)
                with sync_lock:
                    self.assertEqual(
                        sync_calls, [],
                        "refresh did not skip active issue's worktree; "
                        "_sync_worktree_with_base was called for an "
                        "in-flight handler",
                    )

                # Release the worker, wait for the slot to clear, and
                # confirm a follow-up tick DOES sync the (now idle)
                # worktree -- the skip is conditional on active state,
                # not a permanent suppression.
                release.set()
            self._wait_idle(sched, "acme/widget")

            with patch.object(
                base_sync, "_authed_target_fetch",
                return_value=fake_fetch_result,
            ), patch.object(
                base_sync, "_repo_worktrees_root", return_value=fake_root,
            ), patch.object(
                base_sync, "_sync_worktree_with_base", side_effect=fake_sync,
            ), patch.object(workflow, "_process_issue"):
                workflow.tick(gh, self._spec(), scheduler=sched)
                with sync_lock:
                    self.assertIn(7, sync_calls)
        finally:
            release.set()

    def test_scheduler_path_uses_per_worker_client_and_refetches_issue(
        self,
    ) -> None:
        # The scheduler dispatch must mirror the legacy parallel path:
        # mint a worker-thread client via `_for_worker_thread()` and
        # refetch the Issue against that client so PyGithub's
        # Requester chain isn't shared across threads.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))

        clone_calls: list[int] = []
        clone_lock = threading.Lock()
        cloned_clients: list[FakeGitHubClient] = []

        def fake_clone() -> FakeGitHubClient:
            twin = FakeGitHubClient()
            twin.add_issue(make_issue(1, label="implementing"))
            with clone_lock:
                clone_calls.append(1)
                cloned_clients.append(twin)
            return twin

        seen_client_ids: list[int] = []

        def fake_process(worker_gh, _spec, _issue) -> None:
            seen_client_ids.append(id(worker_gh))

        with patch.object(gh, "_for_worker_thread", fake_clone), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self._wait_idle(sched, "acme/widget")

        self.assertEqual(len(cloned_clients), 1)
        # The parent client is NOT what the worker saw.
        self.assertNotIn(id(gh), seen_client_ids)
        self.assertEqual(seen_client_ids[0], id(cloned_clients[0]))


class RefreshBaseAndWorktreesRealGitTest(unittest.TestCase):
    """Integration coverage for `_refresh_base_and_worktrees` against a real
    bare remote + per-issue worktree. Mirrors `SquashHelperRealGitTest`'s
    setup so the helper's interaction with `git fetch` / `git rebase` /
    `git rebase --abort` is exercised end-to-end.
    """

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, env=env, check=True,
        )
        return r.stdout

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-real-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        self._author_env = author_env
        (self.work / "README.md").write_text("hello\n")
        self._git("add", ".", cwd=self.work)
        self._git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        self._git("push", "origin", "main", cwd=self.work)

        # Per-issue worktree branched off origin/main, with one local commit.
        self.wt_root = self.tmpdir / "worktrees" / "acme__widget"
        self.wt_root.mkdir(parents=True)
        self.wt = self.wt_root / "issue-7"
        self._git(
            "worktree", "add", "-b", "orchestrator/issue-7",
            str(self.wt), "origin/main", cwd=self.work,
        )
        (self.wt / "feature.py").write_text("feature\n")
        self._git("add", ".", cwd=self.wt)
        self._git(
            "commit", "-m", "feat: add feature", cwd=self.wt,
            env_extra=author_env,
        )

        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=self.work,
            base_branch="main",
        )
        # Default: per-issue worktree #7 is in `implementing` (no PR yet),
        # so the refresh is allowed to rebase it onto base. Tests that want
        # the PR-skip path call `_seed_pr_state(7)`.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="implementing"))

        # `_authed_target_fetch` would otherwise dial out to
        # `https://x-access-token@github.com/acme/widget.git`, which
        # does not exist for our local bare remote. Redirect it to a
        # plain `git fetch <remote_name> <branch>` against the
        # local-clone `origin` so the integration test still exercises
        # the post-fetch merge / refresh logic end-to-end.
        def _local_fetch(spec, branch):
            r = subprocess.run(
                ["git", "fetch", "--quiet", spec.remote_name, branch],
                cwd=str(spec.target_root),
                capture_output=True, text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return r

        self._fetch_patch = patch.object(
            base_sync, "_authed_target_fetch", side_effect=_local_fetch,
        )
        self._fetch_patch.start()
        self.addCleanup(self._fetch_patch.stop)

    def _seed_pr_state(
        self, issue_number: int, pr_number: int = 999, *,
        merged: bool = False, state: str = "open",
    ) -> None:
        self.gh.seed_state(
            issue_number, pr_number=pr_number,
            branch=f"orchestrator/issue-{issue_number}",
        )
        self.gh.add_pr(FakePR(
            number=pr_number,
            head_branch=f"orchestrator/issue-{issue_number}",
            merged=merged, state=state,
        ))

    def _advance_base(self, *, conflicting: bool) -> None:
        """Push a new commit to origin/main. When `conflicting=True`, the
        commit edits `feature.py` so a base rebase of the per-issue branch
        will conflict with the local feature commit.
        """
        self._git("checkout", "main", cwd=self.work)
        path = self.work / ("feature.py" if conflicting else "extra.txt")
        path.write_text("base side\n")
        self._git("add", ".", cwd=self.work)
        self._git(
            "commit", "-m", "base advance", cwd=self.work,
            env_extra=self._author_env,
        )
        self._git("push", "origin", "main", cwd=self.work)

    def _wt_head(self) -> str:
        return self._git("rev-parse", "HEAD", cwd=self.wt).strip()

    def _is_clean(self) -> bool:
        return self._git("status", "--porcelain", cwd=self.wt).strip() == ""

    def test_clean_advance_rebases_worktree(self) -> None:
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        head_after = self._wt_head()
        self.assertNotEqual(head_before, head_after)
        # The base file landed in the worktree's tree.
        self.assertTrue((self.wt / "extra.txt").exists())
        self.assertEqual(
            self._git("log", "-1", "--format=%s", cwd=self.wt).strip(),
            "feat: add feature",
        )
        self.assertTrue(self._is_clean())

    def test_no_op_when_already_up_to_date(self) -> None:
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())

    def test_conflict_aborts_leaving_worktree_clean(self) -> None:
        self._advance_base(conflicting=True)
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # HEAD did NOT move (rebase aborted) and worktree is clean again --
        # the conflict surfaces later via the resolving_conflict stage.
        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())

    def test_dirty_worktree_skipped_without_disturbing_changes(self) -> None:
        self._advance_base(conflicting=False)
        # Plant an uncommitted edit in the worktree -- mirrors a mid-flight
        # agent edit. The base rebase must NOT run.
        (self.wt / "scratch.py").write_text("scratch\n")
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        self.assertEqual(head_before, self._wt_head())
        # Untracked file still present, nothing else was added.
        self.assertTrue((self.wt / "scratch.py").exists())
        self.assertFalse((self.wt / "extra.txt").exists())

    def test_pr_open_worktree_is_not_merged_locally(self) -> None:
        # Regression: once a PR exists, the per-issue branch has been pushed
        # and `pr.head.sha` equals local HEAD. A local-only base rebase would
        # diverge them and break the validating reviewer (it reads local
        # HEAD) and `_squash_and_force_push`'s lease check (it expects the
        # remote to equal `original_head` = local HEAD). The refresh must
        # NOT do a local rebase here; instead it routes the issue to
        # `resolving_conflict` so the existing handler does rebase + push +
        # relabel-to-validating in one consistent flow.
        # Replace the default `implementing` issue with one in `in_review`
        # plus the PR-having pinned state.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="in_review"))
        self._seed_pr_state(7, pr_number=42)
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # HEAD did NOT move: no local-only rebase was performed.
        self.assertEqual(head_before, self._wt_head())
        # The base file did NOT land in the worktree (not yet -- it will
        # after `_handle_resolving_conflict` runs and pushes).
        self.assertFalse((self.wt / "extra.txt").exists())
        # But the issue WAS routed to resolving_conflict so the handler
        # picks it up.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)


class ComputeUserContentHashTest(unittest.TestCase):
    """The hash must include user-visible content (title, body, human
    comments) and exclude orchestrator-authored content (pinned-state
    marker comments, anything in `orchestrator_comment_ids`). Author-login
    matching is intentionally avoided because the orchestrator PAT is
    often shared with a human reviewer's GitHub account."""

    def test_hash_changes_when_body_changes(self) -> None:
        issue_a = make_issue(1, body="old body")
        issue_b = make_issue(1, body="new body")
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_a, set()),
            workflow._compute_user_content_hash(issue_b, set()),
        )

    def test_hash_changes_when_title_changes(self) -> None:
        issue_a = make_issue(1, title="old", body="b")
        issue_b = make_issue(1, title="new", body="b")
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_a, set()),
            workflow._compute_user_content_hash(issue_b, set()),
        )

    def test_orchestrator_comments_filtered_by_id(self) -> None:
        # A human comment with the same body as a bot comment must still
        # affect the hash; only the recorded bot id is filtered.
        human = FakeComment(id=100, body="please retry", user=FakeUser("alice"))
        bot = FakeComment(id=200, body="picking this up", user=FakeUser("alice"))
        issue_with_human = make_issue(1, comments=[human])
        issue_with_both = make_issue(1, comments=[human, bot])
        self.assertEqual(
            workflow._compute_user_content_hash(issue_with_human, {200}),
            workflow._compute_user_content_hash(issue_with_both, {200}),
        )
        # Without filtering 200, the hash differs.
        self.assertNotEqual(
            workflow._compute_user_content_hash(issue_with_human, set()),
            workflow._compute_user_content_hash(issue_with_both, set()),
        )

    def test_pinned_state_marker_comment_is_filtered_by_marker(self) -> None:
        pinned = FakeComment(
            id=300, body="<!--orchestrator-state {\"k\": 1}-->",
        )
        issue = make_issue(1)
        issue_with_pinned = make_issue(1, comments=[pinned])
        # Pinned-state comment id is NOT in orchestrator_ids but its marker
        # body causes it to be filtered.
        self.assertEqual(
            workflow._compute_user_content_hash(issue, set()),
            workflow._compute_user_content_hash(issue_with_pinned, set()),
        )


class DetectUserContentChangeTest(unittest.TestCase):
    def test_first_call_persists_durably_and_returns_none(self) -> None:
        # The first encounter has no baseline; we record the current value
        # AND write pinned state immediately so a parked/idle tick can't
        # silently absorb a later edit as the new baseline.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        state = gh.read_pinned_state(issue)
        before = gh.write_state_calls
        result = workflow._detect_user_content_change(gh, issue, state)
        self.assertIsNone(result)
        self.assertEqual(
            state.get("user_content_hash"),
            workflow._compute_user_content_hash(issue, set()),
        )
        # Durably written so a later edit after an early-return tick is
        # correctly classified as drift, not absorbed as the new baseline.
        self.assertEqual(gh.write_state_calls, before + 1)
        self.assertEqual(
            gh.pinned_data(1).get("user_content_hash"),
            state.get("user_content_hash"),
        )

    def test_unchanged_returns_none(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        prior_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(1, user_content_hash=prior_hash)
        state = gh.read_pinned_state(issue)
        before = gh.write_state_calls
        self.assertIsNone(
            workflow._detect_user_content_change(gh, issue, state)
        )
        # No extra write when the baseline already matches.
        self.assertEqual(gh.write_state_calls, before)

    def test_body_change_returns_new_hash_without_auto_persist(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue_a = make_issue(1, body="old")
        prior = workflow._compute_user_content_hash(issue_a, set())
        issue_b = make_issue(1, body="new body")
        gh.add_issue(issue_b)
        gh.seed_state(1, user_content_hash=prior)
        state = gh.read_pinned_state(issue_b)
        before = gh.write_state_calls
        result = workflow._detect_user_content_change(gh, issue_b, state)
        self.assertEqual(
            result, workflow._compute_user_content_hash(issue_b, set())
        )
        self.assertNotEqual(result, prior)
        # The helper does NOT auto-persist on a real change; the caller
        # decides whether to act and persist (so the routing branches can
        # use the comparison without committing to a state write).
        self.assertEqual(gh.write_state_calls, before)
        self.assertEqual(state.get("user_content_hash"), prior)


class HandlePickupInitializesUserContentHashTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    def test_pickup_with_decompose_off_seeds_hash(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="q"),
                has_new_commits=False,
            )

        data = gh.pinned_data(1)
        self.assertIn("user_content_hash", data)
        # Hash filters the pickup comment by id (it has been recorded in
        # `orchestrator_comment_ids`), so it should match a re-computation
        # over the same set.
        orch_ids = set(data.get("orchestrator_comment_ids") or [])
        self.assertEqual(
            data["user_content_hash"],
            workflow._compute_user_content_hash(issue, orch_ids),
        )

    def test_pickup_with_decompose_on_seeds_hash(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", True):
            mocks = self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess",
                    last_message=(
                        "fits one\n\n```orchestrator-manifest\n"
                        '{"decision": "single", "rationale": "small"}\n'
                        "```"
                    ),
                ),
            )

        data = gh.pinned_data(2)
        self.assertIn("user_content_hash", data)


class UserContentChangePromptIncludesCommentsTest(unittest.TestCase):
    """A drift triggered by a NEW human comment (not a body edit) must
    surface that comment to the dev. Quoting only title/body would leave
    the dev unaware of the acceptance criterion the human just posted."""

    def test_recent_comments_are_quoted_in_resume_prompt(self) -> None:
        issue = make_issue(1, title="t", body="b")
        issue.comments.append(FakeComment(
            id=500, body="new acceptance criterion: handle empty input",
            user=FakeUser("alice"),
        ))
        comments_text = workflow._recent_comments_text(issue)
        prompt = workflow._build_user_content_change_prompt(
            issue, comments_text,
        )
        self.assertIn("new acceptance criterion", prompt)
        self.assertIn("Conversation so far", prompt)
        self.assertIn("Updated issue body", prompt)


class FirstTimeHashSeedingIsDurableTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 3: `_detect_user_content_change` must persist the
    first-time baseline via `gh.write_pinned_state` immediately, so a
    later edit after a parked/idle tick is not silently absorbed as the
    new baseline."""

    def test_validating_awaiting_human_no_reply_persists_baseline(
        self,
    ) -> None:
        # Legacy state (no `user_content_hash`) parked on awaiting_human
        # with no new comments. `_handle_validating`'s awaiting-human
        # path returns without writing state on a real no-reply tick;
        # the durability fix in `_detect_user_content_change` must still
        # have written the baseline by then.
        gh = FakeGitHubClient()
        issue = make_issue(100, label="validating", body="initial body")
        gh.add_issue(issue)
        pr = FakePR(number=1000, head_branch="orchestrator/issue-100")
        gh.add_pr(pr)
        gh.seed_state(
            100,
            pr_number=pr.number,
            awaiting_human=True,
            last_action_comment_id=500,
            review_round=1,
        )

        # Tick: no new comments and no hash baseline. Park branch
        # returns early without writing state.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Baseline durably persisted by the first-call branch in
        # `_detect_user_content_change`.
        data = gh.pinned_data(100)
        self.assertIsNotNone(data.get("user_content_hash"))

    def test_blocked_child_no_op_persists_baseline(self) -> None:
        # A `blocked` child waiting on a sibling is a per-tick no-op.
        # Without the durability fix, a later edit during the wait would
        # silently become the new baseline because the no-op branch
        # returns without `write_pinned_state`.
        gh = FakeGitHubClient()
        child = make_issue(200, label="blocked", body="child body")
        gh.add_issue(child)
        gh.seed_state(200, parent_number=199)

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        data = gh.pinned_data(200)
        self.assertIsNotNone(data.get("user_content_hash"))


class NoCommitAckDoesNotParkTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 4: a harmless clarification edit can elicit a
    no-commit reply from the dev ('existing work satisfies'). The
    validating / in_review / resolving_conflict drift paths must treat
    that as an ack rather than parking awaiting_human."""

    def test_validating_ack_without_commit_does_not_park(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(600, label="validating", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=6000, head_branch="orchestrator/issue-600")
        gh.add_pr(pr)
        gh.seed_state(
            600,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            review_round=1,
            branch="orchestrator/issue-600",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Reviewed the clarified body.\n\n"
                    "ACK: existing commits already cover the clarified body"
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same-sha", "same-sha"],
        )

        data = gh.pinned_data(600)
        # Crucial: must NOT park as a question.
        self.assertFalse(data.get("awaiting_human"))
        # Dev's ACK justification was posted on the issue as an FYI.
        self.assertTrue(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))

    def test_in_review_ack_without_commit_bounces_to_validating(
        self,
    ) -> None:
        # A no-commit "ack" reply from the dev on an in_review drift
        # MUST bounce DIRECTLY back to `validating` (same destination
        # as the pushed-fix exit; docs do not run on the drift exit,
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff). `review_round`
        # resets so the validating cap counts fresh rounds.
        gh = FakeGitHubClient()
        issue = make_issue(700, label="in_review", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=7000, head_branch="orchestrator/issue-700")
        gh.add_pr(pr)
        gh.seed_state(
            700,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-700",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message="ACK: no additional code change needed",
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same", "same"],
        )

        data = gh.pinned_data(700)
        # Must NOT park (the dev acknowledged, not asked a question).
        self.assertFalse(data.get("awaiting_human"))
        # MUST bounce directly to validating (no documenting hop) so
        # the reviewer re-evaluates against the updated body.
        self.assertIn((700, "validating"), gh.label_history)
        # And NOT through documenting -- no commit landed.
        self.assertNotIn((700, "documenting"), gh.label_history)
        # review_round reset so the validating cap counts fresh rounds.
        self.assertEqual(data.get("review_round"), 0)
        # Dev's reply still posted on the issue as an FYI.
        self.assertTrue(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))


class DriftMarksCommentsConsumedTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: the drift paths feed the dev session the full
    issue thread via `_recent_comments_text`, so `last_action_comment_id`
    must advance past every visible comment. Otherwise the next
    validating->in_review handoff's `_seed_watermark_past_self` stops at
    the same human comment and replays it as fresh PR feedback,
    triggering a duplicate dev resume."""

    def test_validating_drift_bumps_last_action_past_human_comment(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(900, label="validating", body="new body")
        # Pre-existing human comment with a high id -- representing the
        # comment that arrived at the same time as the body edit.
        human = FakeComment(
            id=5000, body="add this acceptance criterion",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=9000, head_branch="orchestrator/issue-900")
        gh.add_pr(pr)
        gh.seed_state(
            900,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            review_round=1,
            branch="orchestrator/issue-900",
            last_action_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="fixed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after"],
        )

        data = gh.pinned_data(900)
        # last_action_comment_id advanced past the human comment so the
        # eventual handoff to in_review does not classify it as fresh
        # feedback.
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 5000,
        )

    def test_in_review_fresh_human_comment_routes_to_fixing_not_drift(
        self,
    ) -> None:
        # Regression for the reviewer's bug: a fresh issue-thread human
        # comment used to trip `user_content_hash` (which covers comments
        # too) and the drift path would resume the dev + bounce to
        # `validating` instead of the contracted route to `fixing`. With
        # the in_review handler scanning fresh feedback BEFORE the drift
        # check, the issue-thread comment now routes to `fixing` and the
        # hash is recomputed so the drift path does not double-fire on the
        # same comment changes next tick.
        gh = FakeGitHubClient()
        issue = make_issue(910, label="in_review", body="new body")
        human = FakeComment(
            id=6000, body="please also handle X",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=9100, head_branch="orchestrator/issue-910")
        gh.add_pr(pr)
        gh.seed_state(
            910,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-910",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No dev spawn, no bounce to `validating`: the fixing route owns
        # this signal.
        mocks["run_agent"].assert_not_called()
        self.assertIn((910, "fixing"), gh.label_history)
        self.assertNotIn((910, "validating"), gh.label_history)
        data = gh.pinned_data(910)
        # The triggering comment is bookmarked for the fixing handler.
        self.assertEqual(data.get("pending_fix_issue_max_id"), 6000)
        # Hash is updated so the drift check does not re-fire on the
        # same comment change after the fixing handler (or an operator
        # relabel) bounces the issue back to `in_review`.
        self.assertNotEqual(data.get("user_content_hash"), "stale-hash")
        # Watermark is deliberately left at the route-time value so the
        # fixing handler can read the triggering comment to build its
        # dev-resume prompt (the bookmark above tells it where to start).
        # The fixing handler advances this watermark itself once the
        # consumed feedback has been fed to the dev.
        self.assertEqual(data.get("pr_last_comment_id"), 0)

    def test_implementing_drift_bumps_last_action_past_human_comment(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(920, label="implementing", body="new body")
        human = FakeComment(
            id=7000, body="here are more requirements",
            user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        gh.seed_state(
            920,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            awaiting_human=True,
            last_action_comment_id=100,
            branch="orchestrator/issue-920",
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="implemented"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before-resume", "after-resume"],
        )

        data = gh.pinned_data(920)
        # The dev's commit goes through `_on_commits` which flips to
        # validating; the validating->in_review handoff later reads
        # last_action_comment_id, so we must have bumped past 7000.
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 7000,
        )

    def test_resolving_conflict_drift_bumps_last_action(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            930, label="resolving_conflict", body="new body",
        )
        human = FakeComment(
            id=8000, body="more context", user=FakeUser("alice"),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=9300, head_branch="orchestrator/issue-930")
        gh.add_pr(pr)
        gh.seed_state(
            930,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            conflict_round=0,
            branch="orchestrator/issue-930",
            last_action_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="resolved"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", "after", "after"],
        )

        data = gh.pinned_data(930)
        # After the pushed resolution flips to validating, the
        # subsequent handoff back to in_review must not replay the human
        # comment that arrived during conflict resolution.
        self.assertGreaterEqual(
            int(data.get("last_action_comment_id")), 8000,
        )


class OrchCommentMarkerSurvivesIdCapTest(unittest.TestCase):
    """Reviewer point 3: `orchestrator_comment_ids` is capped, but the
    hash scans every comment. Once an old orchestrator-comment id is
    evicted from the cap, an id-only filter would start including the
    bot comment in the hash and trigger false drift each tick. The body
    marker (`_ORCH_COMMENT_MARKER`) must keep the hash stable."""

    def test_marker_excludes_orchestrator_comment_even_when_id_unknown(
        self,
    ) -> None:
        # Simulate an orchestrator comment whose id has been evicted
        # from the bounded cap. Its body still carries the marker
        # (because every orchestrator comment is posted with it), so
        # the hash filter must drop it.
        bot_body = "picking this up\n\n" + workflow._ORCH_COMMENT_MARKER
        bot = FakeComment(id=12345, body=bot_body, user=FakeUser("alice"))
        human = FakeComment(
            id=12346, body="please reconsider", user=FakeUser("alice"),
        )
        issue_with_just_human = make_issue(1, comments=[human])
        issue_with_both = make_issue(1, comments=[bot, human])
        # `orchestrator_ids` is EMPTY (the id was evicted from the cap),
        # but the hash must still match because the marker identifies
        # the bot comment.
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_just_human, set()
            ),
            workflow._compute_user_content_hash(
                issue_with_both, set()
            ),
        )

    def test_marker_is_appended_by_post_helpers(self) -> None:
        # Every orchestrator-posted comment must carry the marker so
        # the hash filter survives id-cap eviction.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)
        state = workflow.PinnedState()
        workflow._post_issue_comment(gh, issue, state, "hello")
        # The body actually written to the issue carries the marker.
        last_body = issue.comments[-1].body
        self.assertIn(workflow._ORCH_COMMENT_MARKER, last_body)
        # And it starts with the original body text.
        self.assertTrue(last_body.startswith("hello"))

    def test_marker_is_idempotent_on_double_wrap(self) -> None:
        # Defensive: a caller that already passes a body containing the
        # marker (e.g. a future helper forwards a pre-built body) must
        # not get the marker appended twice -- two markers in one body
        # is harmless but ugly, and an idempotent wrap also keeps
        # `_with_orch_marker` safe to call from helper chains.
        marked = workflow._with_orch_marker("hi")
        twice = workflow._with_orch_marker(marked)
        self.assertEqual(marked, twice)
        self.assertEqual(twice.count(workflow._ORCH_COMMENT_MARKER), 1)


class HashFiltersBotUsersTest(unittest.TestCase):
    """Reviewer point 2: third-party Bot/App accounts (Dependabot,
    Renovate, CI bots) post comments structurally on long-lived issues.
    The hash must filter them by GitHub's `user.type == "Bot"` flag so
    a periodic bot comment doesn't re-trigger drift on every tick it
    posts. Login matching is intentionally avoided because the
    orchestrator PAT may be shared with a human reviewer's account."""

    def test_bot_authored_comment_is_filtered(self) -> None:
        # A Dependabot-style comment must NOT affect the hash even
        # though its body is unique and its id is not tracked.
        human = FakeComment(
            id=900, body="real human comment", user=FakeUser("alice"),
        )
        bot_comment = FakeComment(
            id=901,
            body="Bumps `requests` from 2.31.0 to 2.32.0",
            user=FakeUser("dependabot[bot]", type="Bot"),
        )
        issue_with_just_human = make_issue(1, comments=[human])
        issue_with_bot = make_issue(1, comments=[human, bot_comment])
        self.assertEqual(
            workflow._compute_user_content_hash(
                issue_with_just_human, set()
            ),
            workflow._compute_user_content_hash(
                issue_with_bot, set()
            ),
        )

    def test_user_type_human_still_contributes(self) -> None:
        # A regular human user's `type == "User"` must NOT be filtered.
        comment = FakeComment(
            id=910,
            body="adds an acceptance criterion",
            user=FakeUser("alice", type="User"),
        )
        empty = make_issue(1)
        with_human = make_issue(1, comments=[comment])
        self.assertNotEqual(
            workflow._compute_user_content_hash(empty, set()),
            workflow._compute_user_content_hash(with_human, set()),
        )


class DriftAckRequiresExplicitMarkerTest(unittest.TestCase):
    """Reviewer point: a generic non-empty no-commit response is OFTEN a
    clarification question, not an ack. Only an explicit `ACK: ...`
    marker should be treated as acknowledgement; everything else parks
    awaiting human via `_on_question`."""

    def test_explicit_ack_marker_extracts_reason(self) -> None:
        msg = (
            "I reviewed the change.\n\n"
            "ACK: existing tests already cover the new requirement"
        )
        self.assertEqual(
            workflow._drift_ack_reason(msg),
            "existing tests already cover the new requirement",
        )

    def test_ack_is_case_insensitive_and_last_wins(self) -> None:
        # Case insensitive (mirrors VERDICT parsing) and the LAST marker
        # wins so a sample/template `ACK:` quoted earlier in the message
        # doesn't override the agent's real concluding marker.
        msg = (
            "I considered ack: stale-template-text but on re-reading\n\n"
            "ack: real final justification"
        )
        self.assertEqual(
            workflow._drift_ack_reason(msg),
            "real final justification",
        )

    def test_no_marker_returns_none(self) -> None:
        # Generic "satisfied" prose without the marker is NOT an ack.
        # `_post_user_content_change_result` parks via `_on_question`
        # on this branch so a real question isn't swallowed.
        msg = "Existing code already covers this; no change needed."
        self.assertIsNone(workflow._drift_ack_reason(msg))

    def test_clarification_question_returns_none(self) -> None:
        msg = "Should I also handle the empty-input case?"
        self.assertIsNone(workflow._drift_ack_reason(msg))


class DriftNonAckResponseParksTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """A non-empty no-commit response WITHOUT the `ACK:` marker -- e.g.
    a clarification question -- must park awaiting human, not silently
    advance the workflow with a misleading "satisfies" comment."""

    def test_validating_drift_clarification_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(601, label="validating", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=6001, head_branch="orchestrator/issue-601")
        gh.add_pr(pr)
        gh.seed_state(
            601,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            review_round=1,
            branch="orchestrator/issue-601",
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Should the empty-input case also raise, or return "
                    "an empty list? Need clarification."
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same-sha", "same-sha"],
        )

        data = gh.pinned_data(601)
        # Must park awaiting human so the real question isn't lost.
        self.assertTrue(data.get("awaiting_human"))
        # Must NOT have posted the misleading "satisfies" comment.
        self.assertFalse(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))
        # The question text was surfaced via `_on_question`.
        self.assertTrue(any(
            "Should the empty-input case" in body
            for _, body in gh.posted_comments
        ))

    def test_in_review_drift_clarification_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701, label="in_review", body="clarified body")
        gh.add_issue(issue)
        pr = FakePR(number=7001, head_branch="orchestrator/issue-701")
        gh.add_pr(pr)
        gh.seed_state(
            701,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            user_content_hash="stale-hash",
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/issue-701",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "Does the updated body imply I should also rename "
                    "`old_fn`? Please confirm."
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["same", "same"],
        )

        data = gh.pinned_data(701)
        # Park flagged.
        self.assertTrue(data.get("awaiting_human"))
        # NOT bounced to validating: the dev didn't ack OR commit, so
        # the in_review label is preserved and the human resolves the
        # question.
        self.assertNotIn((701, "validating"), gh.label_history)
        # Misleading "satisfies" comment NOT posted.
        self.assertFalse(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))

    def test_implementing_drift_clarification_question_parks(
        self,
    ) -> None:
        # The implementing-stage inline drift handler shares the same
        # contract: non-empty + no-commit + no ACK -> park as question.
        gh = FakeGitHubClient()
        issue = make_issue(
            602, label="implementing", body="updated requirements",
        )
        gh.add_issue(issue)
        gh.seed_state(
            602,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=False,
            branch="orchestrator/issue-602",
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess",
                last_message=(
                    "I'd like to clarify: should the schema migration "
                    "run forward-only or also support rollback?"
                ),
            ),
            has_new_commits=False,
            dirty_files=(),
            head_shas=["sha-before", "sha-before"],
        )

        data = gh.pinned_data(602)
        self.assertTrue(data.get("awaiting_human"))
        self.assertFalse(any(
            "existing work satisfies the edit" in body
            for _, body in gh.posted_comments
        ))
        # The dev's question was surfaced.
        self.assertTrue(any(
            "schema migration" in body
            for _, body in gh.posted_comments
        ))


class WorktreePlumbingSerializationTest(unittest.TestCase):
    """`tick()` fans non-family-aware stages out across worker threads, so
    `_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`
    can be invoked concurrently against the same `spec.target_root`. The
    git plumbing those helpers run -- `git fetch`, `git worktree add`,
    `git worktree remove` -- writes the parent clone's `.git/config` under
    `.git/config.lock`. Without per-target_root serialization git reports
    `error: could not lock config file .git/config: File exists` and the
    worker fails before its agent ever spawns. These tests pin the lock
    contract down with both a deterministic blocking-fake unit test (every
    `_git` call records concurrency against the lock) and a real-git
    integration smoke test (10 workers, real `git worktree add` against
    a real bare remote)."""

    def setUp(self) -> None:
        # Clear the module-level lock dict so tests do not leak per-key
        # locks across runs (a stale lock from a previous test pointing
        # at a deleted tmp dir would still satisfy the API but would
        # spuriously serialize against a different test's lookup key).
        import threading
        worktrees._TARGET_ROOT_LOCKS.clear()
        # Sanity: the guard lock itself is recreated, not reused. Tests
        # do not need a fresh guard lock but `clear()` empties the dict
        # under the existing guard, which is fine.
        self.assertIsInstance(worktrees._TARGET_ROOT_LOCKS_LOCK, type(threading.Lock()))

    def test_target_root_lock_serializes_concurrent_callers(self) -> None:
        # Drive `_ensure_worktree` against the SAME `spec.target_root`
        # from multiple threads with a `_git` patch that records every
        # subprocess invocation's concurrency. With the lock in place,
        # max-in-flight against target_root must be 1; without it, the
        # threads would interleave their git calls and trip an
        # assertion here.
        import threading
        from unittest.mock import MagicMock

        target_root = Path("/tmp/orchestrator-test-shared-target-root")
        spec = config.RepoSpec(
            slug="acme/widget", target_root=target_root, base_branch="main",
        )

        in_flight = 0
        max_in_flight = 0
        order: list[str] = []
        lock = threading.Lock()

        def fake_git(*args, cwd) -> MagicMock:
            # Every `_git(...)` call against this target_root counts -- a
            # `worktree add` is just one of several plumbing operations
            # that all share `.git/config.lock`.
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                order.append(f"{args[0]}({threading.get_ident()})")
            # Sleep so threads piling up on the same target_root actually
            # overlap if the lock isn't holding them.
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            # Mimic `subprocess.CompletedProcess` enough for the helper:
            # returncode=0 for everything, plus `.stderr=""` /
            # `.stdout=""` defaults via MagicMock auto-attrs.
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_authed_fetch(spec, branch):
            # The base-branch fetch also runs under the lock; count it
            # the same way so the serialization assertion holds.
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                order.append(f"fetch({threading.get_ident()})")
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_has_new_commits(*_a, **_kw) -> bool:
            return False  # force the "(re)create" branch every time.

        def call_ensure(n: int) -> None:
            worktrees._ensure_worktree(spec, n)

        with patch.object(worktree_lifecycle, "_git", side_effect=fake_git), \
             patch.object(
                 worktree_lifecycle, "_authed_target_fetch",
                 side_effect=fake_authed_fetch,
             ), \
             patch.object(worktree_lifecycle, "_has_new_commits", fake_has_new_commits), \
             patch.object(Path, "exists", lambda self: False), \
             patch.object(Path, "mkdir", lambda self, **_kw: None):
            threads = [
                threading.Thread(target=call_ensure, args=(n,))
                for n in (1, 2, 3, 4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive(), "worker timed out")

        # Every `_git` invocation against this target_root was serialized:
        # the per-target_root lock kept max-in-flight at 1 despite four
        # concurrent callers.
        self.assertEqual(
            max_in_flight, 1,
            f"git plumbing was not serialized; observed order={order!r}",
        )
        # And we actually drove the workers (sanity check).
        self.assertGreaterEqual(len(order), 4)

    def test_authed_fetch_is_serialized_per_target_root(self) -> None:
        # `_authed_fetch` updates `refs/remotes/<remote>/<branch>` in the
        # parent clone's git directory (worktrees share the parent's
        # `.git/refs` namespace). Two concurrent `_authed_fetch` calls
        # from different worktrees of the same target_root therefore
        # race on `<branch>.lock` / `packed-refs.lock` and one can fail
        # with `Unable to create '...': File exists`. The reviewer
        # specifically called out the `resolving_conflict` handler at
        # workflow.py:1646 -- it calls `_authed_fetch` against
        # `refs/heads/<base>` which is the single most-contended ref.
        # The fix wraps the actual `git fetch` subprocess in
        # `_target_root_lock`. This test patches `subprocess.run` to
        # record concurrency across the lock-protected critical
        # section and asserts max-in-flight == 1.
        import threading
        from unittest.mock import MagicMock

        target_root = Path("/tmp/orchestrator-test-authed-fetch-target-root")
        spec = config.RepoSpec(
            slug="acme/widget", target_root=target_root, base_branch="main",
        )
        wt = Path("/tmp/orchestrator-test-authed-fetch-worktree")

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        # Track ONLY the `git fetch ...` call (not the pre-flight
        # `git config --local --get-regexp ...` check, which runs
        # outside the target_root lock on the worktree's own config).
        def fake_subprocess_run(args, **_kw) -> MagicMock:
            nonlocal in_flight, max_in_flight
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if len(args) >= 2 and args[-2:] != ["fetch", "--quiet"]:
                # The fetch invocation is `[git_prefix..., "fetch",
                # "--quiet", auth_url, refspec]`. Match on "fetch" being
                # present after the `git` binary + `-c` flags.
                pass
            if "fetch" in args and "--quiet" in args:
                with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                time.sleep(0.02)
                with lock:
                    in_flight -= 1
            return r

        # `_resolve_github_token` must return non-empty so `_authed_fetch`
        # does not short-circuit before the lock.
        with patch.object(
            config, "_resolve_github_token", return_value="ghp-test",
        ), patch.object(worktrees.subprocess, "run", side_effect=fake_subprocess_run):
            threads = [
                threading.Thread(
                    target=lambda i=i: worktrees._authed_fetch(
                        spec,
                        f"+refs/heads/main:refs/remotes/origin/main",
                        cwd=wt,
                    ),
                )
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive())

        self.assertEqual(
            max_in_flight, 1,
            "_authed_fetch did not serialize concurrent fetches against "
            "the same target_root; the resolving_conflict handler would "
            "race on refs/remotes/<remote>/<base> lock files",
        )

    def test_different_target_roots_run_in_parallel(self) -> None:
        # Per-repo locks are keyed on `target_root`. Two specs pointing at
        # DIFFERENT target_roots must NOT serialize against each other --
        # otherwise the multi-repo loop would lose all parallelism.
        import threading
        from unittest.mock import MagicMock

        spec_a = config.RepoSpec(
            slug="acme/one",
            target_root=Path("/tmp/orchestrator-test-target-root-A"),
            base_branch="main",
        )
        spec_b = config.RepoSpec(
            slug="acme/two",
            target_root=Path("/tmp/orchestrator-test-target-root-B"),
            base_branch="main",
        )

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()
        # Block both threads inside `fake_git` simultaneously; if the
        # locks WERE shared across target_roots, one of the threads
        # would queue and the barrier would time out.
        barrier = threading.Barrier(2, timeout=5.0)

        def fake_git(*args, cwd) -> MagicMock:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            barrier.wait()
            with lock:
                in_flight -= 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_authed_fetch(spec, branch) -> MagicMock:
            # `_ensure_worktree` calls the authed fetch first; route it
            # through the same barrier so the in-flight count is built
            # from the fetch in each thread.
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            barrier.wait()
            with lock:
                in_flight -= 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        def fake_has_new_commits(*_a, **_kw) -> bool:
            return False

        with patch.object(worktree_lifecycle, "_git", side_effect=fake_git), \
             patch.object(
                 worktree_lifecycle, "_authed_target_fetch",
                 side_effect=fake_authed_fetch,
             ), \
             patch.object(worktree_lifecycle, "_has_new_commits", fake_has_new_commits), \
             patch.object(Path, "exists", lambda self: False), \
             patch.object(Path, "mkdir", lambda self, **_kw: None):
            t_a = threading.Thread(
                target=lambda: worktrees._ensure_worktree(spec_a, 1)
            )
            t_b = threading.Thread(
                target=lambda: worktrees._ensure_worktree(spec_b, 1)
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=10.0)
            t_b.join(timeout=10.0)
            self.assertFalse(t_a.is_alive())
            self.assertFalse(t_b.is_alive())

        # Both threads cleared the barrier together, so they were
        # genuinely in-flight at the same moment.
        self.assertEqual(max_in_flight, 2)


class EnsureWorktreeRealGitConcurrencyTest(unittest.TestCase):
    """Integration smoke test for the per-target_root lock: drive multiple
    real `_ensure_worktree` calls against a real bare remote concurrently.

    Without the lock, even at 2 workers `git worktree add` would
    intermittently report `error: could not lock config file .git/config:
    File exists` (the reviewer's reproducer). With the lock, every
    worker should succeed and produce its own per-issue worktree
    deterministically.
    """

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, env=env, check=True,
        )
        return r.stdout

    def setUp(self) -> None:
        # Fresh lock dict per test so a leftover entry pointing at a
        # previously-deleted tmp dir cannot satisfy a lookup and
        # accidentally serialize against an unrelated path.
        worktrees._TARGET_ROOT_LOCKS.clear()

        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-ensure-real-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self.work / "README.md").write_text("hello\n")
        self._git("add", ".", cwd=self.work)
        self._git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        self._git("push", "origin", "main", cwd=self.work)

        # Point WORKTREES_DIR at our tmp dir for the duration of the test
        # so `_repo_worktrees_root` creates worktrees here, not in the
        # operator's real worktree dir.
        self._wd_patch = patch.object(
            config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        )
        self._wd_patch.start()
        self.addCleanup(self._wd_patch.stop)

        self.spec = config.RepoSpec(
            slug="acme/widget", target_root=self.work, base_branch="main",
            remote_name="origin",
        )

        # `_authed_target_fetch` dials `https://x-access-token@github.com/...`
        # which has no answer for our local bare remote. Redirect to a
        # plain local fetch so the test still exercises the
        # `_ensure_worktree` worktree-add concurrency path.
        def _local_fetch(spec, branch):
            return subprocess.run(
                ["git", "fetch", "--quiet", spec.remote_name, branch],
                cwd=str(spec.target_root),
                capture_output=True, text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

        self._fetch_patch = patch.object(
            base_sync, "_authed_target_fetch", side_effect=_local_fetch,
        )
        self._fetch_patch.start()
        self.addCleanup(self._fetch_patch.stop)

    def test_concurrent_ensure_worktree_against_same_target_root(self) -> None:
        # Six concurrent workers, each requesting their own per-issue
        # worktree. With the lock in place all six must succeed; without
        # the lock at least one would intermittently surface
        # `error: could not lock config file .git/config: File exists`.
        import threading
        results: list[tuple[int, Optional[Path], Optional[BaseException]]] = []
        results_lock = threading.Lock()

        def call_ensure(n: int) -> None:
            try:
                wt = worktrees._ensure_worktree(self.spec, n)
                with results_lock:
                    results.append((n, wt, None))
            except BaseException as e:  # noqa: BLE001 - record for assertion
                with results_lock:
                    results.append((n, None, e))

        issue_numbers = list(range(1, 7))
        threads = [
            threading.Thread(target=call_ensure, args=(n,))
            for n in issue_numbers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
            self.assertFalse(
                t.is_alive(), "worker timed out (possible lock contention)",
            )

        # No worker raised; every requested worktree path exists on disk.
        errors = [(n, e) for n, _, e in results if e is not None]
        self.assertEqual(
            errors, [],
            f"concurrent _ensure_worktree raised: {errors!r}",
        )
        self.assertEqual(sorted(n for n, _, _ in results), issue_numbers)
        for n, wt, _ in results:
            self.assertIsNotNone(wt)
            self.assertTrue(wt.exists(), f"worktree {wt} missing for issue #{n}")


class BacklogLabelSkipsProcessingTest(unittest.TestCase):
    """The `backlog` control label is a "not yet" hold: applied to an issue
    (typically a freshly opened one), it prevents the orchestrator from
    decomposing, picking up, or otherwise advancing the state machine until
    a human removes the label.
    """

    def test_unlabeled_issue_with_backlog_skips_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701)
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])

    def test_in_flight_issue_with_backlog_skips_dispatch(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(702, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        impl.assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_removing_backlog_allows_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(703)
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_called_once_with(gh, _TEST_SPEC, issue)


class QuestionLabelRoutingTest(unittest.TestCase):
    """`question` is a first-class workflow label routed to its own stage
    handler. The behavioral tests for that handler live in
    `tests/test_workflow_question.py`; this class only covers label
    bootstrapping and dispatcher routing.
    """

    def test_question_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("question", WORKFLOW_LABELS)

    def test_question_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("question", names)

    def test_question_label_is_not_family_aware(self) -> None:
        # Open `question` issues touch only their own pinned state, so the
        # label must stay out of `_FAMILY_AWARE_LABELS` -- otherwise the
        # parallel tick path would route it through the single-threaded
        # family bucket and defeat fan-out concurrency.
        self.assertNotIn("question", workflow._FAMILY_AWARE_LABELS)

    def test_dispatcher_routes_question_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(801, label="question")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_question") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()


class DocumentingLabelRoutingTest(unittest.TestCase):
    """`documenting` is registered as a workflow label so the dispatcher
    routes it to the stub stage handler instead of falling through to
    pickup or implementation. The implementing stage does not auto-apply
    this label yet (parent #149), so any issue carrying it arrived via a
    manual operator action -- the stub parks awaiting human rather than
    silently skipping, otherwise the issue would sit forever waiting for a
    non-existent handler to advance it.
    """

    def test_documenting_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("documenting", WORKFLOW_LABELS)

    def test_documenting_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("documenting", names)

    def test_documenting_label_sits_between_validating_and_in_review(
        self,
    ) -> None:
        # The happy-path lifecycle is implementing -> validating ->
        # documenting (final-docs hop) -> in_review; the spec tuple
        # places the labels in roughly that order so a reader scanning
        # WORKFLOW_LABEL_SPECS top-to-bottom sees the actual flow.
        # Lifecycle routing itself lives in the stage handlers, not
        # this tuple, but the order shouldn't actively mislead.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        impl_idx = names.index("implementing")
        val_idx = names.index("validating")
        doc_idx = names.index("documenting")
        in_review_idx = names.index("in_review")
        self.assertEqual(val_idx, impl_idx + 1)
        self.assertEqual(doc_idx, val_idx + 1)
        self.assertEqual(in_review_idx, doc_idx + 1)

    def test_documenting_label_is_not_family_aware(self) -> None:
        # Open `documenting` issues touch only their own pinned state and
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS`
        # -- otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn("documenting", workflow._FAMILY_AWARE_LABELS)

    def test_documenting_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh. The brief final-
        # docs hop is PR-having (its sibling labels validating /
        # in_review / fixing already qualify), and the documenting
        # handler only checks ahead/behind vs. the PR branch -- not
        # base -- so without the detour a sibling-PR merge during the
        # docs pass would leave the docs commit on a stale base and
        # only the next in_review tick would catch it. Including the
        # label here is what keeps `hold_base_sync` as the only label
        # that gates auto-rebase for a PR-stage worktree.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn("documenting", _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_documenting_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(901, label="documenting")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_documenting") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_validating") as val:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()
        val.assert_not_called()

    def test_documenting_without_pr_number_parks_awaiting_human(self) -> None:
        # End-to-end with the real handler: a manually-applied
        # `documenting` label on an issue with no pinned `pr_number`
        # cannot anchor on a dev PR worktree, so the handler parks
        # awaiting human rather than guessing.
        gh = FakeGitHubClient()
        issue = make_issue(902, label="documenting")
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, 902)
        self.assertIn("documenting", body)
        self.assertTrue(gh.pinned_data(902).get("awaiting_human"))
        # The label is NOT flipped: parking surfaces the situation but
        # leaves the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_documenting_missing_pr_number_is_idempotent_when_parked(
        self,
    ) -> None:
        # A second tick on an already-parked documenting issue (still
        # missing `pr_number`) must not re-post the parking comment or
        # re-emit the audit event -- otherwise every polling tick
        # would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(903, label="documenting")
        gh.add_issue(issue)
        gh.seed_state(903, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)


class FixingLabelRoutingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`fixing` is registered as a workflow label that sits between
    `in_review` and `validating` in the PR-feedback fix loop. The dispatcher
    must route the label to `_handle_fixing` instead of falling through to
    pickup or implementation, and the bootstrap specs / family-aware
    partitioning / closed-issue sweep / PR-worktree refresh detour must
    all recognise it as a PR-having stage. The PR-terminal arcs and the
    no-`pr_number` park covered here pair with the quiet-window / dev-
    resume tests in `tests/test_workflow_fixing.py`.
    """

    def test_fixing_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("fixing", WORKFLOW_LABELS)

    def test_fixing_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("fixing", names)

    def test_fixing_label_sits_between_in_review_and_resolving_conflict(
        self,
    ) -> None:
        # Lifecycle order matters: `fixing` is the next stage after
        # `in_review` when the PR has fresh feedback. The spec tuple
        # encodes the lifecycle ordering, so it must place `fixing` right
        # after `in_review`.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        in_review_idx = names.index("in_review")
        fixing_idx = names.index("fixing")
        self.assertEqual(fixing_idx, in_review_idx + 1)

    def test_fixing_label_is_not_family_aware(self) -> None:
        # Open `fixing` issues touch only their own pinned state and PR
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS` --
        # otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn("fixing", workflow._FAMILY_AWARE_LABELS)

    def test_fixing_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh; a `fixing` worktree
        # is PR-having (its sibling labels validating/in_review already
        # qualify) so it must be eligible for the same detour.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn("fixing", _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_fixing_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701, label="fixing")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_fixing") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_in_review") as in_review:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()
        in_review.assert_not_called()

    def test_fixing_without_pr_number_parks_awaiting_human(self) -> None:
        # A manual relabel directly to `fixing` without a recorded
        # `pr_number` cannot drive the dev-resume path (no PR to push
        # against). Park once, surfacing the misconfiguration to a
        # human; the label is left in place so the operator can fix
        # the relabel.
        gh = FakeGitHubClient()
        issue = make_issue(702, label="fixing")
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, 702)
        self.assertIn("fixing", body)
        self.assertIn("pr_number", body)
        self.assertTrue(gh.pinned_data(702).get("awaiting_human"))
        # The `reason="missing_pr_number"` is recorded on the audit
        # event by `_park_awaiting_human`; the durable `park_reason`
        # field stays None (callers that need a transient/recoverable
        # tag re-set it explicitly -- this park is HITL-only).
        events_for_issue = [
            e for e in gh.recorded_events
            if e.get("issue") == 702
            and e.get("event") == "park_awaiting_human"
        ]
        self.assertEqual(len(events_for_issue), 1)
        self.assertEqual(events_for_issue[0].get("reason"), "missing_pr_number")
        # The label stays put: parking surfaces the situation but leaves
        # the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_fixing_without_pr_number_is_idempotent_when_already_parked(
        self,
    ) -> None:
        # A second tick on an already-parked no-PR fixing issue must
        # not re-post the parking comment -- otherwise every polling
        # tick would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(703, label="fixing")
        gh.add_issue(issue)
        gh.seed_state(703, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)

    def test_fixing_skips_closed_issue_without_pr_number(self) -> None:
        # A closed-`fixing` issue with no recorded PR (manual relabel from
        # an early stage, no PR opened) cannot be finalized via the
        # PR-state arcs. The handler must NOT park (parking a closed issue
        # would spam a parking comment on a terminated thread); it leaves
        # the label alone and lets the operator relabel manually.
        gh = FakeGitHubClient()
        issue = make_issue(704, label="fixing")
        issue.closed = True
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)
        self.assertEqual(gh.label_history, [])

    def test_fixing_finalizes_closed_issue_on_external_merge(self) -> None:
        # The headline closed-sweep contract: a human merges the PR with
        # `Resolves #N` while the issue is labeled `fixing`. The issue
        # auto-closes; the closed-issue sweep yields it; the handler must
        # finalize to `done`, stamp `merged_at`, close (already closed),
        # and run branch cleanup -- otherwise the issue sits closed +
        # `fixing` forever.
        gh = FakeGitHubClient()
        issue = make_issue(705, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=801, head_branch="orchestrator/issue-705",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(705, pr_number=pr.number, branch="orchestrator/issue-705")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((705, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(705))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 705,
        )

    def test_fixing_finalizes_closed_issue_on_closed_without_merge(
        self,
    ) -> None:
        # Mirror branch: PR was closed without merging while the issue
        # was in `fixing`. Handler must flip to `rejected`, stamp
        # `closed_without_merge_at`, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(706, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=802, head_branch="orchestrator/issue-706",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(706, pr_number=pr.number, branch="orchestrator/issue-706")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((706, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(706))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 706,
        )

    def test_closed_fixing_issue_surfaces_in_pollable_sweep(self) -> None:
        # The closed-issue sweep has to include `fixing` so the handler
        # can finalize an externally-merged PR to `done` even when
        # `Resolves #N` already closed the issue.
        gh = FakeGitHubClient()
        open_impl = make_issue(710, label="implementing")
        closed_fixing = make_issue(711, label="fixing")
        closed_fixing.closed = True
        for i in (open_impl, closed_fixing):
            gh.add_issue(i)

        numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(numbers, {710, 711})

    def test_auto_merge_does_not_fire_while_label_is_fixing(self) -> None:
        # Headline merge-safeguard contract: an approved + mergeable PR
        # whose linked issue is labeled `fixing` MUST NOT produce any
        # `gh.merge_pr` call. The orchestrator is permanently manual-
        # merge-only -- no handler calls `merge_pr` today -- but the
        # dispatcher also routes `fixing` to `_handle_fixing` (not
        # `_handle_in_review`), so a regression that smuggled a merge
        # call back into in_review would still not fire here. The
        # `merge_calls == []` assertion below catches either drift.
        gh = FakeGitHubClient()
        issue = make_issue(720, label="fixing")
        gh.add_issue(issue)
        pr = FakePR(
            number=901, head_branch="orchestrator/issue-720",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            720, pr_number=pr.number,
            branch="orchestrator/issue-720",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Pending feedback recorded by the prior in_review tick.
            pending_fix_at="2026-05-23T00:00:00+00:00",
            pending_fix_issue_max_id=2000,
        )

        self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge call, no flip to done -- the dispatcher routed to
        # fixing, so the in_review merge path never ran.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((720, "done"), gh.label_history)


class FixingConflictDetourTest(unittest.TestCase):
    """A behind-base `fixing` worktree is detoured into
    `resolving_conflict` by the pre-tick refresh. The detour must NOT
    swallow pending PR feedback: the `pending_fix_*` bookmarks recorded
    by the in_review handoff and the in_review watermarks MUST survive
    the relabel, so the eventual return from `resolving_conflict` ->
    `validating` -> `in_review` re-discovers the unread feedback and
    routes it back to `fixing`.
    """

    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/refresh-target-fixing"),
            base_branch="main",
        )
        self.wt = Path("/tmp/refresh-wt-fixing")
        self.gh = FakeGitHubClient()

    def _git_result(
        self, *, returncode: int = 0, stdout: str = ""
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr="",
        )

    def test_fixing_detour_preserves_pending_feedback(self) -> None:
        # A `fixing` worktree that is N commits behind `origin/<base>`
        # must flip to `resolving_conflict` and PRESERVE the
        # `pending_fix_*` bookmarks and `pr_last_comment_id` watermark.
        # Any bump of those values here would silently consume the
        # unread feedback that triggered the original in_review ->
        # fixing route: when the resolving_conflict handler eventually
        # pushes the rebase and the validating -> in_review handoff
        # runs, the rescan would skip the (now-watermarked-past) human
        # comment and the in_review HITL ready-ping could advertise
        # the PR as ready for human merge over it.
        self.gh.add_issue(make_issue(7, label="fixing"))
        pr = FakePR(
            number=42, head_branch="orchestrator/issue-7",
            head=FakePRRef(sha="cafe1234"),
            state="open",
        )
        self.gh.add_pr(pr)
        self.gh.seed_state(
            7,
            pr_number=42,
            branch="orchestrator/issue-7",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            pending_fix_at="2026-05-23T00:00:00+00:00",
            pending_fix_issue_max_id=2000,
            pending_fix_review_max_id=3000,
            pending_fix_review_summary_max_id=4000,
        )
        # Behind base by 3 commits drives the detour.
        git_mock = patch.object(
            base_sync, "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             git_mock:
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        # Detour fired: label flipped to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        # Pending-fix bookmarks survived the relabel so the eventual
        # in_review re-entry can correlate the triggering ids.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("pending_fix_at"), "2026-05-23T00:00:00+00:00")
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)
        self.assertEqual(data.get("pending_fix_review_max_id"), 3000)
        self.assertEqual(data.get("pending_fix_review_summary_max_id"), 4000)
        # And the in_review watermark is unchanged -- the rescan after
        # resolving_conflict -> validating -> in_review will surface
        # the original triggering comment as fresh feedback again.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)


class InReviewRoutesFreshFeedbackToFixingTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Fresh actionable PR feedback during `in_review` must hand the issue
    off to `fixing` immediately -- no debounce wait, no dev spawn from the
    in_review handler itself. The pending-fix bookmark recorded in pinned
    state gives the (future) fixing handler a starting point for the
    triggering comment.
    """

    PR_NUMBER = 880
    BRANCH = "orchestrator/issue-880"

    def _seed_in_review_with_pr(self, *, pr=None, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(880, label="in_review")
        gh.add_issue(issue)
        if pr is None:
            pr = FakePR(
                number=self.PR_NUMBER, head_branch=self.BRANCH,
                head=FakePRRef(sha="cafe1234"),
                mergeable=True, check_state="success",
            )
        gh.add_pr(pr)
        state = dict(
            pr_number=pr.number,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(880, **state)
        return gh, issue, pr

    def test_fresh_pr_conversation_comment_flips_to_fixing_no_dev_spawn(
        self,
    ) -> None:
        # The headline contract: a single fresh PR conversation comment
        # within the debounce window must route the issue from `in_review`
        # to `fixing` on this tick. The dev is NOT spawned by
        # `_handle_in_review` any more -- the fixing stage owns that step.
        # Run through the full dispatcher (`_process_issue`) so the test
        # also covers the routing wiring end-to-end.
        now = datetime.now(timezone.utc)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=3000,
                    body="please tighten the integration test",
                    user=FakeUser("alice"),
                    created_at=now,  # well inside the debounce window
                ),
            ],
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev spawn during the debounce window (or after it -- the
        # in_review handler no longer spawns the dev at all).
        mocks["run_agent"].assert_not_called()
        # No merge attempt either: the orchestrator never merges and
        # the fresh feedback routes to fixing.
        self.assertEqual(gh.merge_calls, [])
        # The label flipped to `fixing` this tick.
        self.assertIn((880, "fixing"), gh.label_history)
        # Pending-fix metadata records the triggering comment id and an
        # ISO timestamp so the fixing handler has a bookmark.
        data = gh.pinned_data(880)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 3000)
        self.assertIn("pending_fix_at", data)
        # Watermark stays put so the fixing handler can rescan and reach
        # the triggering comment on its next tick.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)

    def test_no_fresh_feedback_pings_hitl_for_manual_merge(self) -> None:
        # The in_review -> fixing route must NOT preempt the mergeable /
        # HITL-ping path: an approved, mergeable, green PR with no fresh
        # PR comments earns a one-shot HITL ping (the orchestrator never
        # merges) and stays open.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge, no fixing route, no terminal flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((880, "done"), gh.label_history)
        self.assertNotIn((880, "fixing"), gh.label_history)
        self.assertNotIn("pending_fix_at", gh.pinned_data(880))
        # HITL ping fired exactly once.
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(880).get("ready_ping_sha"), "cafe1234",
        )

    def test_no_fresh_feedback_preserves_pr_merged_terminal(self) -> None:
        # Existing terminal PR handling must still finalize the issue to
        # `done` on an external merge -- the fixing route is gated on
        # fresh PR feedback and must not preempt the merged-PR branch.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh, issue, _ = self._seed_in_review_with_pr(pr=pr)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((880, "done"), gh.label_history)
        self.assertNotIn((880, "fixing"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(880))

    def test_fresh_issue_thread_comment_routes_to_fixing_despite_drift_hash(
        self,
    ) -> None:
        # Regression test for the reviewer's reproducer: a normal fresh
        # issue-thread review comment used to trigger user-content drift
        # (because `user_content_hash` covers human issue comments) and
        # the drift path would `_resume_dev_with_text` + flip to
        # `validating` -- violating the contract that any fresh issue-
        # thread feedback during `in_review` records `pending_fix_*` and
        # routes to `fixing`. Seed a stale prior `user_content_hash` so
        # the drift path WOULD fire if the ordering were wrong, then
        # confirm the fresh-feedback scan wins.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh = FakeGitHubClient()
        issue = make_issue(1660, label="in_review")
        # Issue-thread comment posted after the watermark; the hash that
        # was recorded earlier did not include it, so the drift detector
        # WOULD fire on the next tick if the scan order were wrong.
        issue.comments.append(FakeComment(
            id=7000, body="please tighten the docstring",
            user=FakeUser("alice"), created_at=long_ago,
        ))
        gh.add_issue(issue)
        pr = FakePR(
            number=1661, head_branch="orchestrator/issue-1660",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            1660,
            pr_number=pr.number,
            branch="orchestrator/issue-1660",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=6999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Stale hash that doesn't cover the human comment above --
            # the drift path WOULD fire on this tick if the scan order
            # were wrong (this is the reviewer's reproducer).
            user_content_hash="stale-hash-from-before-the-human-comment",
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Contract: no dev spawn, no flip to `validating`.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((1660, "validating"), gh.label_history)
        # The issue routed to `fixing` and recorded the triggering
        # bookmark.
        self.assertIn((1660, "fixing"), gh.label_history)
        data = gh.pinned_data(1660)
        self.assertEqual(data.get("pending_fix_issue_max_id"), 7000)
        self.assertIn("pending_fix_at", data)
        # And the hash was refreshed so the drift path does NOT
        # double-fire on the same comment changes after the fixing
        # handler (or an operator) bounces the issue back to `in_review`.
        self.assertNotEqual(
            data.get("user_content_hash"),
            "stale-hash-from-before-the-human-comment",
        )


class StageEvaluationAnalyticsTest(unittest.TestCase):
    """`_process_issue` times every dispatch and appends a single
    `stage_evaluation` analytics record carrying repo / issue / stage /
    duration_s / result. The record fires on both happy-path and
    exception paths; an unhandled handler exception still propagates so
    the per-issue tick try/except in `workflow.tick` keeps the legacy
    isolation behavior. Backlog-skips are NOT timed -- no handler runs.
    """

    @staticmethod
    def _records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_handler_success_appends_stage_evaluation_record(self) -> None:
        # End-to-end: a labeled issue runs through the dispatcher with
        # the matching handler mocked, and the wrapper writes one
        # `stage_evaluation` line carrying the current label + ok result.
        with tempfile.TemporaryDirectory(prefix="analytics-stageval-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8001, label="implementing")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8001
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["repo"], "geserdugarov/agent-orchestrator")
        self.assertEqual(rec["stage"], "implementing")
        self.assertEqual(rec["result"], "ok")
        self.assertIn("duration_s", rec)
        self.assertGreaterEqual(rec["duration_s"], 0)

    def test_unlabeled_issue_records_stage_evaluation_with_no_stage(
        self,
    ) -> None:
        # The dispatcher routes a label=None issue to `_handle_pickup`;
        # the `stage_evaluation` record drops the optional `stage` field
        # (build_record's documented contract for None values) so the
        # absence of a workflow label is encoded as "no stage" rather
        # than a string sentinel that downstream aggregations would
        # have to special-case.
        with tempfile.TemporaryDirectory(prefix="analytics-pickup-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8002)
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_pickup"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8002
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertNotIn("stage", rec)
        self.assertEqual(rec["result"], "ok")

    def test_handler_exception_records_error_result_and_propagates(
        self,
    ) -> None:
        # The handler raising must NOT suppress the exception: the
        # tick loop's per-issue isolation depends on the dispatcher
        # surfacing failures so they can be logged and the loop
        # continues with the next issue. The record must still land
        # with result=error and the duration captured up to the raise.
        sentinel = RuntimeError("handler blew up")
        with tempfile.TemporaryDirectory(prefix="analytics-err-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8003, label="validating")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(
                     workflow, "_handle_validating", side_effect=sentinel,
                 ):
                with self.assertRaises(RuntimeError) as ctx:
                    workflow._process_issue(gh, _TEST_SPEC, issue)
                self.assertIs(ctx.exception, sentinel)
            records = [
                r for r in self._records(path)
                if r.get("event") == "stage_evaluation"
                and r.get("issue") == 8003
            ]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["stage"], "validating")
        self.assertEqual(rec["result"], "error")
        self.assertIn("duration_s", rec)

    def test_backlog_skip_does_not_record_stage_evaluation(self) -> None:
        # Backlog parks the issue OUTSIDE the state machine before any
        # handler runs; there is nothing to time. The early return must
        # short-circuit before the timing wrapper writes a record so
        # operators do not see a noisy run of zero-duration evaluations
        # for issues that the orchestrator deliberately ignores.
        with tempfile.TemporaryDirectory(prefix="analytics-backlog-") as td:
            path = Path(td) / "analytics.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8004, label="implementing")
            issue.labels.append(FakeLabel(BACKLOG_LABEL))
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path), \
                 patch.object(workflow, "_handle_implementing") as handler:
                workflow._process_issue(gh, _TEST_SPEC, issue)
            handler.assert_not_called()
        self.assertEqual(self._records(path), [])

    def test_disabled_sink_does_not_write_evaluation_record(self) -> None:
        # The off knob is documented as a silent no-op for the analytics
        # sink. `_process_issue` must respect it so an operator who set
        # ANALYTICS_LOG_PATH=off does not see a phantom file appear.
        with tempfile.TemporaryDirectory(prefix="analytics-off-") as td:
            sentinel = Path(td) / "must-not-be-created.jsonl"
            gh = FakeGitHubClient()
            issue = make_issue(8005, label="implementing")
            gh.add_issue(issue)
            with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
                 patch.object(workflow, "_handle_implementing"):
                workflow._process_issue(gh, _TEST_SPEC, issue)
            self.assertFalse(sentinel.exists())
            self.assertEqual(list(Path(td).iterdir()), [])


class StageEnterAnalyticsRecordTest(unittest.TestCase):
    """`set_workflow_label` is the single chokepoint for stage transitions;
    every flip emits both the audit `stage_enter` event (to
    `EVENT_LOG_PATH`) and an analytics-compatible `stage_enter` record
    (to `ANALYTICS_LOG_PATH`). Workflow correctness still keys on pinned
    GitHub state; the analytics record is observability only.
    """

    @staticmethod
    def _records(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_label_transition_writes_analytics_stage_enter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="analytics-stage-enter-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(8101)
                gh.add_issue(issue)
                gh.set_workflow_label(issue, "implementing")
                gh.set_workflow_label(issue, "validating")
            records = self._records(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [r["stage"] for r in records],
            ["implementing", "validating"],
        )
        for r in records:
            self.assertEqual(r["event"], "stage_enter")
            self.assertEqual(r["issue"], 8101)
            self.assertEqual(r["repo"], "geserdugarov/agent-orchestrator")
            datetime.fromisoformat(r["ts"])

    def test_label_cleared_to_none_does_not_emit_record(self) -> None:
        # Mirrors the existing `_emit_stage_enter` no-op for None labels:
        # clearing a label is not a stage and must not produce a phantom
        # `stage_enter` analytics record.
        with tempfile.TemporaryDirectory(prefix="analytics-stage-none-") as td:
            path = Path(td) / "analytics.jsonl"
            with patch.object(analytics, "ANALYTICS_LOG_PATH", path):
                gh = FakeGitHubClient()
                issue = make_issue(8102, label="implementing")
                gh.add_issue(issue)
                gh.set_workflow_label(issue, None)
        self.assertEqual(self._records(path), [])


class FinalizeIfPrMergedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the cross-stage `_finalize_if_pr_merged` helper.

    Stages that previously had no merged-PR check (`_handle_implementing`,
    `_handle_documenting`, `_handle_validating`) plus the umbrella /
    blocked aggregation now call this helper to short-circuit a stale
    in-flight label when the linked PR was merged externally. The helper
    is the single chokepoint, so it carries its own tests in addition to
    the per-handler smoke tests.
    """

    def _state_with_pr_number(self, gh, issue_number, pr_number):
        from orchestrator.github import PinnedState
        gh.seed_state(issue_number, pr_number=pr_number)
        # Mirror what handlers do: read pinned state and hand it to the helper.
        state = PinnedState(comment_id=None, data={"pr_number": pr_number})
        return state

    def test_no_pr_number_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(200, label="validating")
        gh.add_issue(issue)
        from orchestrator.github import PinnedState

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, PinnedState()
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_open_pr_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(201, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=20100, head_branch="orchestrator/issue-201",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 201, 20100)

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_closed_unmerged_pr_returns_false(self) -> None:
        # Closed without merge is `rejected` territory; the helper covers
        # only the merged case so the in_review / fixing / resolving_conflict
        # handlers stay in charge of the rejected arc with their own
        # `closed_without_merge_at` stamp + `pr_closed_without_merge` event.
        gh = FakeGitHubClient()
        issue = make_issue(202, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=20200, head_branch="orchestrator/issue-202",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 202, 20200)

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_merged_pr_finalizes_open_issue(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(203, label="implementing")
        gh.add_issue(issue)
        pr = FakePR(
            number=20300, head_branch="orchestrator/issue-203",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 203, 20300)

        result = self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((203, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 203,
        )
        # An `external`-merge audit event is emitted with the
        # entry-stage label.
        kinds = [e["event"] for e in gh.recorded_events]
        self.assertIn("pr_merged", kinds)
        merged_event = next(
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        )
        self.assertEqual(merged_event.get("merge_method"), "external")
        self.assertEqual(merged_event.get("stage"), "implementing")

    def test_merged_pr_finalizes_closed_issue(self) -> None:
        # An externally-merged PR with `Resolves #N` auto-closes the issue
        # before the orchestrator can react. The helper must still
        # finalize the label (and not attempt to re-close).
        gh = FakeGitHubClient()
        issue = make_issue(204, label="validating")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=20400, head_branch="orchestrator/issue-204",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 204, 20400)

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((204, "done"), gh.label_history)
        self.assertTrue(issue.closed)


class DrainReviewPrTerminalsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the shared `_drain_review_pr_terminals` helper.

    `_handle_in_review`, `_handle_fixing`, and `_handle_resolving_conflict`
    all delegate their terminal arcs (merged PR -> `done`, closed PR ->
    `rejected`, open PR + manually-closed issue -> `rejected` without
    branch cleanup) to this helper. The per-stage handler tests cover the
    integrated behavior; these focused tests pin the helper contract
    (return value, event shape, branch-cleanup semantics, pr=None no-op)
    independently of any stage wiring.
    """

    def _state_with_pr_number(self, gh, issue_number, pr_number, **extra):
        from orchestrator.github import PinnedState

        seed = {"pr_number": pr_number, **extra}
        gh.seed_state(issue_number, **seed)
        return PinnedState(comment_id=None, data=dict(seed))

    def test_pr_none_returns_false_no_op(self) -> None:
        # Fixing's PR-fetch failure path sets `pr=None` and hands it
        # straight to the helper; the helper must treat that as a no-op
        # so the calling handler can fall through to its own fetch-
        # failure deferral (the `if pr is None: return` guard further
        # down the fixing body). No label change, no state writes, no
        # cleanup, no events.
        gh = FakeGitHubClient()
        issue = make_issue(310, label="fixing")
        gh.add_issue(issue)
        state = self._state_with_pr_number(gh, 310, 31000)

        result = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, None, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_open_pr_open_issue_returns_false(self) -> None:
        # The handler-side rescan / debounce / drift logic depends on
        # the helper returning False for a "nothing terminal" state so
        # the caller can continue with the same `pr`.
        gh = FakeGitHubClient()
        issue = make_issue(311, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=31100, head_branch="orchestrator/issue-311",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 311, 31100)

        result = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_merged_pr_finalizes_to_done_with_event_and_cleanup(self) -> None:
        # The merged arc: stamp `merged_at`, flip to `done`, emit
        # `pr_merged` with `merge_method="external"` and the supplied
        # stage, close the issue if still open, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(312, label="fixing")
        gh.add_issue(issue)
        pr = FakePR(
            number=31200, head_branch="orchestrator/issue-312",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(
            gh, 312, 31200, review_round=2, conflict_round=0,
        )

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((312, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 312,
        )
        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        ev = merged_events[0]
        self.assertEqual(ev["stage"], "fixing")
        self.assertEqual(ev["pr_number"], 31200)
        self.assertEqual(ev["merge_method"], "external")
        self.assertEqual(ev["sha"], "cafe1234")
        self.assertEqual(ev["review_round"], 2)

    def test_closed_unmerged_pr_finalizes_to_rejected_with_event_and_cleanup(
        self,
    ) -> None:
        # The closed-PR arc: stamp `closed_without_merge_at`, flip to
        # `rejected`, emit `pr_closed_without_merge` with the supplied
        # stage, close the issue if still open, and run branch cleanup.
        # The branch is dead weight once the PR is gone, mirroring the
        # merged-PR cleanup order.
        gh = FakeGitHubClient()
        issue = make_issue(313, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=31300, head_branch="orchestrator/issue-313",
            head=FakePRRef(sha="dead0001"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(
            gh, 313, 31300, review_round=3, conflict_round=2,
        )

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((313, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 313,
        )
        closed_events = [
            e for e in gh.recorded_events
            if e["event"] == "pr_closed_without_merge"
        ]
        self.assertEqual(len(closed_events), 1)
        ev = closed_events[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        self.assertEqual(ev["pr_number"], 31300)
        self.assertEqual(ev["sha"], "dead0001")
        self.assertEqual(ev["review_round"], 3)
        self.assertEqual(ev["conflict_round"], 2)

    def test_open_pr_with_manually_closed_issue_rejects_without_cleanup(
        self,
    ) -> None:
        # Open PR + manually closed issue is a human stop signal: flip
        # to `rejected` so the in_review HITL ready-ping cannot
        # advertise the PR as ready for human merge over the human
        # rejection, but deliberately leave the branch alone so the
        # operator can salvage / reopen the still-open PR. No event
        # emit either -- `pr_closed_without_merge` is reserved for the
        # genuine closed-PR arc above.
        gh = FakeGitHubClient()
        issue = make_issue(314, label="in_review")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=31400, head_branch="orchestrator/issue-314",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 314, 31400)

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((314, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        # The PR is still open and may be reopened / salvaged, so the
        # branch must survive this exit.
        result["_cleanup_terminal_branch"].assert_not_called()
        # No `pr_closed_without_merge` emit for the open-PR case.
        self.assertEqual(
            [e for e in gh.recorded_events
             if e["event"] == "pr_closed_without_merge"],
            [],
        )
        self.assertEqual(
            [e for e in gh.recorded_events if e["event"] == "pr_merged"],
            [],
        )

    def test_resolving_conflict_terminal_preserves_zero_conflict_round(
        self,
    ) -> None:
        # Legacy / manually-relabelled `resolving_conflict` states may
        # land in the terminal arcs without `conflict_round` ever being
        # seeded (the in_review route normally initializes it to 0
        # before flipping the label). The pre-refactor inline code
        # coerced the value via `int(state.get("conflict_round") or 0)`
        # so the audit record always carried the field. `build_event_record`
        # drops None-valued extras, so the helper must keep that coercion
        # for `stage="resolving_conflict"` -- otherwise legacy states
        # silently lose `conflict_round` from `pr_merged` /
        # `pr_closed_without_merge` events.
        gh = FakeGitHubClient()
        issue = make_issue(316, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=31600, head_branch="orchestrator/issue-316",
            head=FakePRRef(sha="feed1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        # Deliberately omit `conflict_round` from the pinned state.
        state = self._state_with_pr_number(gh, 316, 31600)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        ev = merged_events[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        # Field must be present (build_event_record drops None), and
        # the coerced default must be 0.
        self.assertIn("conflict_round", ev)
        self.assertEqual(ev["conflict_round"], 0)

        # Same coercion for the closed-without-merge arc.
        issue2 = make_issue(317, label="resolving_conflict")
        gh.add_issue(issue2)
        pr2 = FakePR(
            number=31700, head_branch="orchestrator/issue-317",
            head=FakePRRef(sha="feed5678"),
            merged=False, state="closed",
        )
        gh.add_pr(pr2)
        state2 = self._state_with_pr_number(gh, 317, 31700)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue2, state2, pr2,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        closed_events = [
            e for e in gh.recorded_events
            if e["event"] == "pr_closed_without_merge"
        ]
        self.assertEqual(len(closed_events), 1)
        ev2 = closed_events[0]
        self.assertIn("conflict_round", ev2)
        self.assertEqual(ev2["conflict_round"], 0)

    def test_in_review_terminal_omits_missing_conflict_round(self) -> None:
        # The other two stages have always passed the raw
        # `state.get("conflict_round")` through, so a missing counter
        # naturally drops out via `build_event_record`. Pin that contract
        # so a future refactor doesn't accidentally start coercing for
        # `in_review` / `fixing` and start emitting a `conflict_round=0`
        # field on states that never had the counter.
        gh = FakeGitHubClient()
        issue = make_issue(318, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=31800, head_branch="orchestrator/issue-318",
            head=FakePRRef(sha="cafe5678"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 318, 31800)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertNotIn("conflict_round", merged_events[0])

    def test_merged_arc_handles_already_closed_issue_without_re_closing(
        self,
    ) -> None:
        # A `Resolves #N` footer auto-closes the issue the moment the PR
        # merges, so when the closed-issue sweep yields this case the
        # helper sees an already-closed issue. The merged arc still
        # finalizes the label, but must not crash trying to re-close
        # what GitHub already closed.
        gh = FakeGitHubClient()
        issue = make_issue(315, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=31500, head_branch="orchestrator/issue-315",
            head=FakePRRef(sha="feed0001"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 315, 31500)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((315, "done"), gh.label_history)
        self.assertTrue(issue.closed)
        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0]["stage"], "fixing")


class ListPollableIssuesClosedSweepTest(unittest.TestCase):
    """A closed issue stuck at `implementing` / `documenting` / `validating`
    used to be invisible to `list_pollable_issues`. The per-handler
    `_finalize_if_pr_merged` check cannot fire if the sweep does not
    yield the issue, so the sweep was extended alongside the helper.
    """

    def test_closed_implementing_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(301, label="implementing")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [i.number for i in gh.list_pollable_issues()]
        self.assertIn(301, yielded)

    def test_closed_documenting_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(302, label="documenting")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [i.number for i in gh.list_pollable_issues()]
        self.assertIn(302, yielded)

    def test_closed_validating_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(303, label="validating")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [i.number for i in gh.list_pollable_issues()]
        self.assertIn(303, yielded)


if __name__ == "__main__":
    unittest.main()
