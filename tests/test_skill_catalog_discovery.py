# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Local skill and Codex tool discovery tests."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import skill_catalog

from tests.skill_catalog_test_support import _make_skill


_DEVELOP_SKILL = "develop"
_REVIEW_SKILL = "review"
_IMAGEGEN_SKILL = "imagegen"
_AGENT_SKILLS_ROOT = ".agents/skills"
_SKILLS_DIR = "skills"
_CODEX_HOME_ENV = "CODEX_HOME"


class DiscoverLocalSkillsTest(unittest.TestCase):
    def test_scans_both_repo_roots_and_dedups(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            cwd = Path(repo_dir)
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _DEVELOP_SKILL)
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _REVIEW_SKILL)
            _make_skill(cwd / ".claude/skills", _DEVELOP_SKILL)
            _make_skill(cwd / ".claude/skills", "extra")
            with patch.dict(
                os.environ,
                {_CODEX_HOME_ENV: str(cwd / "no-home")},
            ):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(
            names,
            (_DEVELOP_SKILL, _REVIEW_SKILL, "extra"),
        )

    def test_includes_codex_home_global_skills(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            cwd = Path(home_dir) / "wt"
            home = Path(home_dir) / "codexhome"
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _REVIEW_SKILL)
            _make_skill(home / _SKILLS_DIR, "global-skill")
            _make_skill(
                home / _SKILLS_DIR / ".system",
                _IMAGEGEN_SKILL,
            )
            with patch.dict(os.environ, {_CODEX_HOME_ENV: str(home)}):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(
            names,
            (_REVIEW_SKILL, "global-skill", _IMAGEGEN_SKILL),
        )

    def test_global_system_builtins_surface_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as system_dir:
            cwd = Path(system_dir) / "wt"
            home = Path(system_dir) / "codexhome"
            for name in (
                _IMAGEGEN_SKILL,
                "openai-docs",
                "skill-installer",
            ):
                _make_skill(home / _SKILLS_DIR / ".system", name)
            with patch.dict(os.environ, {_CODEX_HOME_ENV: str(home)}):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(
            names,
            (_IMAGEGEN_SKILL, "openai-docs", "skill-installer"),
        )

    def test_repo_skill_precedes_global_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as duplicate_dir:
            cwd = Path(duplicate_dir) / "wt"
            home = Path(duplicate_dir) / "codexhome"
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _REVIEW_SKILL)
            _make_skill(home / _SKILLS_DIR, _REVIEW_SKILL)
            with patch.dict(os.environ, {_CODEX_HOME_ENV: str(home)}):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(names, (_REVIEW_SKILL,))

    def test_only_direct_children_with_skill_md_count(self) -> None:
        with tempfile.TemporaryDirectory() as direct_dir:
            cwd = Path(direct_dir)
            root = cwd / _AGENT_SKILLS_ROOT
            _make_skill(root, _DEVELOP_SKILL)
            (root / "empty").mkdir(parents=True, exist_ok=True)
            deep = root / ".system" / _IMAGEGEN_SKILL
            deep.mkdir(parents=True, exist_ok=True)
            (deep / "SKILL.md").write_text("x", encoding="utf-8")
            with patch.dict(
                os.environ,
                {_CODEX_HOME_ENV: str(cwd / "no-home")},
            ):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(names, (_DEVELOP_SKILL,))

    def test_missing_roots_yield_empty_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as missing_dir:
            cwd = Path(missing_dir) / "does-not-exist"
            missing_home = {
                _CODEX_HOME_ENV: str(Path(missing_dir) / "nope"),
            }
            with patch.dict(os.environ, missing_home):
                self.assertEqual(
                    skill_catalog.discover_local_skills(cwd),
                    (),
                )


class DiscoverCodexToolsTest(unittest.TestCase):
    def test_returns_nonempty_baseline(self) -> None:
        tools = skill_catalog.discover_codex_tools()
        self.assertIsInstance(tools, tuple)
        self.assertEqual(tools, skill_catalog._CODEX_OFFERED_TOOLS)
        self.assertIn("exec_command", tools)
        self.assertIn("web_search", tools)
        self.assertTrue(all(
            isinstance(tool, str)
            for tool in tools
        ))
        self.assertTrue(all(tools))
        self.assertEqual(len(tools), len(set(tools)))
