# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused configuration behavior tests."""

import tempfile
import unittest
from pathlib import Path

from tests import config_reload_helpers as _reload
from tests import config_test_support as _support
from tests import config_test_values as _config_cases


class RepositoryConfigParsingTest(unittest.TestCase):
    """`REPOS` parses N entries; when unset the legacy single-repo trio
    (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`) keeps working."""

    def test_legacy_single_repo_fallback(self) -> None:
        config = _reload.load_config(
            {
                "REPO": _config_cases._LEGACY_REPO,
                "TARGET_REPO_ROOT": _config_cases._LEGACY_ROOT,
                "BASE_BRANCH": _config_cases._LEGACY_BRANCH,
            }
        )

        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        spec = _support.only_repo_spec(specs)
        self.assertEqual(spec.slug, _config_cases._LEGACY_REPO)
        self.assertEqual(spec.target_root, Path(_config_cases._LEGACY_ROOT))
        self.assertEqual(spec.base_branch, _config_cases._LEGACY_BRANCH)
        # No REMOTE_NAME set -> defaults to 'origin' so existing deployments
        # keep working unchanged.
        self.assertEqual(spec.remote_name, _config_cases._ORIGIN_REMOTE)

    def test_remote_name_env_override_for_single_repo(self) -> None:
        # Multi-remote local clones (e.g. public `origin` + private fork
        # `private`) need to drive the non-default remote.
        config = _reload.load_config(
            {
                "REPO": _config_cases._LEGACY_REPO,
                "TARGET_REPO_ROOT": _config_cases._LEGACY_ROOT,
                "BASE_BRANCH": "main",
                "REMOTE_NAME": _config_cases._PRIVATE_REMOTE,
            }
        )
        spec = _support.only_repo_spec(config.default_repo_specs())
        self.assertEqual(spec.remote_name, _config_cases._PRIVATE_REMOTE)

    def test_entries_accept_newline_and_semicolon(self) -> None:
        # Mix newlines, ';', blank lines, and a comment to verify the parser
        # accepts both separators and ignores noise.
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / "other"
            other.mkdir()
            config = _reload.load_config(
                {
                    _config_cases._REPOS_ENV: (
                        "# multi-repo example\n"
                        f"{_config_cases._ALPHA_REPO}|{td}|main\n"
                        "\n"
                        f"{_config_cases._BETA_REPO}|{other}|develop;gamma/three|{td}|master"
                    ),
                }
            )

            specs = config.default_repo_specs()
            self.assertEqual(
                [spec.slug for spec in specs], [_config_cases._ALPHA_REPO, _config_cases._BETA_REPO, "gamma/three"]
            )
            self.assertEqual([spec.base_branch for spec in specs], ["main", "develop", "master"])
            self.assertEqual(specs[1].target_root, other)
            # Backward-compatible: three-field entries default remote_name
            # to 'origin' so existing REPOS configs keep working.
            for spec in specs:
                self.assertEqual(spec.remote_name, _config_cases._ORIGIN_REMOTE)
            # Returned list is a fresh copy so callers can't mutate the cache.
            specs.append("not-a-spec")  # type: ignore[arg-type]
            self.assertEqual(len(config.default_repo_specs()), 3)

    def test_optional_fourth_field_sets_remote_name(self) -> None:
        # Multi-remote target clones (e.g. public `origin` + private fork
        # `private`) need to drive the non-default remote.
        with tempfile.TemporaryDirectory() as td:
            config = _reload.load_config(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}\n"
                        f"{_config_cases._BETA_REPO}|{td}|main|{_config_cases._PRIVATE_REMOTE}"
                    ),
                }
            )
            specs = config.default_repo_specs()
            self.assertEqual(
                [(spec.slug, spec.remote_name) for spec in specs],
                [
                    (_config_cases._ALPHA_REPO, _config_cases._ORIGIN_REMOTE),
                    (_config_cases._BETA_REPO, _config_cases._PRIVATE_REMOTE),
                ],
            )

    def test_repos_overrides_legacy_trio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = _reload.load_config(
                {
                    "REPO": "ignored/legacy",
                    "TARGET_REPO_ROOT": "/nonexistent",
                    "BASE_BRANCH": "ignored",
                    _config_cases._REPOS_ENV: f"{_config_cases._ALPHA_REPO}|{td}|main",
                }
            )

            specs = config.default_repo_specs()
            self.assertEqual(len(specs), 1)
            spec = _support.only_repo_spec(specs)
            self.assertEqual(spec.slug, _config_cases._ALPHA_REPO)
            self.assertEqual(spec.target_root, Path(td))
            self.assertEqual(spec.base_branch, "main")

    def test_missing_target_warns_but_loads(self) -> None:
        # Confirms "warn loudly" semantics: the diagnostic lands on stderr,
        # never stdout, and does not abort the load.
        import io
        from contextlib import redirect_stderr, redirect_stdout

        captured_stderr = io.StringIO()
        captured_stdout = io.StringIO()
        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            config = _reload.load_config(
                {_config_cases._REPOS_ENV: f"{_config_cases._ALPHA_REPO}|/this/path/does/not/exist|main"}
            )
        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertIn("does not exist", captured_stderr.getvalue())
        self.assertIn(_config_cases._ALPHA_REPO, captured_stderr.getvalue())
        self.assertEqual(captured_stdout.getvalue(), "")


class RepositoryConfigValidationTest(unittest.TestCase):
    """`REPOS` parses N entries; when unset the legacy single-repo trio
    (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`) keeps working."""

    def test_empty_remote_name_aborts_at_import(self) -> None:
        # An explicit empty fourth field is a misconfiguration -- omit the
        # trailing '|' to get the default. Surface the mistake at startup.
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: f"{_config_cases._ALPHA_REPO}|{td}|main|",
                }
            )
            self.assertIn("remote_name", error_message)

    def test_too_many_pipe_segments_aborts_at_import(self) -> None:
        # Six fields is malformed -- five (with the optional remote_name and
        # parallel_limit) is the upper bound. Prevents a silent typo like
        # `owner/repo|/path|main|origin|3|extra` from being misinterpreted.
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|3|extra"
                    ),
                }
            )
            self.assertIn("malformed", error_message)

    def test_duplicate_slug_aborts_at_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main\n{_config_cases._ALPHA_REPO}|{td}|develop"
                    ),
                }
            )
            self.assertIn("duplicate slug", error_message)
            self.assertIn(_config_cases._ALPHA_REPO, error_message)

    def test_duplicate_slug_precedes_option_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main\n"
                        f"{_config_cases._ALPHA_REPO}|{td}|develop|{_config_cases._ORIGIN_REMOTE}|invalid"
                    ),
                }
            )
            self.assertIn("duplicate slug", error_message)

    def test_malformed_entry_aborts_at_import(self) -> None:
        # Wrong number of '|' segments.
        error_message = _reload.config_error_message(
            {
                _config_cases._REPOS_ENV: "owner/repo|/tmp",
            }
        )
        self.assertIn("malformed", error_message)

    def test_empty_slug_aborts_at_import(self) -> None:
        # Slug must contain '/'.
        error_message = _reload.config_error_message(
            {
                _config_cases._REPOS_ENV: "no-slash|/tmp|main",
            }
        )
        self.assertIn("owner/name", error_message)


class RepositorySlugValidationTest(unittest.TestCase):
    """`REPOS` parses N entries; when unset the legacy single-repo trio
    (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`) keeps working."""

    def test_empty_slug_component_aborts_import(self) -> None:
        # `owner//repo` and `/repo` and `owner/` are all malformed even
        # though they contain `/`; require exactly two non-empty components.
        for bad_slug in ("owner//repo", "/repo", "owner/", "//"):
            with self.subTest(slug=bad_slug):
                error_message = _reload.config_error_message(
                    {
                        _config_cases._REPOS_ENV: f"{bad_slug}|/tmp|main",
                    }
                )
                self.assertIn("owner/name", error_message)

    def test_extra_slug_segment_aborts_import(self) -> None:
        # `owner/repo/extra` looks plausible but PyGithub treats the slug
        # as the full repo identifier, so any extra `/` would resolve to
        # a wrong (or nonexistent) repo at runtime. Reject at import.
        error_message = _reload.config_error_message(
            {
                _config_cases._REPOS_ENV: "owner/repo/extra|/tmp|main",
            }
        )
        self.assertIn("owner/name", error_message)

    def test_empty_base_branch_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message(
            {
                _config_cases._REPOS_ENV: "owner/repo|/tmp|",
            }
        )
        self.assertIn("base_branch", error_message)

    def test_empty_target_root_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message(
            {
                _config_cases._REPOS_ENV: "owner/repo||main",
            }
        )
        self.assertIn("target_root", error_message)

    def test_repos_with_only_comments_aborts(self) -> None:
        # `REPOS` set but yielding zero entries is a misconfiguration --
        # better to fail loudly than silently fall back to the legacy trio
        # (which the user explicitly opted out of by setting REPOS).
        error_message = _reload.config_error_message(
            {
                _config_cases._REPOS_ENV: "# just a comment\n  \n",
            }
        )
        self.assertIn("no valid entries", error_message)
