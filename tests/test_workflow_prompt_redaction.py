# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Prompt builder for the documentation stage and the shared stderr/log
secret-redaction helpers. The prompt teaches the agent the contract the
verdict parser enforces; the redactor scrubs provider tokens before any
agent output surfaces in comments, log tails, or analytics records."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.agents import AgentResult

from tests.fakes import make_issue
from tests.workflow_helpers import _TEST_SPEC


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
