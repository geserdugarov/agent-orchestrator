# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""GitHub credential resolution tests."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import credentials

_REPO_SLUG = "owner/repo"
_TOKEN_ENV = "GITHUB_TOKEN"
_TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"
_FILE_TOKEN = "file-token"
_CONFIG_DIR = ".config"
_TOKEN_FILE = "token"


def _write_token(token_file: Path, token: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token)


class ResolveGithubTokenTest(unittest.TestCase):
    """The GitHub token is never read from the repository checkout: the
    process environment wins, and the only fallback is a token file outside
    REPO_ROOT -- `~/.config/<owner>/<repo>/token` derived from the repo slug,
    or whatever ORCHESTRATOR_TOKEN_FILE names. A token file that cannot be
    read degrades to "no token" so the caller keeps its own failure handling.
    """

    def test_process_environment_token_wins(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            _write_token(
                home_path / _CONFIG_DIR / _REPO_SLUG / _TOKEN_FILE,
                _FILE_TOKEN,
            )
            with (
                patch.dict(os.environ, {_TOKEN_ENV: "  env-token  "}, clear=True),
                patch.object(Path, "home", return_value=home_path),
            ):
                resolved = credentials.resolve_github_token(_REPO_SLUG)
        self.assertEqual(resolved, "env-token")

    def test_slug_derived_token_file_is_the_fallback(self) -> None:
        # The default path is derived from REPO and lives outside REPO_ROOT,
        # which is what keeps the token out of reach of an agent that can
        # read the orchestrator checkout.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            _write_token(
                home_path / _CONFIG_DIR / _REPO_SLUG / _TOKEN_FILE,
                f"{_FILE_TOKEN}\n",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(Path, "home", return_value=home_path),
            ):
                resolved = credentials.resolve_github_token(_REPO_SLUG)
        self.assertEqual(resolved, _FILE_TOKEN)

    def test_token_file_override_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as token_dir:
            token_file = Path(token_dir) / _TOKEN_FILE
            _write_token(token_file, f" {_FILE_TOKEN} ")
            environment = {_TOKEN_FILE_ENV: str(token_file)}
            with patch.dict(os.environ, environment, clear=True):
                resolved = credentials.resolve_github_token(_REPO_SLUG)
        self.assertEqual(resolved, _FILE_TOKEN)

    def test_missing_token_file_resolves_to_no_token(self) -> None:
        # A host that has not been provisioned yet is a normal state, so the
        # absent file stays silent; the caller reports the missing token.
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as token_dir:
            environment = {_TOKEN_FILE_ENV: str(Path(token_dir) / _TOKEN_FILE)}
            with patch.dict(os.environ, environment, clear=True), redirect_stderr(errors):
                resolved = credentials.resolve_github_token(_REPO_SLUG)
        self.assertEqual(resolved, "")
        self.assertEqual(errors.getvalue(), "")

    def test_unreadable_token_file_warns(self) -> None:
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as token_dir:
            # A directory where the token file belongs stands in for any
            # unreadable path: it fails with an OSError that is not a
            # missing file, which is the branch that has to warn.
            with patch.dict(os.environ, {_TOKEN_FILE_ENV: token_dir}, clear=True), redirect_stderr(errors):
                resolved = credentials.resolve_github_token(_REPO_SLUG)
            self.assertIn(token_dir, errors.getvalue())
        self.assertEqual(resolved, "")
        self.assertIn("could not read token file", errors.getvalue())
