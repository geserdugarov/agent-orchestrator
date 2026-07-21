# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_CONFIG_MODULE = "orchestrator.config"
_SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"
_TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"
_MISSING_TOKEN_PATH = "/tmp/agent-orchestrator-token-missing"
_ENABLED_ENV = "1"
_DISABLED_ENV = "0"
_ALICE = "alice"
_BOB = "bob"
_CLAUDE = "claude"
_CODEX = "codex"
_INVALID_AGENT = "gemini"
_DEV_AGENT_ENV = "DEV_AGENT"
_REVIEW_AGENT_ENV = "REVIEW_AGENT"
_DECOMPOSE_AGENT_ENV = "DECOMPOSE_AGENT"
_DECOMPOSE_ENV = "DECOMPOSE"
_EXPOSE_REPOS_ENV = "EXPOSE_TRACKED_REPOS"
_OFF = "off"
_MODEL_FLAG = "-m"
_REPOS_ENV = "REPOS"
_LEGACY_REPO = "owner/legacy"
_LEGACY_ROOT = "/tmp"
_LEGACY_BRANCH = "trunk"
_ALPHA_REPO = "alpha/one"
_BETA_REPO = "beta/two"
_ORIGIN_REMOTE = "origin"
_PRIVATE_REMOTE = "private"
_PER_REPO_LIMIT_ENV = "MAX_PARALLEL_ISSUES_PER_REPO"
_GLOBAL_LIMIT_ENV = "MAX_PARALLEL_ISSUES_GLOBAL"
_PARALLEL_LIMIT_FIELD = "parallel_limit"
_DEFAULT_DEBOUNCE_SECONDS = 600
_OVERRIDE_DEBOUNCE_SECONDS = 120
_DOTENV_OWNED_KEYS = (
    _DEV_AGENT_ENV,
    _REVIEW_AGENT_ENV,
    _DECOMPOSE_AGENT_ENV,
)


def _load_config(env: dict[str, str] | None = None):
    """Reload `orchestrator.config` with `env` layered over a minimal base
    (dotenv skipped, token file absent) so module-level parsing sees the test
    values. The dotted `import orchestrator.config as config` after popping the
    cached module is required: `from orchestrator import config` would rebind
    the stale package attribute and skip the reload.
    """
    full_env = {
        _SKIP_DOTENV_ENV: _ENABLED_ENV,
        _TOKEN_FILE_ENV: _MISSING_TOKEN_PATH,
    }
    if env:
        full_env.update(env)
    with patch.dict(os.environ, full_env, clear=True):
        sys.modules.pop(_CONFIG_MODULE, None)
        import orchestrator.config as config

        return config


def _config_error_message(env: dict[str, str]) -> str:
    try:
        _load_config(env)
    except SystemExit as error:
        return str(error)
    raise AssertionError("configuration import did not fail")


def _only_repo_spec(specs):
    if len(specs) != 1:
        raise AssertionError(f"expected one repo spec, got {len(specs)}")
    return specs[0]


class HitlHandleConfigTest(unittest.TestCase):
    def test_formats_comma_handles_as_mentions(self) -> None:
        config = _load_config({"HITL_HANDLE": "alice,bob"})

        self.assertEqual(config.HITL_HANDLES, (_ALICE, _BOB))
        self.assertEqual(config.HITL_HANDLE, "alice,bob")
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob")

    def test_strips_spaces_at_signs_and_duplicates(self) -> None:
        config = _load_config({"HITL_HANDLE": " @alice, bob, ,alice,@carol "})

        self.assertEqual(config.HITL_HANDLES, (_ALICE, _BOB, "carol"))
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob @carol")

    def test_empty_config_keeps_existing_default(self) -> None:
        config = _load_config({"HITL_HANDLE": ""})

        self.assertEqual(config.HITL_HANDLES, ("geserdugarov",))
        self.assertEqual(config.HITL_MENTIONS, "@geserdugarov")


class AgentGitIdentityConfigTest(unittest.TestCase):
    def test_defaults_to_orchestrator_identity(self) -> None:
        config = _load_config()

        self.assertEqual(config.AGENT_GIT_NAME, "agent-orchestrator")
        self.assertEqual(
            config.AGENT_GIT_EMAIL,
            "agent-orchestrator@users.noreply.github.com",
        )

    def test_env_overrides_take_effect(self) -> None:
        config = _load_config({
            "AGENT_GIT_NAME": "Custom Bot",
            "AGENT_GIT_EMAIL": "bot@example.com",
        })

        self.assertEqual(config.AGENT_GIT_NAME, "Custom Bot")
        self.assertEqual(config.AGENT_GIT_EMAIL, "bot@example.com")


class AgentBackendConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` are validated at import time so a typo
    aborts the process before the polling loop spins up."""

    def test_defaults_split_claude_dev_codex_review(self) -> None:
        config = _load_config()
        self.assertEqual(config.DEV_AGENT, _CLAUDE)
        self.assertEqual(config.REVIEW_AGENT, _CODEX)

    def test_env_overrides_invert_split(self) -> None:
        config = _load_config({
            _DEV_AGENT_ENV: _CODEX,
            _REVIEW_AGENT_ENV: _CLAUDE,
        })
        self.assertEqual(config.DEV_AGENT, _CODEX)
        self.assertEqual(config.REVIEW_AGENT, _CLAUDE)

    def test_case_and_whitespace_tolerated(self) -> None:
        config = _load_config({
            _DEV_AGENT_ENV: "  CODEX ",
            _REVIEW_AGENT_ENV: "Claude",
        })
        self.assertEqual(config.DEV_AGENT, _CODEX)
        self.assertEqual(config.REVIEW_AGENT, _CLAUDE)

    def test_invalid_dev_agent_aborts_at_import(self) -> None:
        error_message = _config_error_message({_DEV_AGENT_ENV: _INVALID_AGENT})
        self.assertIn(_DEV_AGENT_ENV, error_message)
        self.assertIn(_INVALID_AGENT, error_message)

    def test_invalid_review_agent_aborts_at_import(self) -> None:
        error_message = _config_error_message({_REVIEW_AGENT_ENV: "qwen"})
        self.assertIn(_REVIEW_AGENT_ENV, error_message)

    def test_default_decompose_agent_is_claude(self) -> None:
        config = _load_config()
        self.assertEqual(config.DECOMPOSE_AGENT, _CLAUDE)

    def test_decompose_agent_env_override(self) -> None:
        config = _load_config({_DECOMPOSE_AGENT_ENV: _CODEX})
        self.assertEqual(config.DECOMPOSE_AGENT, _CODEX)

    def test_invalid_decompose_agent_aborts_at_import(self) -> None:
        error_message = _config_error_message({
            _DECOMPOSE_AGENT_ENV: _INVALID_AGENT,
        })
        self.assertIn(_DECOMPOSE_AGENT_ENV, error_message)

    def test_decomposer_validated_when_feature_off(self) -> None:
        # Toggling DECOMPOSE back on later must not surface a fresh
        # "that env var was always invalid" failure.
        error_message = _config_error_message({
            _DECOMPOSE_ENV: _OFF,
            _DECOMPOSE_AGENT_ENV: _INVALID_AGENT,
        })
        self.assertIn(_DECOMPOSE_AGENT_ENV, error_message)


class AgentSpecConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` accept shell-like
    command specs: a backend name optionally followed by backend-CLI args
    (`codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`). Bare backend
    names keep working unchanged.
    """

    def test_bare_backend_has_no_extra_args(self) -> None:
        config = _load_config()
        self.assertEqual(config.DEV_AGENT, _CLAUDE)
        self.assertEqual(config.DEV_AGENT_ARGS, ())
        self.assertEqual(config.REVIEW_AGENT, _CODEX)
        self.assertEqual(config.REVIEW_AGENT_ARGS, ())
        self.assertEqual(config.DECOMPOSE_AGENT, _CLAUDE)
        self.assertEqual(config.DECOMPOSE_AGENT_ARGS, ())

    def test_parses_quoted_codex_spec(self) -> None:
        # Exact spec shape from the issue body. shlex must keep the
        # `-c key="value"` token whole even though it contains both
        # quotes and an `=`; if the parser splits on whitespace naively
        # the value half would be dropped.
        config = _load_config({
            _DEV_AGENT_ENV: (
                "codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"
            ),
        })
        self.assertEqual(config.DEV_AGENT, _CODEX)
        self.assertEqual(
            config.DEV_AGENT_ARGS,
            (_MODEL_FLAG, "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )

    def test_parses_claude_spec_with_flags(self) -> None:
        config = _load_config({
            _REVIEW_AGENT_ENV: "claude --model claude-opus-4-7 --effort high",
        })
        self.assertEqual(config.REVIEW_AGENT, _CLAUDE)
        self.assertEqual(
            config.REVIEW_AGENT_ARGS,
            ("--model", "claude-opus-4-7", "--effort", "high"),
        )

    def test_per_role_args_are_independent(self) -> None:
        # Two roles sharing a backend keep distinct args so a deployment
        # can run e.g. `codex -m gpt-5.5` for dev and `codex` for review.
        config = _load_config({
            _DEV_AGENT_ENV: "codex -m gpt-5.5",
            _REVIEW_AGENT_ENV: _CODEX,
            _DECOMPOSE_AGENT_ENV: "claude --model claude-opus-4-7",
        })
        self.assertEqual(config.DEV_AGENT_ARGS, (_MODEL_FLAG, "gpt-5.5"))
        self.assertEqual(config.REVIEW_AGENT_ARGS, ())
        self.assertEqual(
            config.DECOMPOSE_AGENT_ARGS,
            ("--model", "claude-opus-4-7"),
        )

    def test_first_token_case_normalized(self) -> None:
        # The bare-form parser tolerates ` CODEX `; the spec form should
        # behave identically so legacy values like `DEV_AGENT=Codex` keep
        # parsing the same way after the shell-spec rollout.
        config = _load_config({_DEV_AGENT_ENV: "  CODEX -m foo"})
        self.assertEqual(config.DEV_AGENT, _CODEX)
        self.assertEqual(config.DEV_AGENT_ARGS, (_MODEL_FLAG, "foo"))

    def test_empty_spec_aborts_at_import(self) -> None:
        error_message = _config_error_message({_DEV_AGENT_ENV: "   "})
        self.assertIn(_DEV_AGENT_ENV, error_message)
        self.assertIn("empty", error_message)

    def test_unknown_first_token_aborts_at_import(self) -> None:
        error_message = _config_error_message({
            _DEV_AGENT_ENV: "gemini --model g-1",
        })
        self.assertIn(_DEV_AGENT_ENV, error_message)
        self.assertIn(_INVALID_AGENT, error_message)

    def test_unterminated_quote_aborts_at_import(self) -> None:
        # shlex.split raises ValueError on an unbalanced quote; the
        # importer must surface that as a SystemExit so the orchestrator
        # never starts with an unparseable spec.
        error_message = _config_error_message({
            _DEV_AGENT_ENV: "codex -c 'unterminated",
        })
        self.assertIn(_DEV_AGENT_ENV, error_message)


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
            _SKIP_DOTENV_ENV: _ENABLED_ENV,
            _TOKEN_FILE_ENV: _MISSING_TOKEN_PATH,
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
                sys.modules.pop(_CONFIG_MODULE, None)
                import orchestrator.config as config

                # Drop the skip flag and any owned keys we want the
                # tmp .env to populate. `_load_dotenv`'s `setdefault`
                # respects existing values, so anything left set here
                # would prevent the fixture from taking effect.
                os.environ.pop(_SKIP_DOTENV_ENV, None)
                for key in _DOTENV_OWNED_KEYS:
                    os.environ.pop(key, None)

                with patch.object(config, "REPO_ROOT", Path(td)):
                    config._load_dotenv()

                config.DEV_AGENT, config.DEV_AGENT_ARGS = config._parse_agent_spec(
                    _DEV_AGENT_ENV,
                    os.environ.get(_DEV_AGENT_ENV, _CLAUDE),
                )
                config.REVIEW_AGENT, config.REVIEW_AGENT_ARGS = config._parse_agent_spec(
                    _REVIEW_AGENT_ENV,
                    os.environ.get(_REVIEW_AGENT_ENV, _CODEX),
                )
                return config

    def test_keeps_inner_quote_pairs(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # Inner double-quote pair stays intact; the trailing `'` is the
        # closing half of an outer single-quote pair so it should NOT be
        # eaten by a naive .strip("'").
        raw = "codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"
        self.assertEqual(_strip_dotenv_quotes(raw), raw)

    def test_unwraps_matched_outer_pair(self) -> None:
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

    def test_keeps_mismatched_outer_pair(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # A `"...'` mismatch is more likely a typo than a quoting
        # convention; leaving it intact surfaces the problem at the
        # downstream parser instead of silently corrupting the value.
        self.assertEqual(_strip_dotenv_quotes("\"mismatched'"), "\"mismatched'")

    def test_quoted_codex_spec_round_trips(self) -> None:
        # The exact spec shape advertised in .env.example.advanced and
        # the issue body must parse cleanly when supplied through .env,
        # not just when injected directly into os.environ.
        body = (
            "DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'\n"
        )
        config = self._reload_with_dotenv(body)
        self.assertEqual(config.DEV_AGENT, _CODEX)
        self.assertEqual(
            config.DEV_AGENT_ARGS,
            (_MODEL_FLAG, "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )

    def test_outer_double_quoted_value_unwraps(self) -> None:
        # Backward-compat for operators who wrap their values in outer
        # double quotes (a common dotenv convention).
        body = 'REVIEW_AGENT="claude --model claude-opus-4-7"\n'
        config = self._reload_with_dotenv(body)
        self.assertEqual(config.REVIEW_AGENT, _CLAUDE)
        self.assertEqual(
            config.REVIEW_AGENT_ARGS, ("--model", "claude-opus-4-7"),
        )


class DecomposeKillSwitchConfigTest(unittest.TestCase):
    """The DECOMPOSE kill switch defaults on; truthy spellings keep it on,
    explicit off / typos disable it. Strict parser semantics so a typo
    doesn't silently flip the user's intent.
    """

    def test_default_is_on(self) -> None:
        config = _load_config()
        self.assertTrue(config.DECOMPOSE)

    def test_explicit_off(self) -> None:
        config = _load_config({_DECOMPOSE_ENV: _OFF})
        self.assertFalse(config.DECOMPOSE)

    def test_truthy_spellings_keep_on(self) -> None:
        for spelling in (
            "on", "ON", " on ", _ENABLED_ENV, "true", "True", "yes",
        ):
            with self.subTest(value=spelling):
                config = _load_config({_DECOMPOSE_ENV: spelling})
                self.assertTrue(config.DECOMPOSE)

    def test_falsy_spellings_disable(self) -> None:
        for spelling in (_DISABLED_ENV, "false", "no", _OFF):
            with self.subTest(value=spelling):
                config = _load_config({_DECOMPOSE_ENV: spelling})
                self.assertFalse(config.DECOMPOSE)

    def test_typo_defaults_to_off(self) -> None:
        # Strict parser: any unrecognized value disables decomposition.
        config = _load_config({_DECOMPOSE_ENV: "enabled"})
        self.assertFalse(config.DECOMPOSE)


class ExposeTrackedReposConfigTest(unittest.TestCase):
    """The EXPOSE_TRACKED_REPOS kill switch defaults on (but is inert for
    single-repo hosts, where the context builder gates on `len(specs) > 1`).
    Parsed exactly like DECOMPOSE / SQUASH_ON_APPROVAL: truthy spellings keep
    it on, explicit off / typos disable it.
    """

    def test_default_is_on(self) -> None:
        config = _load_config()
        self.assertTrue(config.EXPOSE_TRACKED_REPOS)

    def test_explicit_off(self) -> None:
        config = _load_config({_EXPOSE_REPOS_ENV: _OFF})
        self.assertFalse(config.EXPOSE_TRACKED_REPOS)

    def test_truthy_spellings_keep_on(self) -> None:
        for spelling in (
            "on", "ON", " on ", _ENABLED_ENV, "true", "True", "yes",
        ):
            with self.subTest(value=spelling):
                config = _load_config({_EXPOSE_REPOS_ENV: spelling})
                self.assertTrue(config.EXPOSE_TRACKED_REPOS)

    def test_falsy_spellings_disable(self) -> None:
        for spelling in (_DISABLED_ENV, "false", "no", _OFF):
            with self.subTest(value=spelling):
                config = _load_config({_EXPOSE_REPOS_ENV: spelling})
                self.assertFalse(config.EXPOSE_TRACKED_REPOS)

    def test_typo_defaults_to_off(self) -> None:
        # Strict parser: any unrecognized value disables the disclosure.
        config = _load_config({_EXPOSE_REPOS_ENV: "enabled"})
        self.assertFalse(config.EXPOSE_TRACKED_REPOS)


class InReviewDebounceConfigTest(unittest.TestCase):
    def test_default_is_ten_minutes(self) -> None:
        config = _load_config()
        self.assertEqual(
            config.IN_REVIEW_DEBOUNCE_SECONDS,
            _DEFAULT_DEBOUNCE_SECONDS,
        )

    def test_env_override(self) -> None:
        config = _load_config({
            "IN_REVIEW_DEBOUNCE_SECONDS": str(_OVERRIDE_DEBOUNCE_SECONDS),
        })
        self.assertEqual(
            config.IN_REVIEW_DEBOUNCE_SECONDS,
            _OVERRIDE_DEBOUNCE_SECONDS,
        )


class MaxRetriesPerDayConfigTest(unittest.TestCase):
    def test_default_is_three(self) -> None:
        config = _load_config()
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 3)

    def test_env_override(self) -> None:
        config = _load_config({"MAX_RETRIES_PER_DAY": "7"})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 7)

    def test_zero_means_unbounded(self) -> None:
        config = _load_config({"MAX_RETRIES_PER_DAY": _DISABLED_ENV})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 0)


class AllowedIssueAuthorsConfigTest(unittest.TestCase):
    """Author-allowlist for unlabeled-issue pickup. Empty (default) disables
    the filter so existing single-user setups keep working; a populated list
    guards against random users on public repos triggering agent runs."""

    def test_default_is_empty_tuple(self) -> None:
        config = _load_config()
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, ())

    def test_parses_comma_separated(self) -> None:
        config = _load_config({"ALLOWED_ISSUE_AUTHORS": "alice,bob"})
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, (_ALICE, _BOB))

    def test_strips_spaces_at_signs_and_duplicates(self) -> None:
        config = _load_config(
            {"ALLOWED_ISSUE_AUTHORS": " @alice, bob, ,alice,@carol "}
        )
        self.assertEqual(
            config.ALLOWED_ISSUE_AUTHORS, (_ALICE, _BOB, "carol")
        )


class MaxConflictRoundsConfigTest(unittest.TestCase):
    """`MAX_CONFLICT_ROUNDS` parses identically to `MAX_REVIEW_ROUNDS`:
    integer, defaults to 3, env override wins.
    """

    def test_default_is_three(self) -> None:
        config = _load_config()
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 3)

    def test_env_override(self) -> None:
        config = _load_config({"MAX_CONFLICT_ROUNDS": "7"})
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 7)


class MultiRepoConfigTest(unittest.TestCase):
    """`REPOS` parses N entries; when unset the legacy single-repo trio
    (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`) keeps working."""

    def test_legacy_single_repo_fallback(self) -> None:
        config = _load_config({
            "REPO": _LEGACY_REPO,
            "TARGET_REPO_ROOT": _LEGACY_ROOT,
            "BASE_BRANCH": _LEGACY_BRANCH,
        })

        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        spec = _only_repo_spec(specs)
        self.assertEqual(spec.slug, _LEGACY_REPO)
        self.assertEqual(spec.target_root, Path(_LEGACY_ROOT))
        self.assertEqual(spec.base_branch, _LEGACY_BRANCH)
        # No REMOTE_NAME set -> defaults to 'origin' so existing deployments
        # keep working unchanged.
        self.assertEqual(spec.remote_name, _ORIGIN_REMOTE)

    def test_remote_name_env_override_for_single_repo(self) -> None:
        # Multi-remote local clones (e.g. public `origin` + private fork
        # `private`) need to drive the non-default remote.
        config = _load_config({
            "REPO": _LEGACY_REPO,
            "TARGET_REPO_ROOT": _LEGACY_ROOT,
            "BASE_BRANCH": "main",
            "REMOTE_NAME": _PRIVATE_REMOTE,
        })
        spec = _only_repo_spec(config.default_repo_specs())
        self.assertEqual(spec.remote_name, _PRIVATE_REMOTE)

    def test_entries_accept_newline_and_semicolon(self) -> None:
        # Mix newlines, ';', blank lines, and a comment to verify the parser
        # accepts both separators and ignores noise.
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / "other"
            other.mkdir()
            config = _load_config({
                _REPOS_ENV: (
                    "# multi-repo example\n"
                    f"{_ALPHA_REPO}|{td}|main\n"
                    "\n"
                    f"{_BETA_REPO}|{other}|develop;gamma/three|{td}|master"
                ),
            })

            specs = config.default_repo_specs()
            self.assertEqual([spec.slug for spec in specs],
                             [_ALPHA_REPO, _BETA_REPO, "gamma/three"])
            self.assertEqual([spec.base_branch for spec in specs],
                             ["main", "develop", "master"])
            self.assertEqual(specs[1].target_root, other)
            # Backward-compatible: three-field entries default remote_name
            # to 'origin' so existing REPOS configs keep working.
            for spec in specs:
                self.assertEqual(spec.remote_name, _ORIGIN_REMOTE)
            # Returned list is a fresh copy so callers can't mutate the cache.
            specs.append("not-a-spec")  # type: ignore[arg-type]
            self.assertEqual(len(config.default_repo_specs()), 3)

    def test_optional_fourth_field_sets_remote_name(self) -> None:
        # Multi-remote target clones (e.g. public `origin` + private fork
        # `private`) need to drive the non-default remote.
        with tempfile.TemporaryDirectory() as td:
            config = _load_config({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}\n"
                    f"{_BETA_REPO}|{td}|main|{_PRIVATE_REMOTE}"
                ),
            })
            specs = config.default_repo_specs()
            self.assertEqual(
                [(spec.slug, spec.remote_name) for spec in specs],
                [
                    (_ALPHA_REPO, _ORIGIN_REMOTE),
                    (_BETA_REPO, _PRIVATE_REMOTE),
                ],
            )

    def test_empty_remote_name_aborts_at_import(self) -> None:
        # An explicit empty fourth field is a misconfiguration -- omit the
        # trailing '|' to get the default. Surface the mistake at startup.
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: f"{_ALPHA_REPO}|{td}|main|",
            })
            self.assertIn("remote_name", error_message)

    def test_too_many_pipe_segments_aborts_at_import(self) -> None:
        # Six fields is malformed -- five (with the optional remote_name and
        # parallel_limit) is the upper bound. Prevents a silent typo like
        # `owner/repo|/path|main|origin|3|extra` from being misinterpreted.
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|3|extra"
                ),
            })
            self.assertIn("malformed", error_message)

    def test_repos_overrides_legacy_trio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = _load_config({
                "REPO": "ignored/legacy",
                "TARGET_REPO_ROOT": "/nonexistent",
                "BASE_BRANCH": "ignored",
                _REPOS_ENV: f"{_ALPHA_REPO}|{td}|main",
            })

            specs = config.default_repo_specs()
            self.assertEqual(len(specs), 1)
            spec = _only_repo_spec(specs)
            self.assertEqual(spec.slug, _ALPHA_REPO)
            self.assertEqual(spec.target_root, Path(td))
            self.assertEqual(spec.base_branch, "main")

    def test_duplicate_slug_aborts_at_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main\n"
                    f"{_ALPHA_REPO}|{td}|develop"
                ),
            })
            self.assertIn("duplicate slug", error_message)
            self.assertIn(_ALPHA_REPO, error_message)

    def test_duplicate_slug_precedes_option_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main\n"
                    f"{_ALPHA_REPO}|{td}|develop|{_ORIGIN_REMOTE}|invalid"
                ),
            })
            self.assertIn("duplicate slug", error_message)

    def test_malformed_entry_aborts_at_import(self) -> None:
        # Wrong number of '|' segments.
        error_message = _config_error_message({
            _REPOS_ENV: "owner/repo|/tmp",
        })
        self.assertIn("malformed", error_message)

    def test_empty_slug_aborts_at_import(self) -> None:
        # Slug must contain '/'.
        error_message = _config_error_message({
            _REPOS_ENV: "no-slash|/tmp|main",
        })
        self.assertIn("owner/name", error_message)

    def test_empty_slug_component_aborts_import(self) -> None:
        # `owner//repo` and `/repo` and `owner/` are all malformed even
        # though they contain `/`; require exactly two non-empty components.
        for bad_slug in ("owner//repo", "/repo", "owner/", "//"):
            with self.subTest(slug=bad_slug):
                error_message = _config_error_message({
                    _REPOS_ENV: f"{bad_slug}|/tmp|main",
                })
                self.assertIn("owner/name", error_message)

    def test_extra_slug_segment_aborts_import(self) -> None:
        # `owner/repo/extra` looks plausible but PyGithub treats the slug
        # as the full repo identifier, so any extra `/` would resolve to
        # a wrong (or nonexistent) repo at runtime. Reject at import.
        error_message = _config_error_message({
            _REPOS_ENV: "owner/repo/extra|/tmp|main",
        })
        self.assertIn("owner/name", error_message)

    def test_empty_base_branch_aborts_at_import(self) -> None:
        error_message = _config_error_message({
            _REPOS_ENV: "owner/repo|/tmp|",
        })
        self.assertIn("base_branch", error_message)

    def test_empty_target_root_aborts_at_import(self) -> None:
        error_message = _config_error_message({
            _REPOS_ENV: "owner/repo||main",
        })
        self.assertIn("target_root", error_message)

    def test_repos_with_only_comments_aborts(self) -> None:
        # `REPOS` set but yielding zero entries is a misconfiguration --
        # better to fail loudly than silently fall back to the legacy trio
        # (which the user explicitly opted out of by setting REPOS).
        error_message = _config_error_message({
            _REPOS_ENV: "# just a comment\n  \n",
        })
        self.assertIn("no valid entries", error_message)

    def test_missing_target_warns_but_loads(self) -> None:
        # Confirms "warn loudly" semantics: the diagnostic lands on stderr,
        # never stdout, and does not abort the load.
        import io
        from contextlib import redirect_stderr, redirect_stdout

        captured_stderr = io.StringIO()
        captured_stdout = io.StringIO()
        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            config = _load_config(
                {_REPOS_ENV: f"{_ALPHA_REPO}|/this/path/does/not/exist|main"}
            )
        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertIn("does not exist", captured_stderr.getvalue())
        self.assertIn(_ALPHA_REPO, captured_stderr.getvalue())
        self.assertEqual(captured_stdout.getvalue(), "")


class ParallelLimitsConfigTest(unittest.TestCase):
    """Per-repo and global parallel issue-processing caps. Defaults preserve
    legacy single-issue-per-repo behavior (per-repo=1) while bounding total
    spawn fan-out across all configured repos (global=3). Each `REPOS` entry
    can override its per-repo limit via the optional fifth pipe field.
    """

    def test_defaults_one_per_repo_three_global(self) -> None:
        config = _load_config()
        self.assertEqual(config.MAX_PARALLEL_ISSUES_PER_REPO, 1)
        self.assertEqual(config.MAX_PARALLEL_ISSUES_GLOBAL, 3)

    def test_env_overrides_take_effect(self) -> None:
        config = _load_config({
            _PER_REPO_LIMIT_ENV: "2",
            _GLOBAL_LIMIT_ENV: "10",
        })
        self.assertEqual(config.MAX_PARALLEL_ISSUES_PER_REPO, 2)
        self.assertEqual(config.MAX_PARALLEL_ISSUES_GLOBAL, 10)

    def test_legacy_repo_gets_default_limit(self) -> None:
        # When REPOS is unset, the legacy single-repo RepoSpec must adopt
        # whatever MAX_PARALLEL_ISSUES_PER_REPO is set to (default 1).
        config = _load_config()
        spec = _only_repo_spec(config.default_repo_specs())
        self.assertEqual(spec.parallel_limit, 1)

    def test_legacy_single_repo_picks_up_env_override(self) -> None:
        config = _load_config({_PER_REPO_LIMIT_ENV: "4"})
        spec = _only_repo_spec(config.default_repo_specs())
        self.assertEqual(spec.parallel_limit, 4)

    def test_three_field_entries_inherit_env_default(self) -> None:
        # Backward-compat: existing three-field REPOS configs inherit the
        # MAX_PARALLEL_ISSUES_PER_REPO env default (or 1 if unset).
        with tempfile.TemporaryDirectory() as td:
            config = _load_config({
                _PER_REPO_LIMIT_ENV: "2",
                _REPOS_ENV: f"{_ALPHA_REPO}|{td}|main",
            })
            spec = _only_repo_spec(config.default_repo_specs())
            self.assertEqual(spec.parallel_limit, 2)

    def test_four_field_entries_inherit_env_default(self) -> None:
        # The existing four-field (with remote_name) shape stays backward-
        # compatible: parallel_limit falls back to the env default.
        with tempfile.TemporaryDirectory() as td:
            config = _load_config({
                _PER_REPO_LIMIT_ENV: "5",
                _REPOS_ENV: f"{_ALPHA_REPO}|{td}|main|{_PRIVATE_REMOTE}",
            })
            spec = _only_repo_spec(config.default_repo_specs())
            self.assertEqual(spec.remote_name, _PRIVATE_REMOTE)
            self.assertEqual(spec.parallel_limit, 5)

    def test_fifth_field_overrides_per_repo_limit(self) -> None:
        # Per-entry override takes precedence over the global env default,
        # so a busy repo can run more issues in parallel than its peers.
        with tempfile.TemporaryDirectory() as td:
            config = _load_config({
                _PER_REPO_LIMIT_ENV: _ENABLED_ENV,
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|3\n"
                    f"{_BETA_REPO}|{td}|main|{_ORIGIN_REMOTE}|7"
                ),
            })
            specs = config.default_repo_specs()
            self.assertEqual(
                [(spec.slug, spec.parallel_limit) for spec in specs],
                [(_ALPHA_REPO, 3), (_BETA_REPO, 7)],
            )

    def test_mixed_entries_three_four_five_fields(self) -> None:
        # All three legacy field counts coexist; only the five-field entry
        # overrides the per-repo default.
        with tempfile.TemporaryDirectory() as td:
            config = _load_config({
                _PER_REPO_LIMIT_ENV: "2",
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main\n"
                    f"{_BETA_REPO}|{td}|main|{_PRIVATE_REMOTE}\n"
                    f"gamma/three|{td}|main|{_ORIGIN_REMOTE}|6"
                ),
            })
            specs = config.default_repo_specs()
            self.assertEqual(
                [(spec.slug, spec.remote_name, spec.parallel_limit) for spec in specs],
                [
                    (_ALPHA_REPO, _ORIGIN_REMOTE, 2),
                    (_BETA_REPO, _PRIVATE_REMOTE, 2),
                    ("gamma/three", _ORIGIN_REMOTE, 6),
                ],
            )

    def test_non_numeric_repo_limit_aborts_import(self) -> None:
        error_message = _config_error_message({_PER_REPO_LIMIT_ENV: "lots"})
        self.assertIn(_PER_REPO_LIMIT_ENV, error_message)
        self.assertIn("lots", error_message)

    def test_zero_per_repo_env_aborts_at_import(self) -> None:
        error_message = _config_error_message({
            _PER_REPO_LIMIT_ENV: _DISABLED_ENV,
        })
        self.assertIn(_PER_REPO_LIMIT_ENV, error_message)

    def test_negative_per_repo_env_aborts_at_import(self) -> None:
        error_message = _config_error_message({_PER_REPO_LIMIT_ENV: "-1"})
        self.assertIn(_PER_REPO_LIMIT_ENV, error_message)

    def test_non_numeric_global_env_aborts_at_import(self) -> None:
        error_message = _config_error_message({_GLOBAL_LIMIT_ENV: "many"})
        self.assertIn(_GLOBAL_LIMIT_ENV, error_message)

    def test_zero_global_env_aborts_at_import(self) -> None:
        error_message = _config_error_message({
            _GLOBAL_LIMIT_ENV: _DISABLED_ENV,
        })
        self.assertIn(_GLOBAL_LIMIT_ENV, error_message)

    def test_malformed_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|seven"
                ),
            })
            self.assertIn(_PARALLEL_LIMIT_FIELD, error_message)
            self.assertIn("seven", error_message)

    def test_zero_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|0"
                ),
            })
            self.assertIn(_PARALLEL_LIMIT_FIELD, error_message)
            self.assertIn(">= 1", error_message)

    def test_negative_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: (
                    f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|-2"
                ),
            })
            self.assertIn(_PARALLEL_LIMIT_FIELD, error_message)

    def test_empty_parallel_limit_field_aborts(self) -> None:
        # An explicit empty fifth field is a misconfiguration -- omit the
        # trailing '|' to get the default. Surface the mistake at startup.
        with tempfile.TemporaryDirectory() as td:
            error_message = _config_error_message({
                _REPOS_ENV: f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|",
            })
            self.assertIn(_PARALLEL_LIMIT_FIELD, error_message)


class ConfigDiagnosticsTest(unittest.TestCase):
    """Configuration failures and warnings funnel through two helpers:
    `_config_error` aborts import (SystemExit carrying the message, exit
    code 1) and `_config_warning` emits a non-fatal line to stderr without
    touching stdout. Every import-time validation and dotenv warning path
    routes through these, so the message, exit code, and stream are pinned
    here at the producer level.
    """

    def test_config_error_carries_message_and_code(self) -> None:
        from orchestrator.config import _config_error

        with self.assertRaises(SystemExit) as cm:
            _config_error("orchestrator: bad config")
        # `str(exc)` is what the import-time validation tests assert on; a
        # string code exits the process with status 1.
        self.assertEqual(str(cm.exception), "orchestrator: bad config")
        self.assertEqual(cm.exception.code, "orchestrator: bad config")

    def test_config_warning_writes_to_stderr_only(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from orchestrator.config import _config_warning

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            _config_warning("orchestrator: heads up")
        self.assertEqual(err.getvalue(), "orchestrator: heads up\n")
        self.assertEqual(out.getvalue(), "")


class RepositoryConfigModuleTest(unittest.TestCase):
    """The repository-entry model and REPOS parsing / default-spec
    construction live in the private ``orchestrator._repo_config`` leaf;
    ``orchestrator.config`` stays the compatibility import site via a
    ``RepoSpec`` re-export and the ``_parse_repos_env`` / ``default_repo_specs``
    wrappers existing callers and test patches resolve.
    """

    def test_repospec_reexported_from_private_module(self) -> None:
        import orchestrator.config as config
        from orchestrator import _repo_config

        self.assertIs(config.RepoSpec, _repo_config.RepoSpec)
        self.assertEqual(
            config.RepoSpec.__module__, "orchestrator._repo_config"
        )

    def test_compat_wrappers_stay_on_config(self) -> None:
        import orchestrator.config as config

        # `config._parse_repos_env` / `config.default_repo_specs` are the
        # narrow wrappers; their module of record is `orchestrator.config`
        # so `patch.object(config, ...)` keeps intercepting them.
        self.assertEqual(config._parse_repos_env.__module__, _CONFIG_MODULE)
        self.assertEqual(
            config.default_repo_specs.__module__, _CONFIG_MODULE,
        )

    def test_parse_repos_env_is_a_stdlib_leaf(self) -> None:
        # The extracted parser takes its default and diagnostics as injected
        # callables rather than reading config module state, so it parses
        # without importing config back.
        from orchestrator import _repo_config

        errors: list[str] = []
        warnings: list[str] = []

        def fail(message: str):
            errors.append(message)
            raise SystemExit(message)

        with tempfile.TemporaryDirectory() as td:
            specs = _repo_config.parse_repos_env(
                f"{_ALPHA_REPO}|{td}|main|{_ORIGIN_REMOTE}|4",
                default_parallel_limit=2,
                config_error=fail,
                config_warning=warnings.append,
            )
        spec = _only_repo_spec(specs)
        self.assertEqual(spec.slug, _ALPHA_REPO)
        self.assertEqual(spec.parallel_limit, 4)
        self.assertEqual((errors, warnings), ([], []))

    def test_parser_uses_injected_default_limit(self) -> None:
        # An entry that omits parallel_limit adopts the injected default,
        # not any config-global fallback.
        from orchestrator import _repo_config

        with tempfile.TemporaryDirectory() as td:
            specs = _repo_config.parse_repos_env(
                f"{_ALPHA_REPO}|{td}|main",
                default_parallel_limit=7,
                config_error=lambda message: (_ for _ in ()).throw(
                    SystemExit(message)
                ),
                config_warning=lambda _message: None,
            )
        spec = _only_repo_spec(specs)
        self.assertEqual(spec.parallel_limit, 7)

    def test_builder_uses_default_spec(self) -> None:
        # A blank REPOS value yields exactly the injected legacy single-repo
        # spec, without touching the parser or the diagnostics.
        from orchestrator import _repo_config

        default_spec = _repo_config.RepoSpec(
            slug=_LEGACY_REPO,
            target_root=Path(_LEGACY_ROOT),
            base_branch=_LEGACY_BRANCH,
            remote_name=_PRIVATE_REMOTE,
            parallel_limit=5,
        )
        specs = _repo_config.build_repo_specs(
            "   ",
            default_spec=default_spec,
            config_error=lambda message: (_ for _ in ()).throw(
                SystemExit(message)
            ),
            config_warning=lambda _message: None,
        )
        self.assertEqual(specs, [default_spec])


if __name__ == "__main__":
    unittest.main()
