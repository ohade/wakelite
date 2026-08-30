"""Desktop notifications must be clickable and land on the right page.

AppleScript's `display notification` carries no click action: macOS attributes
an osascript notification to Script Editor, so clicking one opened Script
Editor's document picker instead of WakeLite. These pin the terminal-notifier
path, its arguments, and the osascript fallback.
"""

import unittest
from unittest.mock import MagicMock, patch

from wakelite.notifier import Notifier


class NotifierClickTargetTests(unittest.TestCase):
    def setUp(self):
        Notifier._poster_cache = [("wakelite-notify", "/fake/WakeLiteNotify")]
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        import wakelite.notifier as module

        Notifier._poster_cache = module._UNRESOLVED

    def test_ui_url_builds_a_dashboard_deep_link(self):
        self.assertTrue(Notifier.ui_url("#incidents").endswith("/ui#incidents"))
        self.assertTrue(Notifier.ui_url().endswith("/ui"))

    def test_click_target_and_group_reach_the_wakelite_bundle(self):
        with patch("wakelite.notifier.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            Notifier().notify(
                "WakeLite failure",
                "plane-health failed",
                open_url="http://127.0.0.1:17341/ui#timer/abc",
                group="wakelite-timer-abc",
            )

        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "/fake/WakeLiteNotify")
        self.assertEqual(cmd[cmd.index("--url") + 1], "http://127.0.0.1:17341/ui#timer/abc")
        self.assertEqual(cmd[cmd.index("--group") + 1], "wakelite-timer-abc")

    def test_terminal_notifier_uses_its_own_flag_spelling(self):
        Notifier._poster_cache = [("terminal-notifier", "/fake/terminal-notifier")]
        with patch("wakelite.notifier.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            Notifier().notify("WakeLite", "m", open_url="http://x/ui", group="g")

        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-open") + 1], "http://x/ui")
        self.assertEqual(cmd[cmd.index("-group") + 1], "g")

    def test_a_notification_without_a_target_omits_the_url_flag(self):
        with patch("wakelite.notifier.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            Notifier().notify("WakeLite", "no target")

        cmd = run.call_args[0][0]
        self.assertNotIn("--url", cmd)
        self.assertNotIn("--group", cmd)

    def test_osascript_still_delivers_when_no_poster_is_installed(self):
        Notifier._poster_cache = []
        with patch("wakelite.notifier.subprocess.run") as run:
            Notifier().notify("WakeLite failure", "plane-health failed", open_url="http://x/ui")

        self.assertEqual(run.call_args[0][0][0], "osascript")

    def test_a_denied_poster_is_skipped_and_the_next_one_is_tried(self):
        Notifier._poster_cache = [
            ("wakelite-notify", "/fake/WakeLiteNotify"),
            ("terminal-notifier", "/fake/terminal-notifier"),
        ]
        with patch("wakelite.notifier.subprocess.run") as run:
            run.side_effect = [MagicMock(returncode=2), MagicMock(returncode=0)]
            Notifier().notify("WakeLite failure", "plane-health failed", open_url="http://x/ui")

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1][0][0][0], "/fake/terminal-notifier")
        # The denied poster is dropped, so the next alert does not retry it.
        self.assertEqual([kind for kind, _ in Notifier._poster_cache], ["terminal-notifier"])

    def test_every_poster_failing_still_delivers_via_osascript(self):
        Notifier._poster_cache = [("wakelite-notify", "/fake/WakeLiteNotify")]
        with patch("wakelite.notifier.subprocess.run") as run:
            run.side_effect = [MagicMock(returncode=2), MagicMock(returncode=0)]
            Notifier().notify("WakeLite failure", "plane-health failed", open_url="http://x/ui")

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1][0][0][0], "osascript")

    def test_muting_suppresses_delivery_entirely(self):
        notifier = Notifier()
        notifier.muted = True
        with patch("wakelite.notifier.subprocess.run") as run:
            notifier.notify("WakeLite failure", "plane-health failed", open_url="http://x/ui")

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
