"""Pin the fail-safe defaults introduced when WakeLite was de-identified.

Before this, the Slack destination and the auto-merge author allowlist were
hardcoded to one person. Both are now environment-driven, and both default to
"do nothing" rather than to a real identity. A regression here does not raise
an error -- it quietly posts to, or merges on behalf of, whoever the constant
used to name. So each default is asserted explicitly.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]


class SlackChannelDefaultTests(unittest.TestCase):
    def _fresh_notifier_module(self, env: dict):
        """Reimport notifier under a given environment; its constants are import-time."""
        with mock.patch.dict(os.environ, env, clear=False):
            import wakelite.notifier as notifier
            return importlib.reload(notifier)

    def tearDown(self) -> None:
        import wakelite.notifier as notifier
        importlib.reload(notifier)

    def test_slack_channel_has_no_builtin_default(self):
        """An unset WAKELITE_SLACK_CHANNEL must not fall back to a real channel."""
        env = {k: v for k, v in os.environ.items() if k != "WAKELITE_SLACK_CHANNEL"}
        with mock.patch.dict(os.environ, env, clear=True):
            import wakelite.notifier as notifier
            notifier = importlib.reload(notifier)
            self.assertEqual(notifier.SLACK_CHANNEL, "")

    def test_no_channel_means_no_post(self):
        """With no channel, notify_slack returns None and never opens a connection."""
        notifier = self._fresh_notifier_module({"WAKELITE_SLACK_CHANNEL": ""})
        with mock.patch.object(notifier.urllib.request, "urlopen") as opened, \
             mock.patch.object(notifier, "_resolve_slack_token", return_value="xoxb-fake"):
            result = notifier.Notifier().notify_slack("hello", channel="")
        self.assertIsNone(result)
        opened.assert_not_called()

    def test_keychain_lookup_is_configurable(self):
        notifier = self._fresh_notifier_module({
            "WAKELITE_KEYCHAIN_SERVICE": "svc-x",
            "WAKELITE_KEYCHAIN_ACCOUNT": "acct-y",
        })
        self.assertEqual(notifier._KEYCHAIN_SERVICE, "svc-x")
        self.assertEqual(notifier._KEYCHAIN_ACCOUNT, "acct-y")

    def test_no_personal_identifiers_remain_in_source(self):
        """The published tree must not name one person's Slack DM or Keychain item."""
        # Assembled from fragments so this file is not itself a hit.
        banned = ("D0AGH" + "QCAU56", "ohade" + "-claude", "com." + "ohad.wakelite")
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
        ).stdout.split()
        hits = []
        this_file = Path(__file__).relative_to(REPO_ROOT).as_posix()
        for rel in tracked:
            if rel == this_file:
                continue  # the assertion literals live here by necessity
            path = REPO_ROOT / rel
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, IsADirectoryError):
                continue
            for token in banned:
                if token in text:
                    hits.append(f"{rel}: {token}")
        self.assertEqual(hits, [], f"personal identifiers found: {hits}")


class AuthorAllowlistDefaultTests(unittest.TestCase):
    SCRIPT = REPO_ROOT / "wakelite" / "scripts" / "daily-merge-stable.sh"

    def _call_allowlist(self, authors: str, pattern=None) -> int:
        """Source the script's function in isolation and ask it about `authors`."""
        env = dict(os.environ)
        env.pop("DAILY_MERGE_AUTHOR_PATTERN", None)
        if pattern is not None:
            env["DAILY_MERGE_AUTHOR_PATTERN"] = pattern
        # Pull just the function out; the script body needs a full fixture to run.
        body = self.SCRIPT.read_text(encoding="utf-8")
        start = body.index("all_authors_are_trusted() {")
        end = body.index("\n}\n", start) + 3
        snippet = body[start:end]
        proc = subprocess.run(
            ["bash", "-c", f'{snippet}\nall_authors_are_trusted "$1"', "_", authors],
            env=env, capture_output=True, text=True,
        )
        return proc.returncode

    def test_unset_pattern_trusts_nobody(self):
        """No configured pattern must refuse a real author, not wave it through."""
        self.assertEqual(self._call_allowlist("Some Person"), 1)

    def test_unset_pattern_refuses_the_formerly_hardcoded_author(self):
        """The regression that matters.

        The old implementation matched author names against a literal $DAILY_MERGE_AUTHOR_PATTERN
        and would auto-merge this author with no configuration at all. If that
        behaviour ever comes back, this is the assertion that catches it: with no
        DAILY_MERGE_AUTHOR_PATTERN set, even that name must be refused.
        """
        self.assertEqual(self._call_allowlist("Oh" + "ad"), 1)
        self.assertEqual(self._call_allowlist("Oh" + "ad Example"), 1)

    def test_empty_author_list_is_trivially_trusted(self):
        self.assertEqual(self._call_allowlist(""), 0)

    def test_pattern_matches_case_insensitively(self):
        self.assertEqual(self._call_allowlist("SOMEBODY Example", pattern="somebody"), 0)

    def test_pattern_rejects_a_foreign_author(self):
        self.assertEqual(self._call_allowlist("Someone Else", pattern="somebody"), 1)

    def test_mixed_list_rejects_when_any_author_is_foreign(self):
        self.assertEqual(self._call_allowlist("somebody\nSomeone Else", pattern="somebody"), 1)


if __name__ == "__main__":
    unittest.main()
