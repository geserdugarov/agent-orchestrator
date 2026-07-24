# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration package public-surface and compatibility-alias tests."""

import importlib
import unittest
from types import MappingProxyType

from orchestrator.config import _dotenv, credentials, environment

_CONFIG_MODULE = "orchestrator.config"
_HERMETIC = MappingProxyType(
    {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
)
# The internal resolver key backing the private `_REPO_SPECS`; the public
# surface exposes the `default_repo_specs` accessor instead.
_INTERNAL_KEYS = frozenset(("REPO_SPECS",))
_API_NAMES = frozenset(("RepoSpec", "default_repo_specs", "REPO_ROOT"))
# Private names still imported by name by unmigrated consumers; deliberately
# excluded from `__all__`.
_COMPAT_ALIASES = (
    "_config_error",
    "_config_warning",
    "_load_dotenv",
    "_strip_dotenv_quotes",
    "_resolve_github_token",
    "_parse_agent_spec",
    "_parse_verify_commands",
)


def _resolver_settings():
    config = importlib.import_module(_CONFIG_MODULE)
    resolved = environment._SettingsResolver(
        dict(_HERMETIC),
        config.REPO_ROOT,
        config._config_error,
        config._config_warning,
    ).resolve()
    return {key for key in resolved if key not in _INTERNAL_KEYS}


class PublicSurfaceTest(unittest.TestCase):
    """`orchestrator.config.__all__` is the exact public surface: the
    `RepoSpec` / `default_repo_specs` / `REPO_ROOT` package API plus every
    resolver-produced setting, with the internal `REPO_SPECS` list hidden
    behind the `default_repo_specs` accessor.
    """

    def setUp(self) -> None:
        self._config = importlib.import_module(_CONFIG_MODULE)

    def test_all_has_no_duplicates(self) -> None:
        exported = self._config.__all__
        self.assertEqual(len(exported), len(set(exported)))

    def test_all_matches_resolver_surface_plus_api(self) -> None:
        self.assertEqual(
            set(self._config.__all__),
            _resolver_settings() | _API_NAMES,
        )

    def test_all_names_are_resolvable_attributes(self) -> None:
        for name in self._config.__all__:
            self.assertTrue(hasattr(self._config, name), name)

    def test_repo_root_is_exported(self) -> None:
        # `_main_self_update` reads `config.REPO_ROOT` at runtime, so it has to
        # stay part of the exported surface.
        self.assertIn("REPO_ROOT", self._config.__all__)

    def test_all_lists_only_public_names(self) -> None:
        # `from orchestrator.config import *` exports exactly `__all__`, so a
        # surface free of private names keeps the compatibility aliases out.
        private = [name for name in self._config.__all__ if name.startswith("_")]
        self.assertEqual(private, [])


class CompatibilityAliasTest(unittest.TestCase):
    """The private `_config_*` / `_parse_*` / `_load_dotenv` /
    `_strip_dotenv_quotes` / `_resolve_github_token` aliases stay importable
    by name for unmigrated consumers, delegate to the owning leaf, and are
    kept out of the public `__all__`.
    """

    def setUp(self) -> None:
        self._config = importlib.import_module(_CONFIG_MODULE)

    def test_aliases_present_but_unexported(self) -> None:
        for alias in _COMPAT_ALIASES:
            with self.subTest(alias=alias):
                self.assertTrue(hasattr(self._config, alias))
                self.assertNotIn(alias, self._config.__all__)

    def test_aliases_delegate_to_owning_leaf(self) -> None:
        self.assertIs(
            self._config._strip_dotenv_quotes, _dotenv.strip_dotenv_quotes,
        )
        self.assertIs(
            self._config._resolve_github_token, credentials.resolve_github_token,
        )
        self.assertIs(
            self._config._parse_verify_commands, environment.parse_verify_commands,
        )

    def test_parse_agent_spec_alias_round_trips(self) -> None:
        self.assertEqual(
            self._config._parse_agent_spec("DEV_AGENT", "codex -m gpt-5.5"),
            ("codex", ("-m", "gpt-5.5")),
        )
