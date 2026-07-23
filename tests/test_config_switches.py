# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused configuration behavior tests."""

import unittest

from tests import config_reload_helpers as _reload
from tests import config_test_values as _config_cases


class DecomposeKillSwitchConfigTest(unittest.TestCase):
    """The DECOMPOSE kill switch defaults on; truthy spellings keep it on,
    explicit off / typos disable it. Strict parser semantics so a typo
    doesn't silently flip the user's intent.
    """

    def test_default_is_on(self) -> None:
        config = _reload.load_config()
        self.assertTrue(config.DECOMPOSE)

    def test_explicit_off(self) -> None:
        config = _reload.load_config({_config_cases._DECOMPOSE_ENV: _config_cases._OFF})
        self.assertFalse(config.DECOMPOSE)

    def test_truthy_spellings_keep_on(self) -> None:
        for spelling in (
            "on",
            "ON",
            " on ",
            _config_cases._ENABLED_ENV,
            "true",
            "True",
            "yes",
        ):
            with self.subTest(value=spelling):
                config = _reload.load_config({_config_cases._DECOMPOSE_ENV: spelling})
                self.assertTrue(config.DECOMPOSE)

    def test_falsy_spellings_disable(self) -> None:
        for spelling in (_config_cases._DISABLED_ENV, "false", "no", _config_cases._OFF):
            with self.subTest(value=spelling):
                config = _reload.load_config({_config_cases._DECOMPOSE_ENV: spelling})
                self.assertFalse(config.DECOMPOSE)

    def test_typo_defaults_to_off(self) -> None:
        # Strict parser: any unrecognized value disables decomposition.
        config = _reload.load_config({_config_cases._DECOMPOSE_ENV: "enabled"})
        self.assertFalse(config.DECOMPOSE)


class ExposeTrackedReposConfigTest(unittest.TestCase):
    """The EXPOSE_TRACKED_REPOS kill switch defaults on (but is inert for
    single-repo hosts, where the context builder gates on `len(specs) > 1`).
    Parsed exactly like DECOMPOSE / SQUASH_ON_APPROVAL: truthy spellings keep
    it on, explicit off / typos disable it.
    """

    def test_default_is_on(self) -> None:
        config = _reload.load_config()
        self.assertTrue(config.EXPOSE_TRACKED_REPOS)

    def test_explicit_off(self) -> None:
        config = _reload.load_config({_config_cases._EXPOSE_REPOS_ENV: _config_cases._OFF})
        self.assertFalse(config.EXPOSE_TRACKED_REPOS)

    def test_truthy_spellings_keep_on(self) -> None:
        for spelling in (
            "on",
            "ON",
            " on ",
            _config_cases._ENABLED_ENV,
            "true",
            "True",
            "yes",
        ):
            with self.subTest(value=spelling):
                config = _reload.load_config({_config_cases._EXPOSE_REPOS_ENV: spelling})
                self.assertTrue(config.EXPOSE_TRACKED_REPOS)

    def test_falsy_spellings_disable(self) -> None:
        for spelling in (_config_cases._DISABLED_ENV, "false", "no", _config_cases._OFF):
            with self.subTest(value=spelling):
                config = _reload.load_config({_config_cases._EXPOSE_REPOS_ENV: spelling})
                self.assertFalse(config.EXPOSE_TRACKED_REPOS)

    def test_typo_defaults_to_off(self) -> None:
        # Strict parser: any unrecognized value disables the disclosure.
        config = _reload.load_config({_config_cases._EXPOSE_REPOS_ENV: "enabled"})
        self.assertFalse(config.EXPOSE_TRACKED_REPOS)


class InReviewDebounceConfigTest(unittest.TestCase):
    def test_default_is_ten_minutes(self) -> None:
        config = _reload.load_config()
        self.assertEqual(
            config.IN_REVIEW_DEBOUNCE_SECONDS,
            _config_cases._DEFAULT_DEBOUNCE_SECONDS,
        )

    def test_env_override(self) -> None:
        config = _reload.load_config(
            {
                "IN_REVIEW_DEBOUNCE_SECONDS": str(_config_cases._OVERRIDE_DEBOUNCE_SECONDS),
            }
        )
        self.assertEqual(
            config.IN_REVIEW_DEBOUNCE_SECONDS,
            _config_cases._OVERRIDE_DEBOUNCE_SECONDS,
        )


class MaxRetriesPerDayConfigTest(unittest.TestCase):
    def test_default_is_three(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 3)

    def test_env_override(self) -> None:
        config = _reload.load_config({"MAX_RETRIES_PER_DAY": "7"})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 7)

    def test_zero_means_unbounded(self) -> None:
        config = _reload.load_config({"MAX_RETRIES_PER_DAY": _config_cases._DISABLED_ENV})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 0)


class AllowedIssueAuthorsConfigTest(unittest.TestCase):
    """Author-allowlist for unlabeled-issue pickup. Empty (default) disables
    the filter so existing single-user setups keep working; a populated list
    guards against random users on public repos triggering agent runs."""

    def test_default_is_empty_tuple(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, ())

    def test_parses_comma_separated(self) -> None:
        config = _reload.load_config({"ALLOWED_ISSUE_AUTHORS": "alice,bob"})
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, (_config_cases._ALICE, _config_cases._BOB))

    def test_strips_spaces_at_signs_and_duplicates(self) -> None:
        config = _reload.load_config({"ALLOWED_ISSUE_AUTHORS": " @alice, bob, ,alice,@carol "})
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, (_config_cases._ALICE, _config_cases._BOB, "carol"))


class MaxConflictRoundsConfigTest(unittest.TestCase):
    """`MAX_CONFLICT_ROUNDS` parses identically to `MAX_REVIEW_ROUNDS`:
    integer, defaults to 3, env override wins.
    """

    def test_default_is_three(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 3)

    def test_env_override(self) -> None:
        config = _reload.load_config({"MAX_CONFLICT_ROUNDS": "7"})
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 7)
