import importlib
import json
import os
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch


def _bootstrap(temp_home: str):
    os.environ["HOME"] = temp_home
    os.environ["WAKELITE_HOME"] = temp_home
    import wakelite.config as config
    import wakelite.service as service
    import wakelite.state as state
    import wakelite.timer_store as timer_store

    importlib.reload(config)
    importlib.reload(state)
    importlib.reload(timer_store)
    importlib.reload(service)

    real_init = service.WakeLiteService.__init__

    def patched_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self.notifier.notify_slack = unittest.mock.MagicMock(return_value="fake-ts-1234")
        def fake_daily_thread_ts():
            self.notifier.notify_slack(":calendar: Timer activity")
            return "fake-thread-ts"
        self.notifier.get_daily_thread_ts = fake_daily_thread_ts

    service.WakeLiteService.__init__ = patched_init
    return service.WakeLiteService


def _basic_timer(name: str, shell: str = "echo ok"):
    return {
        "name": name,
        "comment": f"Negative matrix timer: {name}",
        "enabled": True,
        "recurrence": {"frequency": "daily", "time": "00:00", "interval": 1},
        "command": {
            "mode": "shell",
            "shell": shell,
            "workingDirectory": str(Path.home()),
        },
        "wake": {"enabled": False, "action": "wake", "leadMinutes": 0},
    }


def _cmux_timer(
    name: str,
    cli_path: str,
    workspace_id: str = "ws-1",
    surface_id: str = "sf-1",
    session_id: str = "sess-1",
    shell: str = "echo ok",
):
    timer = _basic_timer(name, shell)
    timer["callback"] = {
        "type": "cmux",
        "workspace_id": workspace_id,
        "surface_id": surface_id,
        "session_id": session_id,
        "cli_path": cli_path,
    }
    return timer


def _make_executable(path: Path) -> str:
    path.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _stdout_file(root: str) -> str:
    path = Path(root) / "stdout.log"
    path.write_text("negative matrix output", encoding="utf-8")
    return str(path)


def _signal_files(home: str, session_id: str):
    return list((Path(home) / ".claude" / "session-signals").glob(f"{session_id}.*.wakelite-callback.json"))


class NegativeMatrixTests(unittest.TestCase):
    def test_cmux_app_crashes_mid_callback_keeps_signal_file_and_logs_failure(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_timer("cmux-crash", cmux)

            with patch("wakelite.service.subprocess.run", side_effect=OSError("cmux crashed")), \
                 self.assertLogs("wakelite.service", level="ERROR") as logs:
                svc._execute_callback(timer, "success", 0, "run-neg-1", _stdout_file(td), 1.0)

            self.assertTrue(_signal_files(td, "sess-1"))
            self.assertIn("cmux command failed", "\n".join(logs.output))

    def test_two_cmux_sessions_same_workspace_do_not_cross_inject(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            first = _cmux_timer("cmux-a", cmux, workspace_id="ws-shared", surface_id="sf-a", session_id="sess-a")
            second = _cmux_timer("cmux-b", cmux, workspace_id="ws-shared", surface_id="sf-b", session_id="sess-b")

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch("wakelite.service.time.sleep"):
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._execute_callback(first, "success", 0, "run-neg-2a", _stdout_file(td), 1.0)
                svc._execute_callback(second, "success", 0, "run-neg-2b", _stdout_file(td), 1.0)

            send_calls = [call.args[0] for call in mock_run.call_args_list if call.args[0][1] == "send"]
            self.assertEqual(send_calls[0][5], "sf-a")
            self.assertEqual(send_calls[1][5], "sf-b")

    def test_cmux_cli_absent_from_launchd_keeps_signal_file(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            missing = str(Path(td) / "missing-cmux")
            timer = _cmux_timer("cmux-cli-absent", missing)

            with self.assertLogs("wakelite.service", level="ERROR") as logs:
                svc._execute_callback(timer, "success", 0, "run-neg-3", _stdout_file(td), 1.0)

            self.assertTrue(_signal_files(td, "sess-1"))
            self.assertIn("cmux CLI not found", "\n".join(logs.output))

    def test_cmux_socket_password_mismatch_fails_closed_without_slack_start_thread(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_timer("cmux-password-mismatch", cmux)
            timer["callback"]["socket_path"] = str(Path(td) / "bad.sock")

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 self.assertLogs("wakelite.service", level="ERROR") as logs:
                mock_run.return_value = unittest.mock.Mock(returncode=1, stdout="", stderr="password mismatch")
                svc._execute_callback(timer, "success", 0, "run-neg-4", _stdout_file(td), 1.0)

            svc.notifier.notify_slack.assert_not_called()
            self.assertTrue(_signal_files(td, "sess-1"))
            self.assertIn("cmux send failed", "\n".join(logs.output))

    def test_workspace_rename_or_move_routes_by_ids_not_names(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_timer(
                "Pretty Workspace Name",
                cmux,
                workspace_id="ws-stable-id",
                surface_id="sf-stable-id",
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch("wakelite.service.time.sleep"):
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._execute_callback(timer, "success", 0, "run-neg-5", _stdout_file(td), 1.0)

            send_call = mock_run.call_args_list[0].args[0]
            self.assertEqual(send_call[3], "ws-stable-id")
            self.assertEqual(send_call[5], "sf-stable-id")

    def test_send_success_but_send_key_enter_failure_keeps_signal_file(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_timer("cmux-enter-fails", cmux)

            def side_effect(args, **kwargs):
                if args[1] == "send-key":
                    return unittest.mock.Mock(returncode=1, stdout="", stderr="cannot press enter")
                return unittest.mock.Mock(returncode=0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect), \
                 patch("wakelite.service.time.sleep"), \
                 self.assertLogs("wakelite.service", level="WARNING") as logs:
                svc._execute_callback(timer, "success", 0, "run-neg-6", _stdout_file(td), 1.0)

            self.assertTrue(_signal_files(td, "sess-1"))
            self.assertIn("partial injection", "\n".join(logs.output))

    def test_slack_thread_collision_avoided_for_session_bound_cmux_timers(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            first = svc.timer_store.create_timer(_cmux_timer("cmux-thread-a", cmux, session_id="sess-a"))
            second = svc.timer_store.create_timer(_cmux_timer("cmux-thread-b", cmux, session_id="sess-b"))

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack") as mock_slack:
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._schedule_occurrence(first, "2026-04-27T10:00:00", is_catchup=False, queued_reason=None)
                svc._schedule_occurrence(second, "2026-04-27T10:00:01", is_catchup=False, queued_reason=None)
                time.sleep(1.5)

            started_calls = [
                call for call in mock_slack.call_args_list
                if call.args and "started" in call.args[0].lower()
            ]
            self.assertEqual(started_calls, [])

    def test_focus_link_token_scenario_never_executes_focus_panel_from_wakelite(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_timer("cmux-no-focus-token-path", cmux)

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch("wakelite.service.time.sleep"):
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._execute_callback(timer, "success", 0, "run-neg-8", _stdout_file(td), 1.0)

            commands = [call.args[0][1] for call in mock_run.call_args_list]
            self.assertNotIn("focus-panel", commands)

    def test_malformed_focus_ids_are_not_shell_interpolated_by_wakelite_callback(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            bad_workspace = "ws; touch /tmp/should-not-run"
            bad_surface = "sf && false"
            timer = _cmux_timer("cmux-argv-safety", cmux, workspace_id=bad_workspace, surface_id=bad_surface)

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch("wakelite.service.time.sleep"):
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._execute_callback(timer, "success", 0, "run-neg-9", _stdout_file(td), 1.0)

            argv = mock_run.call_args_list[0].args[0]
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[3], bad_workspace)
            self.assertEqual(argv[5], bad_surface)

    def test_unknown_terminal_auto_capture_leaves_no_unusable_callback(self):
        from wakelite.config import auto_capture_terminal

        callback = {}
        with patch.dict(os.environ, {"HOME": os.environ.get("HOME", "")}, clear=True):
            auto_capture_terminal(callback)

        self.assertEqual(callback, {})


if __name__ == "__main__":
    unittest.main()
