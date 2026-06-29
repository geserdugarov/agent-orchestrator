# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import analytics, config, skill_catalog


def _spec(
    *,
    slug: str = "geserdugarov/agent-orchestrator",
    target_root: str = "/tmp/orchestrator-skill-catalog-target",
    base_branch: str = "main",
    remote_name: str = "origin",
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


class ExtractSkillCatalogTest(unittest.TestCase):
    """`_extract_skill_catalog` keeps only direct `<root>/<name>/SKILL.md`
    definitions, dedupes by name across roots, and preserves source paths.
    """

    def test_agents_extraction(self) -> None:
        # `.agents/skills/<name>/SKILL.md` definitions are extracted; the
        # name is the single segment between the root and the SKILL.md file.
        paths = [
            ".agents/skills/develop/SKILL.md",
            ".agents/skills/review/SKILL.md",
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, ["develop", "review"])
        self.assertEqual(
            skill_paths,
            {
                "develop": [".agents/skills/develop/SKILL.md"],
                "review": [".agents/skills/review/SKILL.md"],
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
            ".claude/skills/review/SKILL.md",
            ".agents/skills/review/SKILL.md",
            ".agents/skills/develop/SKILL.md",
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, ["develop", "review"])
        self.assertEqual(
            skill_paths["review"],
            [
                ".agents/skills/review/SKILL.md",
                ".claude/skills/review/SKILL.md",
            ],
        )
        self.assertEqual(
            skill_paths["develop"], [".agents/skills/develop/SKILL.md"],
        )

    def test_nested_and_unrelated_paths_ignored(self) -> None:
        # Only a direct `<root>/<name>/SKILL.md` counts: a built-in nested
        # under `.system`, a non-SKILL file, a SKILL.md directly under the
        # root with no name segment, and a path outside the known roots are
        # all rejected. Blank lines are skipped.
        paths = [
            ".claude/skills/.system/imagegen/SKILL.md",
            ".agents/skills/review/SKILL.md",
            ".agents/skills/review/README.md",
            ".agents/skills/SKILL.md",
            ".agents/skills/nested/sub/SKILL.md",
            "docs/skills/leaked/SKILL.md",
            "",
        ]
        skills, skill_paths = skill_catalog._extract_skill_catalog(paths)
        self.assertEqual(skills, ["review"])
        self.assertEqual(
            skill_paths, {"review": [".agents/skills/review/SKILL.md"]},
        )

    def test_empty_input_yields_empty_catalog(self) -> None:
        skills, skill_paths = skill_catalog._extract_skill_catalog([])
        self.assertEqual(skills, [])
        self.assertEqual(skill_paths, {})


class RecordRepoSkillCatalogShapeTest(unittest.TestCase):
    """`analytics.record_repo_skill_catalog` builds a repo-level
    `repo_skill_catalog` record carrying the catalog in extras.
    """

    def _capture(self) -> list[dict]:
        captured: list[dict] = []
        patcher = patch.object(
            analytics, "append_record", captured.append,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured

    def test_record_shape(self) -> None:
        captured = self._capture()
        analytics.record_repo_skill_catalog(
            repo="geserdugarov/agent-orchestrator",
            base_branch="main",
            remote_name="origin",
            skills_available=["develop", "review"],
            skill_paths={
                "develop": [".agents/skills/develop/SKILL.md"],
                "review": [
                    ".agents/skills/review/SKILL.md",
                    ".claude/skills/review/SKILL.md",
                ],
            },
        )
        self.assertEqual(len(captured), 1)
        rec = captured[0]
        self.assertEqual(rec["event"], "repo_skill_catalog")
        # Repo-level event: issue is the sentinel 0 so the record still
        # satisfies the ts/repo/issue/event envelope without a DDL change.
        self.assertEqual(rec["issue"], 0)
        self.assertEqual(rec["repo"], "geserdugarov/agent-orchestrator")
        self.assertEqual(rec["base_branch"], "main")
        self.assertEqual(rec["remote_name"], "origin")
        self.assertEqual(rec["skills_available"], ["develop", "review"])
        self.assertEqual(
            rec["skill_paths"]["review"],
            [
                ".agents/skills/review/SKILL.md",
                ".claude/skills/review/SKILL.md",
            ],
        )
        self.assertIsInstance(rec["ts"], str)
        self.assertNotIn("stage", rec)

    def test_empty_catalog_keeps_skills_available_drops_skill_paths(
        self,
    ) -> None:
        # An empty catalog still records `skills_available: []` (the
        # "scanned, found none" signal); `skill_paths` is dropped when None.
        captured = self._capture()
        analytics.record_repo_skill_catalog(
            repo="geserdugarov/agent-orchestrator",
            base_branch="main",
            remote_name="origin",
            skills_available=[],
            skill_paths=None,
        )
        rec = captured[0]
        self.assertEqual(rec["skills_available"], [])
        self.assertNotIn("skill_paths", rec)


class ListSkillTreeTest(unittest.TestCase):
    """`_list_skill_tree` invokes git against the spec's base ref and is
    fail-open on a missing clone or a git error.
    """

    def test_missing_target_root_returns_none(self) -> None:
        spec = _spec(target_root="/tmp/does-not-exist-skill-catalog-xyz")
        with patch.object(skill_catalog, "_git") as git:
            self.assertIsNone(skill_catalog._list_skill_tree(spec))
        git.assert_not_called()

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
            with patch.object(
                skill_catalog, "_git", return_value=_completed(out),
            ) as git:
                lines = skill_catalog._list_skill_tree(spec)
        self.assertEqual(
            lines,
            [
                ".agents/skills/develop/SKILL.md",
                ".claude/skills/review/SKILL.md",
            ],
        )
        args, kwargs = git.call_args
        self.assertEqual(
            args,
            (
                "ls-tree", "-r", "--name-only", "upstream/release",
                ".agents/skills", ".claude/skills",
            ),
        )
        self.assertEqual(kwargs["cwd"], spec.target_root)

    def test_git_failure_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            spec = _spec(target_root=td)
            with patch.object(
                skill_catalog, "_git",
                return_value=_completed(returncode=128, stderr="bad ref"),
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
            ".claude/skills/review/SKILL.md",
            ".agents/skills/review/SKILL.md",
            ".agents/skills/develop/SKILL.md",
        ]
        with patch.object(
            skill_catalog, "_list_skill_tree", return_value=paths,
        ), patch.object(
            analytics, "record_repo_skill_catalog",
        ) as record:
            skill_catalog._emit_repo_skill_catalog(spec)
        record.assert_called_once_with(
            repo="acme/widgets",
            base_branch="trunk",
            remote_name="upstream",
            skills_available=["develop", "review"],
            skill_paths={
                "develop": [".agents/skills/develop/SKILL.md"],
                "review": [
                    ".agents/skills/review/SKILL.md",
                    ".claude/skills/review/SKILL.md",
                ],
            },
        )

    def test_empty_catalog_passes_none_skill_paths(self) -> None:
        spec = _spec()
        with patch.object(
            skill_catalog, "_list_skill_tree", return_value=[],
        ), patch.object(
            analytics, "record_repo_skill_catalog",
        ) as record:
            skill_catalog._emit_repo_skill_catalog(spec)
        _, kwargs = record.call_args
        self.assertEqual(kwargs["skills_available"], [])
        self.assertIsNone(kwargs["skill_paths"])

    def test_unavailable_tree_records_nothing(self) -> None:
        spec = _spec()
        with patch.object(
            skill_catalog, "_list_skill_tree", return_value=None,
        ), patch.object(
            analytics, "record_repo_skill_catalog",
        ) as record:
            skill_catalog._emit_repo_skill_catalog(spec)
        record.assert_not_called()

    def test_failure_is_swallowed(self) -> None:
        spec = _spec()
        with patch.object(
            skill_catalog, "_list_skill_tree",
            side_effect=RuntimeError("boom"),
        ), patch.object(
            analytics, "record_repo_skill_catalog",
        ) as record:
            # Must not raise -- catalog collection is fail-open.
            skill_catalog._emit_repo_skill_catalog(spec)
        record.assert_not_called()


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
