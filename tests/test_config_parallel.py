# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused configuration behavior tests."""

import tempfile
import unittest

from tests import config_reload_helpers as _reload
from tests import config_test_support as _support
from tests import config_test_values as _config_cases


class ParallelLimitDefaultsTest(unittest.TestCase):
    """Per-repo and global parallel issue-processing caps. Defaults preserve
    legacy single-issue-per-repo behavior (per-repo=1) while bounding total
    spawn fan-out across all configured repos (global=3). Each `REPOS` entry
    can override its per-repo limit via the optional fifth pipe field.
    """

    def test_defaults_one_per_repo_three_global(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.MAX_PARALLEL_ISSUES_PER_REPO, 1)
        self.assertEqual(config.MAX_PARALLEL_ISSUES_GLOBAL, 3)

    def test_env_overrides_take_effect(self) -> None:
        config = _reload.load_config(
            {
                _config_cases._PER_REPO_LIMIT_ENV: "2",
                _config_cases._GLOBAL_LIMIT_ENV: "10",
            }
        )
        self.assertEqual(config.MAX_PARALLEL_ISSUES_PER_REPO, 2)
        self.assertEqual(config.MAX_PARALLEL_ISSUES_GLOBAL, 10)

    def test_legacy_repo_gets_default_limit(self) -> None:
        # When REPOS is unset, the legacy single-repo RepoSpec must adopt
        # whatever MAX_PARALLEL_ISSUES_PER_REPO is set to (default 1).
        config = _reload.load_config()
        spec = _support.only_repo_spec(config.default_repo_specs())
        self.assertEqual(spec.parallel_limit, 1)

    def test_legacy_single_repo_picks_up_env_override(self) -> None:
        config = _reload.load_config({_config_cases._PER_REPO_LIMIT_ENV: "4"})
        spec = _support.only_repo_spec(config.default_repo_specs())
        self.assertEqual(spec.parallel_limit, 4)


class RepositoryParallelLimitParsingTest(unittest.TestCase):
    def test_three_field_entries_inherit_env_default(self) -> None:
        # Backward-compat: existing three-field REPOS configs inherit the
        # MAX_PARALLEL_ISSUES_PER_REPO env default (or 1 if unset).
        with tempfile.TemporaryDirectory() as td:
            config = _reload.load_config(
                {
                    _config_cases._PER_REPO_LIMIT_ENV: "2",
                    _config_cases._REPOS_ENV: f"{_config_cases._ALPHA_REPO}|{td}|main",
                }
            )
            spec = _support.only_repo_spec(config.default_repo_specs())
            self.assertEqual(spec.parallel_limit, 2)

    def test_four_field_entries_inherit_env_default(self) -> None:
        # The existing four-field (with remote_name) shape stays backward-
        # compatible: parallel_limit falls back to the env default.
        with tempfile.TemporaryDirectory() as td:
            config = _reload.load_config(
                {
                    _config_cases._PER_REPO_LIMIT_ENV: "5",
                    _config_cases._REPOS_ENV: f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._PRIVATE_REMOTE}",
                }
            )
            spec = _support.only_repo_spec(config.default_repo_specs())
            self.assertEqual(spec.remote_name, _config_cases._PRIVATE_REMOTE)
            self.assertEqual(spec.parallel_limit, 5)

    def test_fifth_field_overrides_per_repo_limit(self) -> None:
        # Per-entry override takes precedence over the global env default,
        # so a busy repo can run more issues in parallel than its peers.
        with tempfile.TemporaryDirectory() as td:
            config = _reload.load_config(
                {
                    _config_cases._PER_REPO_LIMIT_ENV: _config_cases._ENABLED_ENV,
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|3\n"
                        f"{_config_cases._BETA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|7"
                    ),
                }
            )
            specs = config.default_repo_specs()
            self.assertEqual(
                [(spec.slug, spec.parallel_limit) for spec in specs],
                [(_config_cases._ALPHA_REPO, 3), (_config_cases._BETA_REPO, 7)],
            )

    def test_mixed_entries_three_four_five_fields(self) -> None:
        # All three legacy field counts coexist; only the five-field entry
        # overrides the per-repo default.
        with tempfile.TemporaryDirectory() as td:
            config = _reload.load_config(
                {
                    _config_cases._PER_REPO_LIMIT_ENV: "2",
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main\n"
                        f"{_config_cases._BETA_REPO}|{td}|main|{_config_cases._PRIVATE_REMOTE}\n"
                        f"gamma/three|{td}|main|{_config_cases._ORIGIN_REMOTE}|6"
                    ),
                }
            )
            specs = config.default_repo_specs()
            self.assertEqual(
                [(spec.slug, spec.remote_name, spec.parallel_limit) for spec in specs],
                [
                    (_config_cases._ALPHA_REPO, _config_cases._ORIGIN_REMOTE, 2),
                    (_config_cases._BETA_REPO, _config_cases._PRIVATE_REMOTE, 2),
                    ("gamma/three", _config_cases._ORIGIN_REMOTE, 6),
                ],
            )


class ParallelLimitEnvironmentErrorTest(unittest.TestCase):
    def test_non_numeric_repo_limit_aborts_import(self) -> None:
        error_message = _reload.config_error_message({_config_cases._PER_REPO_LIMIT_ENV: "lots"})
        self.assertIn(_config_cases._PER_REPO_LIMIT_ENV, error_message)
        self.assertIn("lots", error_message)

    def test_zero_per_repo_env_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message(
            {
                _config_cases._PER_REPO_LIMIT_ENV: _config_cases._DISABLED_ENV,
            }
        )
        self.assertIn(_config_cases._PER_REPO_LIMIT_ENV, error_message)

    def test_negative_per_repo_env_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message({_config_cases._PER_REPO_LIMIT_ENV: "-1"})
        self.assertIn(_config_cases._PER_REPO_LIMIT_ENV, error_message)

    def test_non_numeric_global_env_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message({_config_cases._GLOBAL_LIMIT_ENV: "many"})
        self.assertIn(_config_cases._GLOBAL_LIMIT_ENV, error_message)

    def test_zero_global_env_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message(
            {
                _config_cases._GLOBAL_LIMIT_ENV: _config_cases._DISABLED_ENV,
            }
        )
        self.assertIn(_config_cases._GLOBAL_LIMIT_ENV, error_message)


class RepositoryParallelLimitErrorTest(unittest.TestCase):
    def test_malformed_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|seven"
                    ),
                }
            )
            self.assertIn(_config_cases._PARALLEL_LIMIT_FIELD, error_message)
            self.assertIn("seven", error_message)

    def test_zero_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|0"
                    ),
                }
            )
            self.assertIn(_config_cases._PARALLEL_LIMIT_FIELD, error_message)
            self.assertIn(">= 1", error_message)

    def test_negative_parallel_limit_in_repos_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: (
                        f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|-2"
                    ),
                }
            )
            self.assertIn(_config_cases._PARALLEL_LIMIT_FIELD, error_message)

    def test_empty_parallel_limit_field_aborts(self) -> None:
        # An explicit empty fifth field is a misconfiguration -- omit the
        # trailing '|' to get the default. Surface the mistake at startup.
        with tempfile.TemporaryDirectory() as td:
            error_message = _reload.config_error_message(
                {
                    _config_cases._REPOS_ENV: f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|",
                }
            )
            self.assertIn(_config_cases._PARALLEL_LIMIT_FIELD, error_message)
