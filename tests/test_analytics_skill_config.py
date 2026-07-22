# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics skill-trigger configuration tests."""

import unittest


from tests.analytics_reload_helpers import reload_analytics as _reload


_TRACK_SKILL_TRIGGERS = "TRACK_SKILL_TRIGGERS"


class SkillTriggerConfigTest(unittest.TestCase):
    """`TRACK_SKILL_TRIGGERS` parses at import inside the analytics package,
    defaults off, is exported in `__all__`, and honors the same truthy
    spellings as the other boolean knobs in `orchestrator.config`."""

    def test_defaults_off_and_is_exported(self) -> None:
        # Default-off is a deliberate, revisited decision (#515): even after
        # codex skill-trigger coverage landed (#513), the new file-open path's
        # production noise stays unmeasured, so the default holds off until it
        # proves low-noise live. Flipping this assertion is the flip.
        _, analytics = _reload()
        self.assertFalse(analytics.TRACK_SKILL_TRIGGERS)
        self.assertIn(_TRACK_SKILL_TRIGGERS, analytics.__all__)

    def test_truthy_spellings_enable(self) -> None:
        for spelling in ("1", "true", "on", "yes", "On", " YES "):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_TRACK_SKILL_TRIGGERS: spelling})
                self.assertTrue(analytics.TRACK_SKILL_TRIGGERS)

    def test_falsey_and_unknown_values_stay_off(self) -> None:
        for spelling in ("0", "false", "off", "no", "", "maybe"):
            with self.subTest(spelling=spelling):
                _, analytics = _reload({_TRACK_SKILL_TRIGGERS: spelling})
                self.assertFalse(analytics.TRACK_SKILL_TRIGGERS)
