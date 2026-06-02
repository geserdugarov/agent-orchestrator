# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class HitlHandleConfigTest(unittest.TestCase):
    def _load_config(self, hitl_handle: str):
        env = {
            "HITL_HANDLE": hitl_handle,
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        with patch.dict(os.environ, env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_formats_comma_separated_handles_as_mentions(self) -> None:
        config = self._load_config("alice,bob")

        self.assertEqual(config.HITL_HANDLES, ("alice", "bob"))
        self.assertEqual(config.HITL_HANDLE, "alice,bob")
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob")

    def test_strips_whitespace_at_signs_and_duplicates(self) -> None:
        config = self._load_config(" @alice, bob, ,alice,@carol ")

        self.assertEqual(config.HITL_HANDLES, ("alice", "bob", "carol"))
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob @carol")

    def test_empty_config_keeps_existing_default(self) -> None:
        config = self._load_config("")

        self.assertEqual(config.HITL_HANDLES, ("geserdugarov",))
        self.assertEqual(config.HITL_MENTIONS, "@geserdugarov")


class AgentGitIdentityConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_defaults_to_orchestrator_identity(self) -> None:
        config = self._load_config()

        self.assertEqual(config.AGENT_GIT_NAME, "agent-orchestrator")
        self.assertEqual(
            config.AGENT_GIT_EMAIL,
            "agent-orchestrator@users.noreply.github.com",
        )

    def test_env_overrides_take_effect(self) -> None:
        config = self._load_config({
            "AGENT_GIT_NAME": "Custom Bot",
            "AGENT_GIT_EMAIL": "bot@example.com",
        })

        self.assertEqual(config.AGENT_GIT_NAME, "Custom Bot")
        self.assertEqual(config.AGENT_GIT_EMAIL, "bot@example.com")


class AgentBackendConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` are validated at import time so a typo
    aborts the process before the polling loop spins up."""

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_defaults_split_claude_dev_codex_review(self) -> None:
        config = self._load_config()
        self.assertEqual(config.DEV_AGENT, "claude")
        self.assertEqual(config.REVIEW_AGENT, "codex")

    def test_env_overrides_invert_split(self) -> None:
        config = self._load_config({
            "DEV_AGENT": "codex",
            "REVIEW_AGENT": "claude",
        })
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(config.REVIEW_AGENT, "claude")

    def test_case_and_whitespace_tolerated(self) -> None:
        config = self._load_config({
            "DEV_AGENT": "  CODEX ",
            "REVIEW_AGENT": "Claude",
        })
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(config.REVIEW_AGENT, "claude")

    def test_invalid_dev_agent_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DEV_AGENT": "gemini"})
        self.assertIn("DEV_AGENT", str(cm.exception))
        self.assertIn("gemini", str(cm.exception))

    def test_invalid_review_agent_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REVIEW_AGENT": "qwen"})
        self.assertIn("REVIEW_AGENT", str(cm.exception))

    def test_default_decompose_agent_is_claude(self) -> None:
        config = self._load_config()
        self.assertEqual(config.DECOMPOSE_AGENT, "claude")

    def test_decompose_agent_env_override(self) -> None:
        config = self._load_config({"DECOMPOSE_AGENT": "codex"})
        self.assertEqual(config.DECOMPOSE_AGENT, "codex")

    def test_invalid_decompose_agent_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DECOMPOSE_AGENT": "gemini"})
        self.assertIn("DECOMPOSE_AGENT", str(cm.exception))

    def test_decompose_agent_validated_even_when_decompose_off(self) -> None:
        # Toggling DECOMPOSE back on later must not surface a fresh
        # "that env var was always invalid" failure.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({
                "DECOMPOSE": "off",
                "DECOMPOSE_AGENT": "gemini",
            })
        self.assertIn("DECOMPOSE_AGENT", str(cm.exception))


class AgentSpecConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` accept shell-like
    command specs: a backend name optionally followed by backend-CLI args
    (`codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`). Bare backend
    names keep working unchanged.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_bare_backend_has_no_extra_args(self) -> None:
        config = self._load_config()
        self.assertEqual(config.DEV_AGENT, "claude")
        self.assertEqual(config.DEV_AGENT_ARGS, ())
        self.assertEqual(config.REVIEW_AGENT, "codex")
        self.assertEqual(config.REVIEW_AGENT_ARGS, ())
        self.assertEqual(config.DECOMPOSE_AGENT, "claude")
        self.assertEqual(config.DECOMPOSE_AGENT_ARGS, ())

    def test_parses_quoted_codex_spec(self) -> None:
        # Exact spec shape from the issue body. shlex must keep the
        # `-c key="value"` token whole even though it contains both
        # quotes and an `=`; if the parser splits on whitespace naively
        # the value half would be dropped.
        config = self._load_config({
            "DEV_AGENT": "codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'",
        })
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(
            config.DEV_AGENT_ARGS,
            ("-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )

    def test_parses_claude_spec_with_flags(self) -> None:
        config = self._load_config({
            "REVIEW_AGENT": "claude --model claude-opus-4-7 --effort high",
        })
        self.assertEqual(config.REVIEW_AGENT, "claude")
        self.assertEqual(
            config.REVIEW_AGENT_ARGS,
            ("--model", "claude-opus-4-7", "--effort", "high"),
        )

    def test_per_role_args_are_independent(self) -> None:
        # Two roles sharing a backend keep distinct args so a deployment
        # can run e.g. `codex -m gpt-5.5` for dev and `codex` for review.
        config = self._load_config({
            "DEV_AGENT": "codex -m gpt-5.5",
            "REVIEW_AGENT": "codex",
            "DECOMPOSE_AGENT": "claude --model claude-opus-4-7",
        })
        self.assertEqual(config.DEV_AGENT_ARGS, ("-m", "gpt-5.5"))
        self.assertEqual(config.REVIEW_AGENT_ARGS, ())
        self.assertEqual(
            config.DECOMPOSE_AGENT_ARGS,
            ("--model", "claude-opus-4-7"),
        )

    def test_first_token_case_normalized(self) -> None:
        # The bare-form parser tolerates ` CODEX `; the spec form should
        # behave identically so legacy values like `DEV_AGENT=Codex` keep
        # parsing the same way after the shell-spec rollout.
        config = self._load_config({"DEV_AGENT": "  CODEX -m foo"})
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(config.DEV_AGENT_ARGS, ("-m", "foo"))

    def test_empty_spec_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DEV_AGENT": "   "})
        msg = str(cm.exception)
        self.assertIn("DEV_AGENT", msg)
        self.assertIn("empty", msg)

    def test_unknown_first_token_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DEV_AGENT": "gemini --model g-1"})
        msg = str(cm.exception)
        self.assertIn("DEV_AGENT", msg)
        self.assertIn("gemini", msg)

    def test_unterminated_quote_aborts_at_import(self) -> None:
        # shlex.split raises ValueError on an unbalanced quote; the
        # importer must surface that as a SystemExit so the orchestrator
        # never starts with an unparseable spec.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DEV_AGENT": "codex -c 'unterminated"})
        self.assertIn("DEV_AGENT", str(cm.exception))


class DotenvQuoteStrippingTest(unittest.TestCase):
    """`_load_dotenv` previously stripped quote chars off both ends of a
    value with `value.strip('"').strip("'")`, which corrupted any value
    whose payload legitimately ended in a quote. The documented
    `DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`
    spec hit exactly that bug -- the trailing `'` got eaten, and
    `_parse_agent_spec` then died on `No closing quotation`.

    The fix is to only strip a single matched outer quote pair, so quoted
    segments inside the value survive verbatim.
    """

    # Keys the dotenv path is allowed to write. Stripped from the patched
    # env before calling `_load_dotenv` so `os.environ.setdefault` in the
    # loader actually writes the temp .env's values (instead of silently
    # no-opping against a real value inherited from the developer's
    # shell or repo .env).
    _DOTENV_OWNED_KEYS = (
        "DEV_AGENT",
        "REVIEW_AGENT",
        "DECOMPOSE_AGENT",
    )

    def _reload_with_dotenv(
        self, dotenv_body: str, *, extra_env: dict[str, str] | None = None
    ):
        """Reload config hermetically against an isolated temp REPO_ROOT
        containing the given `.env` contents.

        Hermeticity matters: the previous version of this helper imported
        `orchestrator.config` with `ORCHESTRATOR_SKIP_DOTENV` unset and
        before patching `REPO_ROOT`, so the import-time `_load_dotenv()`
        ran against the developer's real REPO_ROOT/.env. That had two
        failure modes:
          * `os.environ.setdefault` populated `DEV_AGENT` / `REVIEW_AGENT`
            from the real .env, and the later `_load_dotenv` against
            the tmp dir silently no-op'd on those keys -- the temp
            fixture had no effect.
          * If the real .env carried an invalid value the initial
            import would abort, killing the test for reasons unrelated
            to the fixture under test.

        Fix: import with dotenv skipped, clear the keys the fixture
        owns, then manually run `_load_dotenv()` under the patched
        REPO_ROOT.
        """
        env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if extra_env:
            env.update(extra_env)
        with tempfile.TemporaryDirectory() as td:
            dotenv_path = Path(td) / ".env"
            dotenv_path.write_text(dotenv_body)

            with patch.dict(os.environ, env, clear=True):
                # Initial import is dotenv-skipped, so it cannot read the
                # real REPO_ROOT/.env (or any other host file). Module
                # constants get their default values; the fixture
                # rebinds them below from the temp dotenv.
                sys.modules.pop("orchestrator.config", None)
                import orchestrator.config as config

                # Drop the skip flag and any owned keys we want the
                # tmp .env to populate. `_load_dotenv`'s `setdefault`
                # respects existing values, so anything left set here
                # would prevent the fixture from taking effect.
                os.environ.pop("ORCHESTRATOR_SKIP_DOTENV", None)
                for key in self._DOTENV_OWNED_KEYS:
                    os.environ.pop(key, None)

                with patch.object(config, "REPO_ROOT", Path(td)):
                    config._load_dotenv()

                config.DEV_AGENT, config.DEV_AGENT_ARGS = config._parse_agent_spec(
                    "DEV_AGENT", os.environ.get("DEV_AGENT", "claude")
                )
                config.REVIEW_AGENT, config.REVIEW_AGENT_ARGS = config._parse_agent_spec(
                    "REVIEW_AGENT", os.environ.get("REVIEW_AGENT", "codex")
                )
                return config

    def test_strip_dotenv_quotes_keeps_inner_quote_pairs(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # Inner double-quote pair stays intact; the trailing `'` is the
        # closing half of an outer single-quote pair so it should NOT be
        # eaten by a naive .strip("'").
        raw = "codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"
        self.assertEqual(_strip_dotenv_quotes(raw), raw)

    def test_strip_dotenv_quotes_unwraps_matched_outer_pair(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # Operator-written `KEY="value with spaces"` -- a single matched
        # outer pair IS unwrapped so existing dotenv conventions keep
        # working.
        self.assertEqual(
            _strip_dotenv_quotes('"value with spaces"'),
            "value with spaces",
        )
        self.assertEqual(
            _strip_dotenv_quotes("'single quoted'"),
            "single quoted",
        )

    def test_strip_dotenv_quotes_leaves_mismatched_pair_alone(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # A `"...'` mismatch is more likely a typo than a quoting
        # convention; leaving it intact surfaces the problem at the
        # downstream parser instead of silently corrupting the value.
        self.assertEqual(_strip_dotenv_quotes("\"mismatched'"), "\"mismatched'")

    def test_quoted_codex_spec_round_trips_through_dotenv(self) -> None:
        # The exact spec shape advertised in .env.example.advanced and
        # the issue body must parse cleanly when supplied through .env,
        # not just when injected directly into os.environ.
        body = (
            "DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'\n"
        )
        config = self._reload_with_dotenv(body)
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(
            config.DEV_AGENT_ARGS,
            ("-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )

    def test_outer_double_quoted_dotenv_value_still_unwraps(self) -> None:
        # Backward-compat for operators who wrap their values in outer
        # double quotes (a common dotenv convention).
        body = 'REVIEW_AGENT="claude --model claude-opus-4-7"\n'
        config = self._reload_with_dotenv(body)
        self.assertEqual(config.REVIEW_AGENT, "claude")
        self.assertEqual(
            config.REVIEW_AGENT_ARGS, ("--model", "claude-opus-4-7"),
        )


class DecomposeKillSwitchConfigTest(unittest.TestCase):
    """The DECOMPOSE kill switch defaults on; truthy spellings keep it on,
    explicit off / typos disable it. Mirrors AUTO_MERGE's strict parser
    semantics so a typo doesn't silently flip the user's intent.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_on(self) -> None:
        config = self._load_config()
        self.assertTrue(config.DECOMPOSE)

    def test_explicit_off(self) -> None:
        config = self._load_config({"DECOMPOSE": "off"})
        self.assertFalse(config.DECOMPOSE)

    def test_truthy_spellings_keep_on(self) -> None:
        for value in ("on", "ON", " on ", "1", "true", "True", "yes"):
            with self.subTest(value=value):
                config = self._load_config({"DECOMPOSE": value})
                self.assertTrue(config.DECOMPOSE)

    def test_falsy_spellings_disable(self) -> None:
        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value):
                config = self._load_config({"DECOMPOSE": value})
                self.assertFalse(config.DECOMPOSE)

    def test_typo_defaults_to_off(self) -> None:
        # Strict parser: any unrecognized value disables decomposition.
        config = self._load_config({"DECOMPOSE": "enabled"})
        self.assertFalse(config.DECOMPOSE)


class AutoMergeConfigTest(unittest.TestCase):
    """Default off; only an explicit truthy spelling flips it on. A typo
    silently defaulting to on would let the orchestrator merge against the
    user's intent, so the parser is deliberately strict.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_off(self) -> None:
        config = self._load_config()
        self.assertFalse(config.AUTO_MERGE)

    def test_explicit_off(self) -> None:
        config = self._load_config({"AUTO_MERGE": "off"})
        self.assertFalse(config.AUTO_MERGE)

    def test_truthy_spellings_enable(self) -> None:
        for value in ("on", "ON", " on ", "1", "true", "True", "yes"):
            with self.subTest(value=value):
                config = self._load_config({"AUTO_MERGE": value})
                self.assertTrue(
                    config.AUTO_MERGE, f"{value!r} should enable AUTO_MERGE"
                )

    def test_falsy_spellings_disable(self) -> None:
        for value in ("0", "false", "no", ""):
            with self.subTest(value=value):
                config = self._load_config({"AUTO_MERGE": value})
                self.assertFalse(
                    config.AUTO_MERGE, f"{value!r} should leave AUTO_MERGE off"
                )

    def test_typo_defaults_to_off(self) -> None:
        # The whole point of off-by-default + strict-truthy parsing: a typo
        # cannot silently turn on auto-merge.
        config = self._load_config({"AUTO_MERGE": "enabled"})
        self.assertFalse(config.AUTO_MERGE)


class InReviewDebounceConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_ten_minutes(self) -> None:
        config = self._load_config()
        self.assertEqual(config.IN_REVIEW_DEBOUNCE_SECONDS, 600)

    def test_env_override(self) -> None:
        config = self._load_config({"IN_REVIEW_DEBOUNCE_SECONDS": "120"})
        self.assertEqual(config.IN_REVIEW_DEBOUNCE_SECONDS, 120)


class MaxRetriesPerDayConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_three(self) -> None:
        config = self._load_config()
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 3)

    def test_env_override(self) -> None:
        config = self._load_config({"MAX_RETRIES_PER_DAY": "7"})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 7)

    def test_zero_means_unbounded(self) -> None:
        config = self._load_config({"MAX_RETRIES_PER_DAY": "0"})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 0)


class AllowedIssueAuthorsConfigTest(unittest.TestCase):
    """Author-allowlist for unlabeled-issue pickup. Empty (default) disables
    the filter so existing single-user setups keep working; a populated list
    guards against random users on public repos triggering agent runs."""

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_empty_tuple(self) -> None:
        config = self._load_config()
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, ())

    def test_parses_comma_separated(self) -> None:
        config = self._load_config({"ALLOWED_ISSUE_AUTHORS": "alice,bob"})
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, ("alice", "bob"))

    def test_strips_whitespace_at_signs_and_duplicates(self) -> None:
        config = self._load_config(
            {"ALLOWED_ISSUE_AUTHORS": " @alice, bob, ,alice,@carol "}
        )
        self.assertEqual(
            config.ALLOWED_ISSUE_AUTHORS, ("alice", "bob", "carol")
        )


class MaxConflictRoundsConfigTest(unittest.TestCase):
    """`MAX_CONFLICT_ROUNDS` parses identically to `MAX_REVIEW_ROUNDS`:
    integer, defaults to 3, env override wins.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_three(self) -> None:
        config = self._load_config()
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 3)

    def test_env_override(self) -> None:
        config = self._load_config({"MAX_CONFLICT_ROUNDS": "7"})
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 7)


class MultiRepoConfigTest(unittest.TestCase):
    """`REPOS` parses N entries; when unset the legacy single-repo trio
    (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`) keeps working."""

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_legacy_single_repo_fallback_when_repos_unset(self) -> None:
        config = self._load_config({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        })

        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].slug, "owner/legacy")
        self.assertEqual(specs[0].target_root, Path("/tmp"))
        self.assertEqual(specs[0].base_branch, "trunk")
        # No REMOTE_NAME set -> defaults to 'origin' so existing deployments
        # keep working unchanged.
        self.assertEqual(specs[0].remote_name, "origin")

    def test_remote_name_env_override_for_single_repo(self) -> None:
        # Multi-remote local clones (e.g. public `origin` + private fork
        # `private`) need to drive the non-default remote.
        config = self._load_config({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "main",
            "REMOTE_NAME": "private",
        })
        specs = config.default_repo_specs()
        self.assertEqual(specs[0].remote_name, "private")

    def test_multi_entry_parsing_newline_and_semicolon(self) -> None:
        # Mix newlines, ';', blank lines, and a comment to verify the parser
        # accepts both separators and ignores noise.
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / "other"
            other.mkdir()
            config = self._load_config({
                "REPOS": (
                    "# multi-repo example\n"
                    f"alpha/one|{td}|main\n"
                    "\n"
                    f"beta/two|{other}|develop;gamma/three|{td}|master"
                ),
            })

            specs = config.default_repo_specs()
            self.assertEqual([s.slug for s in specs],
                             ["alpha/one", "beta/two", "gamma/three"])
            self.assertEqual([s.base_branch for s in specs],
                             ["main", "develop", "master"])
            self.assertEqual(specs[1].target_root, other)
            # Backward-compatible: three-field entries default remote_name
            # to 'origin' so existing REPOS configs keep working.
            for spec in specs:
                self.assertEqual(spec.remote_name, "origin")
            # Returned list is a fresh copy so callers can't mutate the cache.
            specs.append("not-a-spec")  # type: ignore[arg-type]
            self.assertEqual(len(config.default_repo_specs()), 3)

    def test_optional_fourth_field_sets_remote_name(self) -> None:
        # Multi-remote target clones (e.g. public `origin` + private fork
        # `private`) need to drive the non-default remote.
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "REPOS": (
                    f"alpha/one|{td}|main|origin\n"
                    f"beta/two|{td}|main|private"
                ),
            })
            specs = config.default_repo_specs()
            self.assertEqual(
                [(s.slug, s.remote_name) for s in specs],
                [("alpha/one", "origin"), ("beta/two", "private")],
            )

    def test_empty_remote_name_aborts_at_import(self) -> None:
        # An explicit empty fourth field is a misconfiguration -- omit the
        # trailing '|' to get the default. Surface the mistake at startup.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({"REPOS": f"alpha/one|{td}|main|"})
            self.assertIn("remote_name", str(cm.exception))

    def test_too_many_pipe_segments_aborts_at_import(self) -> None:
        # Six fields is malformed -- five (with the optional remote_name and
        # parallel_limit) is the upper bound. Prevents a silent typo like
        # `owner/repo|/path|main|origin|3|extra` from being misinterpreted.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config(
                    {"REPOS": f"alpha/one|{td}|main|origin|3|extra"}
                )
            self.assertIn("malformed", str(cm.exception))

    def test_repos_overrides_legacy_trio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "REPO": "ignored/legacy",
                "TARGET_REPO_ROOT": "/nonexistent",
                "BASE_BRANCH": "ignored",
                "REPOS": f"alpha/one|{td}|main",
            })

            specs = config.default_repo_specs()
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].slug, "alpha/one")
            self.assertEqual(specs[0].target_root, Path(td))
            self.assertEqual(specs[0].base_branch, "main")

    def test_duplicate_slug_aborts_at_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({
                    "REPOS": (
                        f"alpha/one|{td}|main\n"
                        f"alpha/one|{td}|develop"
                    ),
                })
            msg = str(cm.exception)
            self.assertIn("duplicate slug", msg)
            self.assertIn("alpha/one", msg)

    def test_malformed_entry_aborts_at_import(self) -> None:
        # Wrong number of '|' segments.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo|/tmp"})
        self.assertIn("malformed", str(cm.exception))

    def test_empty_slug_aborts_at_import(self) -> None:
        # Slug must contain '/'.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "no-slash|/tmp|main"})
        self.assertIn("owner/name", str(cm.exception))

    def test_slug_with_empty_component_aborts_at_import(self) -> None:
        # `owner//repo` and `/repo` and `owner/` are all malformed even
        # though they contain `/`; require exactly two non-empty components.
        for bad in ("owner//repo", "/repo", "owner/", "//"):
            with self.subTest(slug=bad):
                with self.assertRaises(SystemExit) as cm:
                    self._load_config({"REPOS": f"{bad}|/tmp|main"})
                self.assertIn("owner/name", str(cm.exception))

    def test_slug_with_extra_path_segment_aborts_at_import(self) -> None:
        # `owner/repo/extra` looks plausible but PyGithub treats the slug
        # as the full repo identifier, so any extra `/` would resolve to
        # a wrong (or nonexistent) repo at runtime. Reject at import.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo/extra|/tmp|main"})
        self.assertIn("owner/name", str(cm.exception))

    def test_empty_base_branch_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo|/tmp|"})
        self.assertIn("base_branch", str(cm.exception))

    def test_empty_target_root_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo||main"})
        self.assertIn("target_root", str(cm.exception))

    def test_repos_with_only_comments_aborts(self) -> None:
        # `REPOS` set but yielding zero entries is a misconfiguration --
        # better to fail loudly than silently fall back to the legacy trio
        # (which the user explicitly opted out of by setting REPOS).
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "# just a comment\n  \n"})
        self.assertIn("no valid entries", str(cm.exception))

    def test_missing_target_root_warns_but_does_not_abort(self) -> None:
        # Captures the stderr warning to confirm "warn loudly" semantics.
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            config = self._load_config(
                {"REPOS": "alpha/one|/this/path/does/not/exist|main"}
            )
        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertIn("does not exist", buf.getvalue())
        self.assertIn("alpha/one", buf.getvalue())


class ParallelLimitsConfigTest(unittest.TestCase):
    """Per-repo and global parallel issue-processing caps. Defaults preserve
    legacy single-issue-per-repo behavior (per-repo=1) while bounding total
    spawn fan-out across all configured repos (global=3). Each `REPOS` entry
    can override its per-repo limit via the optional fifth pipe field.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_defaults_one_per_repo_three_global(self) -> None:
        config = self._load_config()
        self.assertEqual(config.MAX_PARALLEL_ISSUES_PER_REPO, 1)
        self.assertEqual(config.MAX_PARALLEL_ISSUES_GLOBAL, 3)

    def test_env_overrides_take_effect(self) -> None:
        config = self._load_config({
            "MAX_PARALLEL_ISSUES_PER_REPO": "2",
            "MAX_PARALLEL_ISSUES_GLOBAL": "10",
        })
        self.assertEqual(config.MAX_PARALLEL_ISSUES_PER_REPO, 2)
        self.assertEqual(config.MAX_PARALLEL_ISSUES_GLOBAL, 10)

    def test_legacy_single_repo_inherits_default_per_repo_limit(self) -> None:
        # When REPOS is unset, the legacy single-repo RepoSpec must adopt
        # whatever MAX_PARALLEL_ISSUES_PER_REPO is set to (default 1).
        config = self._load_config()
        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].parallel_limit, 1)

    def test_legacy_single_repo_picks_up_env_override(self) -> None:
        config = self._load_config({"MAX_PARALLEL_ISSUES_PER_REPO": "4"})
        specs = config.default_repo_specs()
        self.assertEqual(specs[0].parallel_limit, 4)

    def test_three_field_entries_inherit_env_default(self) -> None:
        # Backward-compat: existing three-field REPOS configs inherit the
        # MAX_PARALLEL_ISSUES_PER_REPO env default (or 1 if unset).
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "MAX_PARALLEL_ISSUES_PER_REPO": "2",
                "REPOS": f"alpha/one|{td}|main",
            })
            specs = config.default_repo_specs()
            self.assertEqual(specs[0].parallel_limit, 2)

    def test_four_field_entries_inherit_env_default(self) -> None:
        # The existing four-field (with remote_name) shape stays backward-
        # compatible: parallel_limit falls back to the env default.
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "MAX_PARALLEL_ISSUES_PER_REPO": "5",
                "REPOS": f"alpha/one|{td}|main|private",
            })
            specs = config.default_repo_specs()
            self.assertEqual(specs[0].remote_name, "private")
            self.assertEqual(specs[0].parallel_limit, 5)

    def test_fifth_field_overrides_per_repo_limit(self) -> None:
        # Per-entry override takes precedence over the global env default,
        # so a busy repo can run more issues in parallel than its peers.
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "MAX_PARALLEL_ISSUES_PER_REPO": "1",
                "REPOS": (
                    f"alpha/one|{td}|main|origin|3\n"
                    f"beta/two|{td}|main|origin|7"
                ),
            })
            specs = config.default_repo_specs()
            self.assertEqual(
                [(s.slug, s.parallel_limit) for s in specs],
                [("alpha/one", 3), ("beta/two", 7)],
            )

    def test_mixed_entries_three_four_five_fields(self) -> None:
        # All three legacy field counts coexist; only the five-field entry
        # overrides the per-repo default.
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "MAX_PARALLEL_ISSUES_PER_REPO": "2",
                "REPOS": (
                    f"alpha/one|{td}|main\n"
                    f"beta/two|{td}|main|private\n"
                    f"gamma/three|{td}|main|origin|6"
                ),
            })
            specs = config.default_repo_specs()
            self.assertEqual(
                [(s.slug, s.remote_name, s.parallel_limit) for s in specs],
                [
                    ("alpha/one", "origin", 2),
                    ("beta/two", "private", 2),
                    ("gamma/three", "origin", 6),
                ],
            )

    def test_non_numeric_per_repo_env_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"MAX_PARALLEL_ISSUES_PER_REPO": "lots"})
        msg = str(cm.exception)
        self.assertIn("MAX_PARALLEL_ISSUES_PER_REPO", msg)
        self.assertIn("lots", msg)

    def test_zero_per_repo_env_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"MAX_PARALLEL_ISSUES_PER_REPO": "0"})
        self.assertIn("MAX_PARALLEL_ISSUES_PER_REPO", str(cm.exception))

    def test_negative_per_repo_env_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"MAX_PARALLEL_ISSUES_PER_REPO": "-1"})
        self.assertIn("MAX_PARALLEL_ISSUES_PER_REPO", str(cm.exception))

    def test_non_numeric_global_env_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"MAX_PARALLEL_ISSUES_GLOBAL": "many"})
        msg = str(cm.exception)
        self.assertIn("MAX_PARALLEL_ISSUES_GLOBAL", msg)

    def test_zero_global_env_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"MAX_PARALLEL_ISSUES_GLOBAL": "0"})
        self.assertIn("MAX_PARALLEL_ISSUES_GLOBAL", str(cm.exception))

    def test_malformed_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({
                    "REPOS": f"alpha/one|{td}|main|origin|seven",
                })
            msg = str(cm.exception)
            self.assertIn("parallel_limit", msg)
            self.assertIn("seven", msg)

    def test_zero_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({
                    "REPOS": f"alpha/one|{td}|main|origin|0",
                })
            self.assertIn("parallel_limit", str(cm.exception))
            self.assertIn(">= 1", str(cm.exception))

    def test_negative_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({
                    "REPOS": f"alpha/one|{td}|main|origin|-2",
                })
            self.assertIn("parallel_limit", str(cm.exception))

    def test_empty_parallel_limit_field_aborts(self) -> None:
        # An explicit empty fifth field is a misconfiguration -- omit the
        # trailing '|' to get the default. Surface the mistake at startup.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({"REPOS": f"alpha/one|{td}|main|origin|"})
            self.assertIn("parallel_limit", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
