# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The `develop` skill's model-facing trigger anchor must name an action the
implementer prompt actually performs.

Claude decides whether to pull a skill from its `description` frontmatter, not
its body. The implementer prompt tells the agent to COMMIT and explicitly NOT
to push or open the PR (the orchestrator does that), so an anchor phrased around
"opening a PR" points at an action the developer never takes and the skill goes
unused -- the low `develop` trigger rate. These tests pin the anchor to
committing and pin the implementer prompt to the commit-not-push contract it
must stay aligned with, so the two cannot silently drift back apart.
"""
from __future__ import annotations

import unittest
from itertools import takewhile
from pathlib import Path

from orchestrator import workflow_messages

from tests.fakes import make_issue
from tests.workflow_helpers import _TEST_SPEC

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Both roots the skill-catalog scanner reads. `.claude` is a symlink to
# `.agents`, but the harness may load either, so the anchor must hold on both.
_DEVELOP_SKILLS = (
    _REPO_ROOT / ".agents" / "skills" / "develop" / "SKILL.md",
    _REPO_ROOT / ".claude" / "skills" / "develop" / "SKILL.md",
)
_YAML_BLOCK_MARKERS = frozenset({">", ">-", ">+", "|", "|-", "|+"})


def _is_indented_or_blank(line: str) -> bool:
    return not line or line[0].isspace()


def _description_field(lines: list[str]) -> tuple[int, str] | None:
    return next(
        (
            (index, line.split(":", 1)[1].strip())
            for index, line in enumerate(lines[1:], 1)
            if line.startswith("description:")
        ),
        None,
    )


def _frontmatter_description(text: str) -> str:
    """Fold the `description:` block scalar out of a SKILL.md frontmatter.

    Handles the `description: >-` folded form the skill files use: the value is
    the indented block below the key, rejoined on single spaces and stopped at
    the next top-level key or the closing `---`.
    """
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "missing frontmatter open"
    description = _description_field(lines)
    if description is None:
        return ""
    index, inline = description
    if inline and inline not in _YAML_BLOCK_MARKERS:
        return inline
    return " ".join(
        line.strip()
        for line in takewhile(_is_indented_or_blank, lines[index + 1:])
        if line.strip()
    )


class DevelopSkillTriggerAnchorTest(unittest.TestCase):
    def test_anchor_names_commit_not_pr(self) -> None:
        for path in _DEVELOP_SKILLS:
            desc = _frontmatter_description(
                path.read_text(encoding="utf-8")
            ).lower()
            with self.subTest(path=str(path)):
                # The action every developer run actually performs.
                self.assertIn("commit", desc)
                # Actions the implementer is forbidden to take must not be
                # advertised as the trigger, or the skill goes unused.
                self.assertNotIn("opening a pr", desc)
                self.assertNotIn("open a pr", desc)
                self.assertNotIn("push", desc)

    def test_implementer_prompt_matches_anchor(self) -> None:
        prompt = workflow_messages._build_implement_prompt(
            _TEST_SPEC, make_issue(1), "", [_TEST_SPEC],
        )
        # The prompt drives the agent to commit -- the anchor's verb -- while
        # forbidding the push / open-PR action the anchor must never name.
        self.assertIn("COMMIT", prompt)
        self.assertIn("Do NOT push", prompt)
        self.assertIn("orchestrator pushes and opens the PR", prompt)


if __name__ == "__main__":
    unittest.main()
