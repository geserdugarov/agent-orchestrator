# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Repository configuration module-boundary tests."""

import importlib
import tempfile
import unittest
from pathlib import Path

from tests import config_test_support as _support
from tests import config_test_values as _config_cases


class RepositoryConfigModuleTest(unittest.TestCase):
    """The repository-entry model and REPOS parsing / default-spec
    construction live in the private ``orchestrator._repo_config`` leaf;
    ``orchestrator.config`` stays the compatibility import site via a
    ``RepoSpec`` re-export and the ``_parse_repos_env`` / ``default_repo_specs``
    wrappers existing callers and test patches resolve.
    """

    def test_repospec_reexported_from_private_module(self) -> None:
        config = importlib.import_module(_config_cases._CONFIG_MODULE)
        from orchestrator import _repo_config

        self.assertIs(config.RepoSpec, _repo_config.RepoSpec)
        self.assertEqual(config.RepoSpec.__module__, "orchestrator._repo_config")

    def test_compat_wrappers_stay_on_config(self) -> None:
        config = importlib.import_module(_config_cases._CONFIG_MODULE)

        # `config._parse_repos_env` / `config.default_repo_specs` are the
        # narrow wrappers; their module of record is `orchestrator.config`
        # so `patch.object(config, ...)` keeps intercepting them.
        self.assertEqual(config._parse_repos_env.__module__, _config_cases._CONFIG_MODULE)
        self.assertEqual(
            config.default_repo_specs.__module__,
            _config_cases._CONFIG_MODULE,
        )

    def test_parse_repos_env_is_a_stdlib_leaf(self) -> None:
        # The extracted parser takes its default and diagnostics as injected
        # callables rather than reading config module state, so it parses
        # without importing config back.
        from orchestrator import _repo_config

        errors: list[str] = []
        warnings: list[str] = []

        with tempfile.TemporaryDirectory() as td:
            specs = _repo_config.parse_repos_env(
                f"{_config_cases._ALPHA_REPO}|{td}|main|{_config_cases._ORIGIN_REMOTE}|4",
                default_parallel_limit=2,
                config_error=_support.ConfigErrorRecorder(errors),
                config_warning=warnings.append,
            )
        spec = _support.only_repo_spec(specs)
        self.assertEqual(spec.slug, _config_cases._ALPHA_REPO)
        self.assertEqual(spec.parallel_limit, 4)
        self.assertEqual((errors, warnings), ([], []))

    def test_parser_uses_injected_default_limit(self) -> None:
        # An entry that omits parallel_limit adopts the injected default,
        # not any config-global fallback.
        from orchestrator import _repo_config

        with tempfile.TemporaryDirectory() as td:
            specs = _repo_config.parse_repos_env(
                f"{_config_cases._ALPHA_REPO}|{td}|main",
                default_parallel_limit=7,
                config_error=_support.exit_with_config_error,
                config_warning=lambda _message: None,
            )
        spec = _support.only_repo_spec(specs)
        self.assertEqual(spec.parallel_limit, 7)

    def test_builder_uses_default_spec(self) -> None:
        # A blank REPOS value yields exactly the injected legacy single-repo
        # spec, without touching the parser or the diagnostics.
        from orchestrator import _repo_config

        default_spec = _repo_config.RepoSpec(
            slug=_config_cases._LEGACY_REPO,
            target_root=Path(_config_cases._LEGACY_ROOT),
            base_branch=_config_cases._LEGACY_BRANCH,
            remote_name=_config_cases._PRIVATE_REMOTE,
            parallel_limit=5,
        )
        specs = _repo_config.build_repo_specs(
            "   ",
            default_spec=default_spec,
            config_error=_support.exit_with_config_error,
            config_warning=lambda _message: None,
        )
        self.assertEqual(specs, [default_spec])
