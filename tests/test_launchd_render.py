"""Every installed plist must contain real paths, and installs must fail loudly.

Reported on a second machine, 2026-09-16: `install --scope system --load`
reported success while the installed daemon kept the repo's literal
placeholders:

    ['/path/to/wakelite/bin/wakelite-reconciler', '--interval', '600']

`install_user()` writes through `_render()`, which substitutes real paths.
`install_system()` used `shutil.copy2` instead, so wake-from-sleep -- the
product's headline feature -- silently never worked for anyone who installed
system scope. `_render()`'s own docstring records this bug being found and
fixed for the user scope; the system scope was missed.

It stayed silent because `launchctl bootstrap` accepts a plist pointing at a
nonexistent program and exits 0, so every command in the install path reported
success. Hence the second test class: the installer must check the rendered
program is executable rather than trusting bootstrap's exit code.
"""
from __future__ import annotations

import plistlib
import unittest
from pathlib import Path
from unittest import mock

from wakelite import launchd_install


PLACEHOLDERS = ("/path/to/wakelite", "/path/to/home")


class RenderTests(unittest.TestCase):
    def test_no_template_placeholder_survives_any_install_path(self):
        """User and system scope alike must render, never copy verbatim."""
        written: dict[str, str] = {}

        def capture(self_path, text, *a, **kw):
            written[str(self_path)] = text

        with mock.patch.object(Path, "write_text", capture), \
             mock.patch.object(Path, "mkdir"), \
             mock.patch.object(launchd_install, "_run",
                               return_value={"cmd": [], "returncode": 0, "stdout": "", "stderr": ""}), \
             mock.patch.object(launchd_install, "_verify_program", return_value=None):
            launchd_install.install_user(load=False)
            launchd_install.install_system(load=False)

        self.assertTrue(written, "no plist was written")
        for path, text in written.items():
            for ph in PLACEHOLDERS:
                self.assertNotIn(
                    ph, text,
                    f"{path} still contains the template placeholder {ph!r}",
                )

    def test_system_plist_program_path_is_real(self):
        """The rendered system daemon must point at this checkout's reconciler."""
        rendered = launchd_install._render(launchd_install.SYSTEM_TEMPLATE)
        args = plistlib.loads(rendered.encode())["ProgramArguments"]
        self.assertTrue(
            Path(args[0]).is_file(),
            f"system daemon ProgramArguments[0] is not a real file: {args[0]}",
        )


class ProgramVerificationTests(unittest.TestCase):
    """`launchctl bootstrap` exits 0 on a plist pointing at nothing (see module docstring)."""

    def test_missing_program_is_reported(self):
        result = launchd_install._verify_program(
            "<?xml version='1.0'?><plist version='1.0'><dict>"
            "<key>ProgramArguments</key><array>"
            "<string>/definitely/not/here/wakelite-runner</string></array>"
            "</dict></plist>"
        )
        self.assertIsNotNone(result, "a nonexistent program must be reported, not ignored")
        self.assertIn("not executable", result)

    def test_real_program_passes(self):
        rendered = launchd_install._render(launchd_install.USER_TEMPLATE)
        self.assertIsNone(
            launchd_install._verify_program(rendered),
            "the real rendered runner path should verify cleanly",
        )

    def test_install_surfaces_the_problem_instead_of_reporting_success(self):
        with mock.patch.object(launchd_install, "_write_agent"), \
             mock.patch.object(launchd_install, "_render", return_value="<plist/>"), \
             mock.patch.object(launchd_install, "_verify_program", return_value="not executable: /nope"), \
             mock.patch.object(launchd_install, "_run",
                               return_value={"cmd": [], "returncode": 0, "stdout": "", "stderr": ""}):
            result = launchd_install.install_user(load=False)
        self.assertIn("problems", result)
        self.assertTrue(result["problems"], "install must surface the verification failure")


if __name__ == "__main__":
    unittest.main()
