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
    """The repository-entry model lives in ``orchestrator.config.models`` and
    the REPOS parsing / default-spec construction in
    ``orchestrator.config.repositories``; ``orchestrator.config`` stays the
    compatibility import site via a ``RepoSpec`` re-export and the
    ``default_repo_specs`` wrapper existing callers and test patches resolve.
    """

    def test_repospec_reexported_from_models_module(self) -> None:
        config = importlib.import_module(_config_cases._CONFIG_MODULE)
        from orchestrator.config import models

        self.assertIs(config.RepoSpec, models.RepoSpec)
        self.assertEqual(config.RepoSpec.__module__, _config_cases._MODELS_MODULE)

    def test_default_repo_specs_wrapper_on_config(self) -> None:
        config = importlib.import_module(_config_cases._CONFIG_MODULE)

        # `config.default_repo_specs` is the narrow wrapper; its module of
        # record is `orchestrator.config` so `patch.object(config, ...)` keeps
        # intercepting it.
        self.assertEqual(
            config.default_repo_specs.__module__,
            _config_cases._CONFIG_MODULE,
        )

    def test_parse_repos_env_is_a_leaf(self) -> None:
        # The parser takes its default and diagnostics as injected callables
        # rather than reading config module state, so it parses without
        # importing config back.
        from orchestrator.config import repositories

        errors: list[str] = []
        warnings: list[str] = []

        with tempfile.TemporaryDirectory() as td:
            specs = repositories.parse_repos_env(
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
        from orchestrator.config import repositories

        with tempfile.TemporaryDirectory() as td:
            specs = repositories.parse_repos_env(
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
        from orchestrator.config import models, repositories

        default_spec = models.RepoSpec(
            slug=_config_cases._LEGACY_REPO,
            target_root=Path(_config_cases._LEGACY_ROOT),
            base_branch=_config_cases._LEGACY_BRANCH,
            remote_name=_config_cases._PRIVATE_REMOTE,
            parallel_limit=5,
        )
        specs = repositories.build_repo_specs(
            "   ",
            default_spec=default_spec,
            config_error=_support.exit_with_config_error,
            config_warning=lambda _message: None,
        )
        self.assertEqual(specs, [default_spec])
