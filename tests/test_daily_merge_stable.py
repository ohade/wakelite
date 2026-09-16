from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "wakelite" / "scripts" / "daily-merge-stable.sh"


class DailyMergeStableFixtureTests(unittest.TestCase):
    """FIXTURE: black-box tests against disposable local Git remotes."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # macOS exposes /var as a symlink to /private/var. Canonicalize once so
        # the production worktree-containment check sees the same path as Git.
        self.root = Path(self.temp.name).resolve()
        self.remote = self.root / "origin.git"
        self.products = self.root / "products"
        self.worktree_root = self.root / "worktrees" / "products"
        self.feature_worktree = self.worktree_root / "feature-test"
        self.state = self.root / "state"
        self.log = self.root / "daily-merge.log"
        self.notify_log = self.root / "notifications.log"
        self.notifier = self.root / "fixture-notifier.sh"
        self.git_wrapper = self.root / "fixture-git.sh"
        self.git_mode = self.root / "git-mode"
        self.race_clone = self.root / "race-actor"
        self.race_marker = self.root / "race-pushed"
        self.real_git = shutil.which("git")
        if not self.real_git:
            self.skipTest("git is required")

        self._git("init", "--bare", str(self.remote), cwd=self.root)
        self._git("clone", str(self.remote), str(self.products), cwd=self.root)
        self._git("config", "user.name", "Fixture Author", cwd=self.products)
        self._git("config", "user.email", "fixture-author@example.test", cwd=self.products)

        (self.products / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "base.txt", cwd=self.products)
        self._git("commit", "-m", "base", cwd=self.products)
        self._git("push", "-u", "origin", "HEAD:master", cwd=self.products)
        base_sha = self._git("rev-parse", "HEAD", cwd=self.products).stdout.strip()

        (self.products / "stable.txt").write_text("stable\n", encoding="utf-8")
        self._git("add", "stable.txt", cwd=self.products)
        self._git("commit", "-m", "stable update", cwd=self.products)
        self._git("tag", "stable/trunk", cwd=self.products)
        self._git("push", "origin", "master", "--tags", cwd=self.products)

        self.worktree_root.mkdir(parents=True)
        self._git("branch", "feature/test", base_sha, cwd=self.products)
        self._git(
            "worktree",
            "add",
            str(self.feature_worktree),
            "feature/test",
            cwd=self.products,
        )
        self._commit_feature_file("feature.txt", "feature\n", "feature work")
        self._git("push", "-u", "origin", "feature/test", cwd=self.feature_worktree)

        self.notifier.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$DAILY_MERGE_SLACK_TEXT\" >>\"$FIXTURE_NOTIFY_LOG\"\n"
            "exit \"${FIXTURE_NOTIFY_EXIT:-0}\"\n",
            encoding="utf-8",
        )
        self.notifier.chmod(0o755)
        self.git_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "for fixture_arg in \"$@\"; do\n"
            "  if [[ \"$fixture_arg\" == push ]]; then\n"
            "    case \"$(cat \"$FIXTURE_GIT_MODE\")\" in\n"
            "      dns) printf '%s\\n' 'fatal: Could not resolve host: fixture.invalid' >&2; exit 1 ;;\n"
            "      auth) printf '%s\\n' 'Permission denied (publickey).' >&2; exit 128 ;;\n"
            "      unknown) printf '%s\\n' 'fixture push exploded unexpectedly' >&2; exit 1 ;;\n"
            "      nff_race)\n"
            "        if [[ ! -e \"$FIXTURE_RACE_MARKER\" ]]; then\n"
            "          \"$FIXTURE_REAL_GIT\" -C \"$FIXTURE_RACE_REPO\" push origin feature/test >/dev/null\n"
            "          touch \"$FIXTURE_RACE_MARKER\"\n"
            "          printf '%s\\n' '! [rejected] feature/test -> feature/test (fetch first)' >&2\n"
            "          exit 1\n"
            "        fi\n"
            "        ;;\n"
            "    esac\n"
            "  fi\n"
            "done\n"
            "exec \"$FIXTURE_REAL_GIT\" \"$@\"\n",
            encoding="utf-8",
        )
        self.git_wrapper.chmod(0o755)
        self.git_mode.write_text("normal\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(
        self,
        *args: str,
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.real_git, *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
        )

    def _commit_feature_file(self, name: str, body: str, message: str) -> None:
        (self.feature_worktree / name).write_text(body, encoding="utf-8")
        self._git("add", name, cwd=self.feature_worktree)
        self._git("commit", "-m", message, cwd=self.feature_worktree)

    def _run_script(
        self,
        *,
        git_mode: str = "normal",
        notify_exit: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        self.git_mode.write_text(f"{git_mode}\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "DAILY_MERGE_PRODUCTS_REPO": str(self.products),
                "DAILY_MERGE_WORKTREE_DIR": str(self.worktree_root),
                "DAILY_MERGE_REF": "stable/trunk",
                "DAILY_MERGE_STATE_DIR": str(self.state),
                "DAILY_MERGE_LOG_FILE": str(self.log),
                "DAILY_MERGE_SLACK_CHANNEL": "fixture-channel",
                "DAILY_MERGE_BYPASS_FILE": str(self.root / "compile-bypass"),
                "DAILY_MERGE_EXCLUDED_BRANCHES": "",
                "DAILY_MERGE_NOTIFIER_BIN": str(self.notifier),
                "DAILY_MERGE_GIT_BIN": str(self.git_wrapper),
                "DAILY_MERGE_STREAK_THRESHOLD": "3",
                # The script trusts no author unless told who to trust, so the
                # fixture declares its own identity. Matches the fixture-author@example.test
                # authors configured in _setup_products.
                "DAILY_MERGE_AUTHOR_PATTERN": "fixture",
                "FIXTURE_NOTIFY_LOG": str(self.notify_log),
                "FIXTURE_NOTIFY_EXIT": str(notify_exit),
                "FIXTURE_GIT_MODE": str(self.git_mode),
                "FIXTURE_REAL_GIT": str(self.real_git),
                "FIXTURE_RACE_REPO": str(self.race_clone),
                "FIXTURE_RACE_MARKER": str(self.race_marker),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _notification_count(self) -> int:
        if not self.notify_log.exists():
            return 0
        return self.notify_log.read_text(encoding="utf-8").count(
            "Daily Merge Stable escalation"
        )

    def test_fixture_diverged_non_fast_forward_recovers_pushes_and_stays_silent(self) -> None:
        remote_clone = self.root / "remote-actor"
        self._git("clone", str(self.remote), str(remote_clone), cwd=self.root)
        self._git("config", "user.name", "Fixture Remote", cwd=remote_clone)
        self._git("config", "user.email", "fixture-remote@example.test", cwd=remote_clone)
        self._git("checkout", "feature/test", cwd=remote_clone)
        (remote_clone / "remote.txt").write_text("remote\n", encoding="utf-8")
        self._git("add", "remote.txt", cwd=remote_clone)
        self._git("commit", "-m", "remote feature work", cwd=remote_clone)
        self._git("push", "origin", "feature/test", cwd=remote_clone)

        self._commit_feature_file("local.txt", "local\n", "local feature work")
        result = self._run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._git("fetch", "origin", "feature/test", cwd=self.products)
        remote_tip = self._git(
            "rev-parse", "origin/feature/test", cwd=self.products
        ).stdout.strip()
        for required_path in ("feature.txt", "local.txt", "remote.txt", "stable.txt"):
            shown = self._git(
                "show", f"{remote_tip}:{required_path}", cwd=self.products
            )
            self.assertTrue(shown.stdout.strip(), required_path)
        self.assertEqual(self._notification_count(), 0)

    def test_fixture_push_race_fetches_merges_retries_and_stays_silent(self) -> None:
        self._git("clone", str(self.remote), str(self.race_clone), cwd=self.root)
        self._git("config", "user.name", "Fixture Race", cwd=self.race_clone)
        self._git("config", "user.email", "fixture-race@example.test", cwd=self.race_clone)
        self._git("checkout", "feature/test", cwd=self.race_clone)
        (self.race_clone / "race.txt").write_text("race\n", encoding="utf-8")
        self._git("add", "race.txt", cwd=self.race_clone)
        self._git("commit", "-m", "racing remote work", cwd=self.race_clone)

        result = self._run_script(git_mode="nff_race")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Non-fast-forward repaired and pushed", result.stdout)
        self._git("fetch", "origin", "feature/test", cwd=self.products)
        remote_tip = self._git(
            "rev-parse", "origin/feature/test", cwd=self.products
        ).stdout.strip()
        for required_path in ("feature.txt", "race.txt", "stable.txt"):
            shown = self._git(
                "show", f"{remote_tip}:{required_path}", cwd=self.products
            )
            self.assertTrue(shown.stdout.strip(), required_path)
        self.assertEqual(self._notification_count(), 0)

    def test_fixture_dns_failure_is_silent_once_then_escalates_at_threshold(self) -> None:
        first = self._run_script(git_mode="dns")
        second = self._run_script(git_mode="dns")
        third = self._run_script(git_mode="dns")

        self.assertEqual(first.returncode, 75, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 75, second.stdout + second.stderr)
        self.assertEqual(third.returncode, 1, third.stdout + third.stderr)
        self.assertEqual(self._notification_count(), 1)
        self.assertIn("transient_network", self.notify_log.read_text(encoding="utf-8"))
        self.assertIn("streak 3", self.notify_log.read_text(encoding="utf-8"))

    def test_fixture_auth_failure_escalates_on_first_occurrence(self) -> None:
        result = self._run_script(git_mode="auth")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self._notification_count(), 1)
        self.assertIn("auth_permission", self.notify_log.read_text(encoding="utf-8"))
        self.assertIn("streak 1", self.notify_log.read_text(encoding="utf-8"))

    def test_fixture_unknown_failure_escalates_at_same_branch_threshold(self) -> None:
        first = self._run_script(git_mode="unknown")
        second = self._run_script(git_mode="unknown")
        third = self._run_script(git_mode="unknown")

        self.assertEqual(first.returncode, 75, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 75, second.stdout + second.stderr)
        self.assertEqual(third.returncode, 1, third.stdout + third.stderr)
        self.assertEqual(self._notification_count(), 1)
        self.assertIn("unknown", self.notify_log.read_text(encoding="utf-8"))

    def test_fixture_green_path_posts_nothing(self) -> None:
        initial = self._run_script()
        green = self._run_script()

        self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.assertEqual(self._notification_count(), 0)
        self.assertIn("Slack remained silent", green.stdout)

    def test_fixture_unreachable_slack_is_loud_and_nonzero(self) -> None:
        result = self._run_script(git_mode="auth", notify_exit=9)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Slack escalation delivery failed", result.stdout)
        state_rows = [
            path.read_text(encoding="utf-8").splitlines()
            for path in self.state.glob("*.state")
        ]
        self.assertTrue(state_rows)
        self.assertTrue(all(row[3] == "0" for row in state_rows), state_rows)


if __name__ == "__main__":
    unittest.main()
