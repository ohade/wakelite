"""A first install must not report a failure for unloading something never loaded.

Reported by a first-time user, 2026-09-16: `launchd install --scope user --load`
printed

    watchdog_bootout ... returncode 5: Boot-out failed: Input/output error

On a clean machine nothing is loaded yet, so the pre-emptive `launchctl bootout`
always fails. The install then succeeds -- the following bootstrap returns 0 --
but the output reads as a broken install to someone running it for the first time.

launchctl reports "not loaded" as EIO (5) with that message rather than as a
distinct code, so the fix classifies the result instead of just hiding rc != 0:
a genuine bootout failure must still be visible.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from wakelite import launchd_install


NOT_LOADED = {
    "returncode": 5,
    "stdout": "",
    "stderr": "Boot-out failed: 5: Input/output error\nTry re-running the command as root for richer errors.",
}
REAL_FAILURE = {
    "returncode": 1,
    "stdout": "",
    "stderr": "Boot-out failed: 9: Bad file descriptor",
}
SUCCESS = {"returncode": 0, "stdout": "", "stderr": ""}


def _fake_run(results):
    """Return a _run stand-in that answers bootouts from `results`, else success."""
    def run(cmd):
        base = {"cmd": cmd}
        if "bootout" in cmd:
            base.update(results.pop(0) if results else SUCCESS)
        else:
            base.update(SUCCESS)
        return base
    return run


class BootoutNoiseTests(unittest.TestCase):
    def test_first_install_does_not_report_a_bootout_failure(self):
        """Nothing loaded yet: both bootouts must be reported as benign."""
        with mock.patch.object(launchd_install, "_write_agent"), \
             mock.patch.object(launchd_install, "_run",
                               side_effect=_fake_run([dict(NOT_LOADED), dict(NOT_LOADED)])):
            result = launchd_install.install_user(load=True)

        for key in ("bootout", "watchdog_bootout"):
            entry = result[key]
            self.assertEqual(
                entry.get("status"), "not_loaded",
                f"{key} should be classified not_loaded, got {entry!r}",
            )
            self.assertEqual(
                entry.get("returncode"), 0,
                f"{key} must not surface a non-zero returncode on a first install",
            )

    def test_a_genuine_bootout_failure_is_still_visible(self):
        """The fix must classify, not silence. A real failure keeps its code."""
        with mock.patch.object(launchd_install, "_write_agent"), \
             mock.patch.object(launchd_install, "_run",
                               side_effect=_fake_run([dict(REAL_FAILURE), dict(NOT_LOADED)])):
            result = launchd_install.install_user(load=True)

        self.assertEqual(result["bootout"]["status"], "failed")
        self.assertEqual(result["bootout"]["returncode"], 1)
        self.assertEqual(result["watchdog_bootout"]["status"], "not_loaded")

    def test_unloading_something_that_was_loaded_is_reported_as_unloaded(self):
        with mock.patch.object(launchd_install, "_write_agent"), \
             mock.patch.object(launchd_install, "_run",
                               side_effect=_fake_run([dict(SUCCESS), dict(SUCCESS)])):
            result = launchd_install.install_user(load=True)

        self.assertEqual(result["bootout"]["status"], "unloaded")
        self.assertEqual(result["watchdog_bootout"]["status"], "unloaded")

    def test_uninstall_is_classified_too(self):
        """`uninstall --scope user` on an already-stopped agent has the same wart."""
        missing = Path("/nonexistent/wakelite-test.plist")
        with mock.patch.object(launchd_install, "_run",
                               side_effect=_fake_run([dict(NOT_LOADED), dict(NOT_LOADED)])), \
             mock.patch.object(launchd_install, "USER_TARGET", missing), \
             mock.patch.object(launchd_install, "WATCHDOG_TARGET", missing):
            result = launchd_install.uninstall_user(unload=True)

        self.assertEqual(result["bootout"]["status"], "not_loaded")
        self.assertEqual(result["bootout"]["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
