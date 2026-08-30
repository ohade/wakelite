"""`timer create --help` must document the current callback contract.

Written as a TestCase rather than a bare function on purpose: CLAUDE.md
documents `python -m unittest discover` as the runner, and unittest's loader
only collects TestCase subclasses. As a module-level `def test_...` this file
was silently skipped -- `python -m unittest tests.test_cli_help` reported
"Ran 0 tests" -- so the assertions below had never executed under the
project's own test command. Found 2026-08-30 while reconciling a 257 vs 258
count between unittest and pytest.
"""

import subprocess
import sys
import unittest


class TimerCreateHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.help_text = subprocess.run(
            [sys.executable, "-m", "wakelite.cli", "timer", "create", "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_help_names_every_supported_terminal(self):
        self.assertIn('"cmux", "ghostty", or "wezterm"', self.help_text)

    def test_help_documents_the_amq_callback_flag(self):
        self.assertIn('"amq": true', self.help_text)
        self.assertIn("cmux+session defaults true at creation", self.help_text)

    def test_help_describes_the_session_id_field(self):
        self.assertIn("Claude Code session ID used for identity/resume", self.help_text)

    def test_help_no_longer_carries_the_superseded_wording(self):
        # These two described an older contract. They must stay absent, or the
        # help text has regressed to documenting behaviour that no longer holds.
        self.assertNotIn("Claude/Codex session identity", self.help_text)
        self.assertNotIn('only "wezterm" supported', self.help_text)


if __name__ == "__main__":
    unittest.main()
