# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import analytics, skill_catalog

from tests.skill_catalog_test_support import (
    _capture_analytics_records,
    _catalog_identity,
    _completed,
    _spec,
)


_TEST_REPO_SLUG = "geserdugarov/agent-orchestrator"
_TEST_BASE_BRANCH = "main"
_TEST_REMOTE_NAME = "origin"
_DEVELOP_SKILL = "develop"
_REVIEW_SKILL = "review"
_AGENT_SKILLS_ROOT = ".agents/skills"
_AGENT_DEVELOP_SKILL_PATH = ".agents/skills/develop/SKILL.md"
_AGENT_REVIEW_SKILL_PATH = ".agents/skills/review/SKILL.md"
_CLAUDE_REVIEW_SKILL_PATH = ".claude/skills/review/SKILL.md"
_LIST_SKILL_TREE_METHOD = "_list_skill_tree"
_RECORD_CATALOG_METHOD = "record_repo_skill_catalog"
_BAD_REF_EXIT_CODE = 128


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
        self.assertEqual(
            _catalog_identity(record),
            (
                "repo_skill_catalog",
                0,
                _TEST_REPO_SLUG,
                _TEST_BASE_BRANCH,
                _TEST_REMOTE_NAME,
            ),
        )
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
            git_mock = MagicMock(return_value=_completed(
                ".agents/skills/develop/SKILL.md\n"
                ".claude/skills/review/SKILL.md\n"
                "\n",
            ))
            with patch.object(skill_catalog, "_git", git_mock):
                lines = skill_catalog._list_skill_tree(spec)
        self.assertEqual(
            lines,
            [
                _AGENT_DEVELOP_SKILL_PATH,
                _CLAUDE_REVIEW_SKILL_PATH,
            ],
        )
        git_call = git_mock.call_args
        self.assertEqual(
            git_call.args,
            (
                "ls-tree", "-r", "--name-only", "upstream/release",
                _AGENT_SKILLS_ROOT, ".claude/skills",
            ),
        )
        self.assertEqual(git_call.kwargs["cwd"], spec.target_root)

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


if __name__ == "__main__":
    unittest.main()
