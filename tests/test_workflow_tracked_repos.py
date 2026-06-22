# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`_build_tracked_repos_context` renders the compact, read-only awareness
block listing the *other* repos this orchestrator tracks. It is the core
primitive behind tracked-repos awareness; this module pins its gating (kill
switch, single-repo no-op), current-repo exclusion, the per-repo line content
(slug / target_root / base branch), the overflow cap, the stage-neutral
framing, and -- load-bearing for the security story -- the absence of any
secret or remote field.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow, workflow_messages


def _spec(
    slug: str, root: str, base: str = "main", remote: str = "origin"
) -> config.RepoSpec:
    return config.RepoSpec(
        slug=slug,
        target_root=Path(root),
        base_branch=base,
        remote_name=remote,
    )


class BuildTrackedReposContextTest(unittest.TestCase):
    def _build(
        self,
        current: config.RepoSpec,
        specs: list[config.RepoSpec],
        *,
        expose: bool = True,
    ) -> str:
        # Patch the exact config module the builder reads so the result is
        # deterministic regardless of ambient EXPOSE_TRACKED_REPOS / any
        # prior test that reloaded orchestrator.config.
        with patch.object(
            workflow_messages.config, "EXPOSE_TRACKED_REPOS", expose
        ):
            return workflow._build_tracked_repos_context(current, specs)

    def test_single_repo_is_no_op(self) -> None:
        # The default single-repo deployment must see zero added tokens.
        cur = _spec("owner/only", "/srv/only")
        self.assertEqual(self._build(cur, [cur]), "")

    def test_empty_specs_is_no_op(self) -> None:
        cur = _spec("owner/only", "/srv/only")
        self.assertEqual(self._build(cur, []), "")

    def test_kill_switch_off_returns_empty(self) -> None:
        cur = _spec("owner/lance", "/srv/lance")
        other = _spec("owner/ray", "/srv/ray")
        self.assertEqual(self._build(cur, [cur, other], expose=False), "")

    def test_lists_other_repos_with_slug_root_and_base(self) -> None:
        cur = _spec("owner/lance", "/srv/lance")
        ray = _spec("owner/ray", "/srv/repos/ray", base="main")
        arrow = _spec("owner/arrow", "/srv/repos/arrow", base="master")
        out = self._build(cur, [cur, ray, arrow])

        # Each other repo contributes its slug, durable target_root, and base.
        self.assertIn("owner/ray", out)
        self.assertIn("/srv/repos/ray", out)
        self.assertIn("`main`", out)
        self.assertIn("owner/arrow", out)
        self.assertIn("/srv/repos/arrow", out)
        self.assertIn("`master`", out)

    def test_excludes_current_repo_from_listing(self) -> None:
        # The current repo's path must never appear; its slug appears only in
        # the "your task is on X" marker, not as a listed reference checkout.
        cur = _spec("owner/lance", "/srv/CURRENT-ROOT-MARKER")
        other = _spec("owner/ray", "/srv/ray")
        out = self._build(cur, [cur, other])

        self.assertIn("`owner/lance`", out)  # task marker
        self.assertNotIn("/srv/CURRENT-ROOT-MARKER", out)
        # The current repo is not rendered as a "- slug — source at ..." line.
        self.assertNotIn("- owner/lance —", out)

    def test_caps_listing_with_and_n_more(self) -> None:
        cur = _spec("owner/lance", "/srv/lance")
        others = [_spec(f"sib/{i}", f"/srv/{i}") for i in range(22)]
        out = self._build(cur, [cur, *others])

        # First 20 listed inline, the remaining 2 collapsed into one line.
        for i in range(20):
            self.assertIn(f"sib/{i}", out)
        self.assertNotIn("sib/20", out)
        self.assertNotIn("sib/21", out)
        self.assertIn("and 2 more", out)

    def test_omits_secret_and_remote_fields(self) -> None:
        # Load-bearing for the security analysis: the block carries only
        # operator-configured, non-secret data. No remote name / URL, no
        # token-shaped field is rendered.
        cur = _spec("owner/lance", "/srv/lance")
        other = _spec(
            "owner/ray", "/srv/ray", remote="SECRET-REMOTE-NAME"
        )
        out = self._build(cur, [cur, other])

        self.assertNotIn("SECRET-REMOTE-NAME", out)
        self.assertNotIn("git@", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("token", out.lower())

    def test_stage_neutral_read_only_framing(self) -> None:
        # The framing states only that the *sibling* checkouts are read-only;
        # it must NOT imply a write grant inside the current worktree (that is
        # owned by the surrounding stage prompt).
        cur = _spec("owner/lance", "/srv/lance")
        other = _spec("owner/ray", "/srv/ray")
        out = self._build(cur, [cur, other])

        self.assertIn("read-only", out)
        # The block explicitly DEFERS the write decision to the surrounding
        # stage prompt instead of granting (or denying) it here, so it stays
        # safe to embed in both the read-only and commit-producing stages.
        self.assertIn("governed by the rest of this prompt", out)


if __name__ == "__main__":
    unittest.main()
