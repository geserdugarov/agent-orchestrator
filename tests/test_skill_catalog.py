# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import analytics, config, skill_catalog


_TEST_REPO_SLUG = "geserdugarov/agent-orchestrator"
_TEST_BASE_BRANCH = "main"
_TEST_REMOTE_NAME = "origin"
_DEVELOP_SKILL = "develop"
_REVIEW_SKILL = "review"
_IMAGEGEN_SKILL = "imagegen"
_AGENT_SKILLS_ROOT = ".agents/skills"
_SKILLS_DIR = "skills"
_AGENT_DEVELOP_SKILL_PATH = ".agents/skills/develop/SKILL.md"
_AGENT_REVIEW_SKILL_PATH = ".agents/skills/review/SKILL.md"
_CLAUDE_REVIEW_SKILL_PATH = ".claude/skills/review/SKILL.md"
_LIST_SKILL_TREE_METHOD = "_list_skill_tree"
_RECORD_CATALOG_METHOD = "record_repo_skill_catalog"
_CODEX_HOME_ENV = "CODEX_HOME"
_BAD_REF_EXIT_CODE = 128


def _spec(
    *,
    slug: str = _TEST_REPO_SLUG,
    target_root: str = "/tmp/orchestrator-skill-catalog-target",
    base_branch: str = _TEST_BASE_BRANCH,
    remote_name: str = _TEST_REMOTE_NAME,
) -> config.RepoSpec:
    return config.RepoSpec(
        slug=slug,
        target_root=Path(target_root),
        base_branch=base_branch,
        remote_name=remote_name,
    )


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["git", "ls-tree"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _capture_analytics_records(case: unittest.TestCase) -> list[dict]:
    captured: list[dict] = []
    patcher = patch.object(analytics, "append_record", captured.append)
    patcher.start()
    case.addCleanup(patcher.stop)
    return captured


class ExtractSkillCatalogTest(unittest.TestCase):
    """`_extract_skill_catalog` keeps only direct `<root>/<name>/SKILL.md`
    definitions, dedupes by name across roots, and preserves source paths.
    """

    def test_agents_extraction(self) -> None:
        # `.agents/skills/<name>/SKILL.md` definitions are extracted; the
        # name is the single segment between the root and the SKILL.md file.
        paths = [
            _AGENT_DEVELOP_SKILL_PATH,
            _AGENT_REVIEW_SKILL_PATH,
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, [_DEVELOP_SKILL, _REVIEW_SKILL])
        self.assertEqual(
            skill_paths,
            {
                _DEVELOP_SKILL: [_AGENT_DEVELOP_SKILL_PATH],
                _REVIEW_SKILL: [_AGENT_REVIEW_SKILL_PATH],
            },
        )

    def test_claude_extraction(self) -> None:
        # `.claude/skills/<name>/SKILL.md` definitions are extracted the
        # same way as the `.agents` root.
        paths = [
            ".claude/skills/verify/SKILL.md",
            ".claude/skills/run/SKILL.md",
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, ["run", "verify"])
        self.assertEqual(
            skill_paths,
            {
                "run": [".claude/skills/run/SKILL.md"],
                "verify": [".claude/skills/verify/SKILL.md"],
            },
        )

    def test_cross_root_dedupe(self) -> None:
        # A skill defined under both roots appears once in the names list,
        # but every source path that produced it is preserved (sorted).
        paths = [
            _CLAUDE_REVIEW_SKILL_PATH,
            _AGENT_REVIEW_SKILL_PATH,
            _AGENT_DEVELOP_SKILL_PATH,
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, [_DEVELOP_SKILL, _REVIEW_SKILL])
        self.assertEqual(
            skill_paths[_REVIEW_SKILL],
            [
                _AGENT_REVIEW_SKILL_PATH,
                _CLAUDE_REVIEW_SKILL_PATH,
            ],
        )
        self.assertEqual(
            skill_paths[_DEVELOP_SKILL], [_AGENT_DEVELOP_SKILL_PATH],
        )

    def test_nested_and_unrelated_paths_ignored(self) -> None:
        # Only a direct `<root>/<name>/SKILL.md` counts: a built-in nested
        # under `.system`, a non-SKILL file, a SKILL.md directly under the
        # root with no name segment, and a path outside the known roots are
        # all rejected. Blank lines are skipped.
        paths = [
            ".claude/skills/.system/imagegen/SKILL.md",
            _AGENT_REVIEW_SKILL_PATH,
            ".agents/skills/review/README.md",
            ".agents/skills/SKILL.md",
            ".agents/skills/nested/sub/SKILL.md",
            "docs/skills/leaked/SKILL.md",
            "",
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, [_REVIEW_SKILL])
        self.assertEqual(
            skill_paths, {_REVIEW_SKILL: [_AGENT_REVIEW_SKILL_PATH]},
        )

    def test_empty_input_yields_empty_catalog(self) -> None:
        skills, skill_paths = skill_catalog._extract_skill_catalog([])
        self.assertEqual(skills, [])
        self.assertEqual(skill_paths, {})


class RecordRepoSkillCatalogShapeTest(unittest.TestCase):
    """`analytics.record_repo_skill_catalog` builds a repo-level
    `repo_skill_catalog` record carrying the catalog in extras.
    """

    def test_record_shape(self) -> None:
        captured = _capture_analytics_records(self)
        analytics.record_repo_skill_catalog(
            repo=_TEST_REPO_SLUG,
            base_branch=_TEST_BASE_BRANCH,
            remote_name=_TEST_REMOTE_NAME,
            skills_available=[_DEVELOP_SKILL, _REVIEW_SKILL],
            skill_paths={
                _DEVELOP_SKILL: [_AGENT_DEVELOP_SKILL_PATH],
                _REVIEW_SKILL: [
                    _AGENT_REVIEW_SKILL_PATH,
                    _CLAUDE_REVIEW_SKILL_PATH,
                ],
            },
        )
        self.assertEqual(len(captured), 1)
        record = captured[0]
        self.assertEqual(record["event"], "repo_skill_catalog")
        # Repo-level event: issue is the sentinel 0 so the record still
        # satisfies the ts/repo/issue/event envelope without a DDL change.
        self.assertEqual(record["issue"], 0)
        self.assertEqual(record["repo"], _TEST_REPO_SLUG)
        self.assertEqual(record["base_branch"], _TEST_BASE_BRANCH)
        self.assertEqual(record["remote_name"], _TEST_REMOTE_NAME)
        self.assertEqual(
            record["skills_available"],
            [_DEVELOP_SKILL, _REVIEW_SKILL],
        )
        self.assertEqual(
            record["skill_paths"][_REVIEW_SKILL],
            [
                _AGENT_REVIEW_SKILL_PATH,
                _CLAUDE_REVIEW_SKILL_PATH,
            ],
        )
        self.assertIsInstance(record["ts"], str)
        self.assertNotIn("stage", record)

    def test_empty_catalog_keeps_skills_drops_paths(
        self,
    ) -> None:
        # An empty catalog still records `skills_available: []` (the
        # "scanned, found none" signal); `skill_paths` is dropped when None.
        captured = _capture_analytics_records(self)
        analytics.record_repo_skill_catalog(
            repo=_TEST_REPO_SLUG,
            base_branch=_TEST_BASE_BRANCH,
            remote_name=_TEST_REMOTE_NAME,
            skills_available=[],
            skill_paths=None,
        )
        record = captured[0]
        self.assertEqual(record["skills_available"], [])
        self.assertNotIn("skill_paths", record)


class ListSkillTreeTest(unittest.TestCase):
    """`_list_skill_tree` invokes git against the spec's base ref and is
    fail-open on a missing clone or a git error.
    """

    def test_missing_target_root_returns_none(self) -> None:
        spec = _spec(target_root="/tmp/does-not-exist-skill-catalog-xyz")
        git_mock = MagicMock()
        with patch.object(skill_catalog, "_git", git_mock):
            self.assertIsNone(skill_catalog._list_skill_tree(spec))
        git_mock.assert_not_called()

    def test_ls_tree_command_and_parse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            spec = _spec(
                target_root=td, remote_name="upstream", base_branch="release",
            )
            out = (
                ".agents/skills/develop/SKILL.md\n"
                ".claude/skills/review/SKILL.md\n"
                "\n"
            )
            git_mock = MagicMock(return_value=_completed(out))
            with patch.object(skill_catalog, "_git", git_mock):
                lines = skill_catalog._list_skill_tree(spec)
        self.assertEqual(
            lines,
            [
                _AGENT_DEVELOP_SKILL_PATH,
                _CLAUDE_REVIEW_SKILL_PATH,
            ],
        )
        args, kwargs = git_mock.call_args
        self.assertEqual(
            args,
            (
                "ls-tree", "-r", "--name-only", "upstream/release",
                _AGENT_SKILLS_ROOT, ".claude/skills",
            ),
        )
        self.assertEqual(kwargs["cwd"], spec.target_root)

    def test_git_failure_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            spec = _spec(target_root=td)
            with patch.object(
                skill_catalog, "_git",
                return_value=_completed(
                    returncode=_BAD_REF_EXIT_CODE,
                    stderr="bad ref",
                ),
            ):
                self.assertIsNone(skill_catalog._list_skill_tree(spec))


class EmitRepoSkillCatalogTest(unittest.TestCase):
    """`_emit_repo_skill_catalog` wires spec fields into the analytics
    record and never raises out of the producer.
    """

    def test_wires_spec_fields_into_record(self) -> None:
        spec = _spec(
            slug="acme/widgets", remote_name="upstream", base_branch="trunk",
        )
        paths = [
            _CLAUDE_REVIEW_SKILL_PATH,
            _AGENT_REVIEW_SKILL_PATH,
            _AGENT_DEVELOP_SKILL_PATH,
        ]
        record_mock = MagicMock()
        with patch.object(
            skill_catalog, _LIST_SKILL_TREE_METHOD, return_value=paths,
        ), patch.object(
            analytics, _RECORD_CATALOG_METHOD, record_mock,
        ):
            skill_catalog._emit_repo_skill_catalog(spec)
        record_mock.assert_called_once_with(
            repo="acme/widgets",
            base_branch="trunk",
            remote_name="upstream",
            skills_available=[_DEVELOP_SKILL, _REVIEW_SKILL],
            skill_paths={
                _DEVELOP_SKILL: [_AGENT_DEVELOP_SKILL_PATH],
                _REVIEW_SKILL: [
                    _AGENT_REVIEW_SKILL_PATH,
                    _CLAUDE_REVIEW_SKILL_PATH,
                ],
            },
        )

    def test_empty_catalog_passes_none_skill_paths(self) -> None:
        spec = _spec()
        record_mock = MagicMock()
        with patch.object(
            skill_catalog, _LIST_SKILL_TREE_METHOD, return_value=[],
        ), patch.object(
            analytics, _RECORD_CATALOG_METHOD, record_mock,
        ):
            skill_catalog._emit_repo_skill_catalog(spec)
        _, kwargs = record_mock.call_args
        self.assertEqual(kwargs["skills_available"], [])
        self.assertIsNone(kwargs["skill_paths"])

    def test_unavailable_tree_records_nothing(self) -> None:
        spec = _spec()
        record_mock = MagicMock()
        with patch.object(
            skill_catalog, _LIST_SKILL_TREE_METHOD, return_value=None,
        ), patch.object(
            analytics, _RECORD_CATALOG_METHOD, record_mock,
        ):
            skill_catalog._emit_repo_skill_catalog(spec)
        record_mock.assert_not_called()

    def test_failure_is_swallowed(self) -> None:
        spec = _spec()
        record_mock = MagicMock()
        with patch.object(
            skill_catalog, _LIST_SKILL_TREE_METHOD,
            side_effect=RuntimeError("boom"),
        ), patch.object(
            analytics, _RECORD_CATALOG_METHOD, record_mock,
        ):
            # Must not raise -- catalog collection is fail-open.
            skill_catalog._emit_repo_skill_catalog(spec)
        record_mock.assert_not_called()


class TickEmitsRepoSkillCatalogTest(unittest.TestCase):
    """`workflow.tick` drives `_emit_repo_skill_catalog` once per tick."""

    def test_tick_calls_emit_once(self) -> None:
        from orchestrator import workflow
        from tests.fakes import FakeGitHubClient, make_issue
        from tests.workflow_helpers import _TEST_SPEC

        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        emit = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees"), \
                patch.object(workflow, "_process_issue"), \
                patch.object(workflow, "_emit_repo_skill_catalog", emit):
            workflow.tick(gh, _TEST_SPEC)
        emit.assert_called_once_with(_TEST_SPEC)


def _make_skill(root: Path, name: str) -> None:
    """Create a `<root>/<name>/SKILL.md` skill definition under `root`."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# skill\n", encoding="utf-8")


class DiscoverLocalSkillsTest(unittest.TestCase):
    """`discover_local_skills` scans the worktree repo roots (direct children)
    plus the global `$CODEX_HOME/skills` root -- there also descending the
    `.system` container of codex's built-in skills -- deduped across roots,
    fail-open on missing roots."""

    def test_scans_both_repo_roots_and_dedups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _DEVELOP_SKILL)
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _REVIEW_SKILL)
            # A name defined in the second repo root appears once.
            _make_skill(cwd / ".claude/skills", _DEVELOP_SKILL)
            _make_skill(cwd / ".claude/skills", "extra")
            with patch.dict(
                os.environ,
                {_CODEX_HOME_ENV: str(cwd / "no-home")},
            ):
                names = skill_catalog.discover_local_skills(cwd)
        # Sorted within each root, roots in order (`.agents/skills` then
        # `.claude/skills`), deduped first-seen: `develop`/`review` from the
        # first root, then `extra` from the second (`develop` already seen).
        self.assertEqual(names, (_DEVELOP_SKILL, _REVIEW_SKILL, "extra"))

    def test_includes_codex_home_global_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "wt"
            home = Path(td) / "codexhome"
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _REVIEW_SKILL)
            _make_skill(home / _SKILLS_DIR, "global-skill")
            # codex's built-ins live under the global root's `.system` container.
            _make_skill(home / _SKILLS_DIR / ".system", _IMAGEGEN_SKILL)
            with patch.dict(os.environ, {_CODEX_HOME_ENV: str(home)}):
                names = skill_catalog.discover_local_skills(cwd)
        # Repo-local first (its scan runs before the global root), then the
        # global root's direct + `.system` skills sorted together.
        self.assertEqual(
            names,
            (_REVIEW_SKILL, "global-skill", _IMAGEGEN_SKILL),
        )

    def test_global_system_builtins_surface_by_name(self) -> None:
        # codex auto-loads its built-in skills from the global root's `.system`
        # container even with no user skills placed directly under the root, so
        # they surface under their own `<name>` (imagegen, openai-docs, ...).
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "wt"
            home = Path(td) / "codexhome"
            for name in (_IMAGEGEN_SKILL, "openai-docs", "skill-installer"):
                _make_skill(home / _SKILLS_DIR / ".system", name)
            with patch.dict(os.environ, {_CODEX_HOME_ENV: str(home)}):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(
            names,
            (_IMAGEGEN_SKILL, "openai-docs", "skill-installer"),
        )

    def test_repo_skill_precedes_global_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "wt"
            home = Path(td) / "codexhome"
            _make_skill(cwd / _AGENT_SKILLS_ROOT, _REVIEW_SKILL)
            _make_skill(home / _SKILLS_DIR, _REVIEW_SKILL)
            with patch.dict(os.environ, {_CODEX_HOME_ENV: str(home)}):
                names = skill_catalog.discover_local_skills(cwd)
        self.assertEqual(names, (_REVIEW_SKILL,))

    def test_only_direct_children_with_skill_md_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            root = cwd / _AGENT_SKILLS_ROOT
            _make_skill(root, _DEVELOP_SKILL)
            # A dir without a SKILL.md is not a skill.
            (root / "empty").mkdir(parents=True, exist_ok=True)
            # A `.system` container under a *repo* root is not descended: only
            # the global codex root loads its `.system` built-ins, so a repo's
            # `.system/imagegen/SKILL.md` does not surface here.
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
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td) / "does-not-exist"
            missing_home = {_CODEX_HOME_ENV: str(Path(td) / "nope")}
            with patch.dict(os.environ, missing_home):
                self.assertEqual(skill_catalog.discover_local_skills(cwd), ())


class DiscoverCodexToolsTest(unittest.TestCase):
    """`discover_codex_tools` returns the best-effort offered-tools baseline
    used to backfill codex trajectory records (codex's stream exposes no
    offered-tools frame)."""

    def test_returns_nonempty_baseline(self) -> None:
        tools = skill_catalog.discover_codex_tools()
        self.assertIsInstance(tools, tuple)
        self.assertEqual(tools, skill_catalog._CODEX_OFFERED_TOOLS)
        # Anchors on tools captured from a real codex exec request payload.
        self.assertIn("exec_command", tools)
        self.assertIn("web_search", tools)
        # De-duplicated, non-empty names only.
        self.assertTrue(all(isinstance(tool, str) for tool in tools))
        self.assertTrue(all(tools))
        self.assertEqual(len(tools), len(set(tools)))


if __name__ == "__main__":
    unittest.main()
