import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


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
    return service.WakeLiteService


def _timer(name: str, shell: str, notifications=None, until=None):
    payload = {
        "name": name,
        "comment": f"Test Slack activity policy for {name}",
        "enabled": True,
        "recurrence": {"frequency": "daily", "time": "00:00"},
        "command": {
            "mode": "shell",
            "shell": shell,
            "workingDirectory": str(Path.home()),
        },
    }
    if notifications is not None:
        payload["notifications"] = notifications
    if until is not None:
        payload["until"] = until
    return payload


class SlackActivitySchemaTests(unittest.TestCase):
    def test_default_is_enabled_and_explicit_boolean_values_are_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            store = WakeLiteService(tick_seconds=1).timer_store

            default_timer = store.create_timer(_timer("default", "exit 0"))
            self.assertIs(default_timer["notifications"]["slackActivity"], True)

            for index, value in enumerate((False, True)):
                timer = store.create_timer(
                    _timer(
                        f"explicit-{index}",
                        "exit 0",
                        {"onSuccess": False, "onFailure": True, "slackActivity": value},
                    )
                )
                self.assertIs(timer["notifications"]["slackActivity"], value)

    def test_invalid_notifications_fail_closed(self):
        invalid_values = (None, [], "false", 0)
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                WakeLiteService = _bootstrap(td)
                store = WakeLiteService(tick_seconds=1).timer_store
                payload = _timer("invalid", "exit 0")
                payload["notifications"] = value
                with self.assertRaisesRegex(ValueError, "notifications must be an object"):
                    store.create_timer(payload)

        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            store = WakeLiteService(tick_seconds=1).timer_store
            with self.assertRaisesRegex(ValueError, "notifications.slackActivity must be boolean"):
                store.create_timer(
                    _timer("invalid-type", "exit 0", {"slackActivity": "false"})
                )
            with self.assertRaisesRegex(ValueError, "Unknown keys in notifications"):
                store.create_timer(
                    _timer("unknown-key", "exit 0", {"slack_activity": False})
                )


class SlackActivityLifecycleTests(unittest.TestCase):
    def _run(self, shell: str, slack_activity, until=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        WakeLiteService = _bootstrap(temp_dir.name)
        service = WakeLiteService(tick_seconds=1)
        notifications = {"onSuccess": False, "onFailure": True}
        if slack_activity is not None:
            notifications["slackActivity"] = slack_activity
        timer = service.timer_store.create_timer(
            _timer("activity-test", shell, notifications, until=until)
        )
        service.notifier.get_daily_thread_ts = MagicMock(return_value="thread-ts")
        service.notifier.notify_slack = MagicMock()
        with patch.object(service.notifier, "notify"):
            service._run_occurrence(
                timer,
                "2026-07-20T10:00:00+00:00",
                is_catchup=False,
                queued_reason=None,
                retry_of_run_id=None,
            )
        return service, timer

    def test_false_is_slack_silent_for_success_failure_and_waiting(self):
        for shell, expected_status in (
            ("exit 0", "success"),
            ("exit 1", "failed"),
            ("exit 75", "waiting"),
        ):
            with self.subTest(status=expected_status):
                service, timer = self._run(shell, False)
                service.notifier.get_daily_thread_ts.assert_not_called()
                service.notifier.notify_slack.assert_not_called()
                run = service.list_runs(limit=1, timer_id=timer["id"])[0]
                self.assertEqual(run["status"], expected_status)

    def test_false_is_slack_silent_when_until_auto_deletes(self):
        service, timer = self._run(
            "exit 0",
            False,
            until={"on_success": "delete", "on_failure": "continue"},
        )
        self.assertIsNone(service.timer_store.get_timer(timer["id"]))
        service.notifier.get_daily_thread_ts.assert_not_called()
        service.notifier.notify_slack.assert_not_called()

    def test_absent_or_true_preserves_start_and_end_activity(self):
        for value in (None, True):
            for shell, expected_label in (
                ("exit 0", "success"),
                ("exit 1", "failed"),
                ("exit 75", "waiting"),
            ):
                with self.subTest(slack_activity=value, status=expected_label):
                    service, _ = self._run(shell, value)
                    service.notifier.get_daily_thread_ts.assert_called_once_with()
                    self.assertEqual(service.notifier.notify_slack.call_count, 2)
                    messages = [
                        call.args[0]
                        for call in service.notifier.notify_slack.call_args_list
                    ]
                    self.assertIn("started", messages[0].lower())
                    self.assertIn(expected_label, messages[1].lower())


if __name__ == "__main__":
    unittest.main()
