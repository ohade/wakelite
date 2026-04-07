import importlib
import json
import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch


def _bootstrap(temp_home: str):
    os.environ["WAKELITE_HOME"] = temp_home
    import wakelite.config as config
    import wakelite.service as service
    import wakelite.state as state
    import wakelite.timer_store as timer_store

    importlib.reload(config)
    importlib.reload(state)
    importlib.reload(timer_store)
    importlib.reload(service)

    # Prevent tests from posting real Slack messages.
    # Tests that need to assert on Slack calls should still use
    # patch.object(svc.notifier, "notify_slack") for explicit control.
    _real_init = service.WakeLiteService.__init__

    def _patched_init(self, *a, **kw):
        _real_init(self, *a, **kw)
        self.notifier.notify_slack = unittest.mock.MagicMock(return_value="fake-ts-1234")

    service.WakeLiteService.__init__ = _patched_init

    return service.WakeLiteService


def _basic_timer(name: str, shell: str = "echo ok"):
    return {
        "name": name,
        "comment": f"Test timer: {name}",
        "enabled": True,
        "recurrence": {
            "frequency": "daily",
            "time": "00:00",
            "interval": 1,
        },
        "command": {
            "mode": "shell",
            "shell": shell,
            "workingDirectory": str(Path.home()),
        },
        "wake": {"enabled": False, "action": "wake", "leadMinutes": 0},
    }


class ServiceTests(unittest.TestCase):
    def test_list_runs_includes_logs_url_and_has_logs(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(_basic_timer("logs-meta", "echo hello"))

            svc._schedule_occurrence(timer, "2026-02-24T01:55:00", is_catchup=False, queued_reason=None)
            time.sleep(1.3)

            runs = svc.list_runs(limit=10, timer_id=timer["id"])
            self.assertTrue(runs)
            self.assertIn("logs_url", runs[0])
            self.assertTrue(runs[0]["logs_url"].startswith("/v1/runs/"))
            self.assertIn("has_logs", runs[0])
            self.assertTrue(runs[0]["has_logs"])

    def test_get_run_logs_marks_expired_when_files_missing_and_old(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(_basic_timer("logs-expired", "echo hello"))

            svc._schedule_occurrence(timer, "2026-02-24T01:55:00", is_catchup=False, queued_reason=None)
            time.sleep(1.3)
            run = svc.list_runs(limit=1, timer_id=timer["id"])[0]
            run_id = run["run_id"]

            stdout_path = Path(run["stdout_path"])
            stderr_path = Path(run["stderr_path"])
            if stdout_path.exists():
                stdout_path.unlink()
            if stderr_path.exists():
                stderr_path.unlink()

            old_created = "2020-01-01T00:00:00+00:00"
            with svc.state._connect() as conn:
                conn.execute("UPDATE run_history SET created_at = ? WHERE run_id = ?", (old_created, run_id))

            payload = svc.get_run_logs(run_id)
            self.assertFalse(payload["stdout_available"])
            self.assertFalse(payload["stderr_available"])
            self.assertTrue(payload["logs_expired"])

    def test_prune_run_log_files_keeps_runner_and_reconciler_logs(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            home = Path(td)
            log_root = home / "logs"
            old_dir = log_root / "timer-a" / "2026-01-01"
            old_dir.mkdir(parents=True, exist_ok=True)
            old_file = old_dir / "11111111-1111-1111-1111-111111111111.out.log"
            old_file.write_text("old", encoding="utf-8")
            recent_file = old_dir / "22222222-2222-2222-2222-222222222222.out.log"
            recent_file.write_text("recent", encoding="utf-8")

            runner_log = log_root / "runner.log"
            reconciler_log = log_root / "reconciler.log"
            launchd_out_log = log_root / "runner.launchd.out.log"
            runner_log.write_text("runner", encoding="utf-8")
            reconciler_log.write_text("reconciler", encoding="utf-8")
            launchd_out_log.write_text("launchd", encoding="utf-8")

            now = time.time()
            old_ts = now - (9 * 86400)
            recent_ts = now - (1 * 86400)
            os.utime(old_file, (old_ts, old_ts))
            os.utime(recent_file, (recent_ts, recent_ts))
            os.utime(runner_log, (old_ts, old_ts))
            os.utime(reconciler_log, (old_ts, old_ts))
            os.utime(launchd_out_log, (old_ts, old_ts))

            summary = svc._prune_run_log_files(retention_days=7)
            self.assertGreaterEqual(summary["files_deleted"], 1)
            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(runner_log.exists())
            self.assertTrue(reconciler_log.exists())
            self.assertTrue(launchd_out_log.exists())

    def test_run_history_keeps_timer_name_after_timer_delete(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(_basic_timer("name-retention"))

            svc._schedule_occurrence(timer, "2026-02-24T01:55:00", is_catchup=False, queued_reason=None)
            time.sleep(1.3)
            svc.timer_store.delete_timer(timer["id"])

            runs = svc.list_runs(limit=10, timer_id=timer["id"])
            self.assertTrue(runs)
            self.assertEqual(runs[0]["timer_name"], "name-retention")

    def test_timer_notification_defaults_and_update(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            created = svc.timer_store.create_timer(_basic_timer("notify"))
            self.assertEqual(created["notifications"]["onSuccess"], False)
            self.assertEqual(created["notifications"]["onFailure"], True)

            updated = svc.timer_store.update_timer(
                created["id"],
                {"notifications": {"onSuccess": True, "onFailure": True}},
            )
            self.assertEqual(updated["notifications"]["onSuccess"], True)
            self.assertEqual(updated["notifications"]["onFailure"], True)

    def test_idempotency_create_same_key_same_payload(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            payload = _basic_timer("idem")
            a = svc.create_timer(payload, "k1")
            b = svc.create_timer(payload, "k1")
            self.assertEqual(a["timer"]["id"], b["timer"]["id"])

    def test_idempotency_create_same_key_different_payload_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            svc.create_timer(_basic_timer("one"), "k1")
            with self.assertRaises(Exception) as ctx:
                svc.create_timer(_basic_timer("two"), "k1")
            self.assertIn("idempotency key", str(ctx.exception))

    def test_overlap_queue_once_runs_one_queued_after_current(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(_basic_timer("queue", "sleep 1"))

            with patch.object(svc.notifier, "notify_slack"):
                svc._schedule_occurrence(timer, "2026-02-24T01:55:00", is_catchup=False, queued_reason=None)
                time.sleep(0.15)
                svc._schedule_occurrence(timer, "2026-02-24T01:55:30", is_catchup=False, queued_reason=None)
                svc._schedule_occurrence(timer, "2026-02-24T01:56:00", is_catchup=False, queued_reason=None)
                time.sleep(2.6)

            runs = svc.list_runs(limit=10, timer_id=timer["id"])
            self.assertEqual(len(runs), 2)
            self.assertTrue(all(r["status"] in ("success", "failed", "waiting") for r in runs))

    def test_abort_run_terminates_active_process(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(_basic_timer("abortable", "sleep 30"))

            svc._schedule_occurrence(timer, "2026-02-24T01:55:00", is_catchup=False, queued_reason=None)

            run = None
            for _ in range(40):
                runs = svc.list_runs(limit=5, timer_id=timer["id"])
                if runs:
                    run = runs[0]
                    if run["status"] == "started":
                        break
                time.sleep(0.1)

            self.assertIsNotNone(run)
            self.assertEqual(run["status"], "started")

            response = svc.abort_run(run["run_id"], "abort-k1")
            self.assertTrue(response["aborted"])
            self.assertEqual(response["status"], "aborting")

            terminal = None
            for _ in range(50):
                runs = svc.list_runs(limit=5, timer_id=timer["id"])
                if runs and runs[0]["status"] in ("success", "failed", "aborted", "uncertain_crash", "skipped", "waiting"):
                    terminal = runs[0]
                    break
                time.sleep(0.1)

            self.assertIsNotNone(terminal)
            self.assertEqual(terminal["status"], "aborted")
            self.assertIn("aborted", (terminal.get("message") or "").lower())

    def test_start_repairs_stale_runtime_lock_and_replays_queue(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(_basic_timer("repair-runtime", "echo repaired"))
            timer_id = timer["id"]

            crashed_sched = "2026-02-24T01:55:00"
            queued_sched = "2026-02-24T01:56:00"

            svc.state.reserve_occurrence(timer_id, crashed_sched, False)
            run_ctx = svc.state.create_run(
                timer_id=timer_id,
                timer_name=timer["name"],
                scheduled_at=crashed_sched,
                is_catchup=False,
                queued_reason="run_now",
                retry_of_run_id=None,
            )
            svc.state.set_runtime_running(timer_id, run_ctx["run_id"], crashed_sched)
            svc.state.finish_run(
                run_id=run_ctx["run_id"],
                timer_id=timer_id,
                scheduled_at=crashed_sched,
                status="uncertain_crash",
                exit_code=None,
                message="Recovered after crash; retry queued",
                stdout_path=None,
                stderr_path=None,
            )

            svc.state.reserve_occurrence(timer_id, queued_sched, False)
            self.assertTrue(svc.state.set_queue_once(timer_id, queued_sched))
            self.assertTrue(svc.state.get_runtime(timer_id).is_running)

            svc.start()
            try:
                success_seen = False
                for _ in range(40):
                    runs = svc.list_runs(limit=20, timer_id=timer_id)
                    if any(r["scheduled_at"] == queued_sched and r["status"] == "success" for r in runs):
                        success_seen = True
                        break
                    time.sleep(0.2)
                self.assertTrue(success_seen)
            finally:
                svc.stop()

            runtime = svc.state.get_runtime(timer_id)
            self.assertFalse(runtime.is_running)
            self.assertFalse(runtime.queued_once)


class IntervalTimerTests(unittest.TestCase):
    def test_interval_timer_fires_multiple_times(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            outfile = os.path.join(td, "interval_out.txt")
            timer = svc.timer_store.create_timer({
                "name": "interval-test",
                "comment": "Fires every 10 seconds",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "command": {"mode": "shell", "shell": f"echo tick >> {outfile}"},
            })

            svc.start()
            try:
                time.sleep(22)
            finally:
                svc.stop()

            lines = Path(outfile).read_text().strip().split("\n") if Path(outfile).exists() else []
            self.assertGreaterEqual(len(lines), 2, f"Expected >=2 fires in 22s, got {len(lines)}")

    def test_interval_timer_no_catchup_after_gap(self):
        """If service was 'down' during gap, interval timer fires once, not N times."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            outfile = os.path.join(td, "no_catchup.txt")
            timer = svc.timer_store.create_timer({
                "name": "no-catchup",
                "comment": "10s interval",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "command": {"mode": "shell", "shell": f"echo tick >> {outfile}"},
            })

            # Simulate: set last_fired to 60s ago
            from datetime import datetime, timedelta
            old_time = (datetime.now() - timedelta(seconds=60)).isoformat()
            svc.state.set_meta(f"interval.last_fired.{timer['id']}", old_time)

            svc.start()
            try:
                time.sleep(2.5)
            finally:
                svc.stop()

            lines = Path(outfile).read_text().strip().split("\n") if Path(outfile).exists() else []
            # Should fire once (no catchup), not 6 times
            self.assertEqual(len(lines), 1, f"Expected 1 fire (no catchup), got {len(lines)}")


class DaemonTimerTests(unittest.TestCase):
    def test_daemon_restarts_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            outfile = os.path.join(td, "daemon_starts.txt")
            timer = svc.timer_store.create_timer({
                "name": "daemon-restart",
                "comment": "Exits with code 1, should restart",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "execution": {
                    "restart_on_failure": True,
                    "restart_delay_seconds": 1,
                    "restart_max_backoff_seconds": 2,
                },
                "command": {"mode": "shell", "shell": f"echo start >> {outfile} && exit 1"},
            })

            svc.start()
            try:
                time.sleep(6)
            finally:
                svc.stop()

            lines = Path(outfile).read_text().strip().split("\n") if Path(outfile).exists() else []
            # Should have restarted at least once after the first failure
            self.assertGreaterEqual(len(lines), 2, f"Expected >=2 starts (restart), got {len(lines)}")

    def test_daemon_no_restart_when_restart_on_failure_false(self):
        """With restart_on_failure=False, daemon should NOT restart on non-zero exit."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            outfile = os.path.join(td, "daemon_norestart.txt")
            timer = svc.timer_store.create_timer({
                "name": "daemon-norestart",
                "comment": "Exits with code 1, should not restart",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "execution": {"restart_on_failure": False, "restart_delay_seconds": 1},
                "command": {"mode": "shell", "shell": f"echo start >> {outfile} && exit 1"},
            })

            svc.start()
            try:
                time.sleep(4)
            finally:
                svc.stop()

            lines = Path(outfile).read_text().strip().split("\n") if Path(outfile).exists() else []
            # restart_on_failure=False + exit 1 = no restart
            self.assertEqual(len(lines), 1, f"Expected exactly 1 start (no restart), got {len(lines)}")


class ParallelExecutionTests(unittest.TestCase):
    def test_two_interval_timers_run_concurrently(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            out1 = os.path.join(td, "par1.txt")
            out2 = os.path.join(td, "par2.txt")

            svc.timer_store.create_timer({
                "name": "parallel-1",
                "comment": "Sleeps 2s then writes",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "execution": {"overlap": "skip"},
                "command": {"mode": "shell", "shell": f"sleep 2 && echo done >> {out1}"},
            })
            svc.timer_store.create_timer({
                "name": "parallel-2",
                "comment": "Sleeps 2s then writes",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "execution": {"overlap": "skip"},
                "command": {"mode": "shell", "shell": f"sleep 2 && echo done >> {out2}"},
            })

            svc.start()
            try:
                time.sleep(5)
            finally:
                svc.stop()

            p1 = Path(out1).read_text().strip().split("\n") if Path(out1).exists() else []
            p2 = Path(out2).read_text().strip().split("\n") if Path(out2).exists() else []
            # Both should have completed at least once within 4s (proving they ran in parallel)
            self.assertGreaterEqual(len(p1), 1, "parallel-1 didn't fire")
            self.assertGreaterEqual(len(p2), 1, "parallel-2 didn't fire")


class CapacityGuardTests(unittest.TestCase):
    def test_capacity_exceeded_blocks_creation(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=2)

            for i in range(2):
                svc.timer_store.create_timer({
                    "name": f"filler-{i}",
                    "comment": f"Filler timer {i}",
                    "enabled": True,
                    "timer_type": "daemon",
                    "recurrence": {"frequency": "interval", "every": "0s"},
                    "command": {"mode": "shell", "shell": "sleep 999"},
                })

            from wakelite.service import CapacityExceededError
            with self.assertRaises(CapacityExceededError):
                svc.create_timer({
                    "name": "one-too-many",
                    "comment": "Should be blocked",
                    "enabled": True,
                    "timer_type": "daemon",
                    "recurrence": {"frequency": "interval", "every": "0s"},
                    "command": {"mode": "shell", "shell": "sleep 999"},
                }, idempotency_key="cap-test")

    def test_disabled_timer_doesnt_count_toward_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=2)

            svc.timer_store.create_timer({
                "name": "disabled-filler",
                "comment": "Disabled, should not count",
                "enabled": False,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "command": {"mode": "shell", "shell": "sleep 999"},
            })
            svc.timer_store.create_timer({
                "name": "enabled-filler",
                "comment": "Enabled",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "command": {"mode": "shell", "shell": "sleep 999"},
            })

            # Should succeed — 1 enabled + 1 new = 2, within limit
            result = svc.create_timer({
                "name": "fits",
                "comment": "Should fit",
                "enabled": True,
                "recurrence": {"frequency": "daily", "time": "00:00"},
                "command": {"mode": "shell", "shell": "echo ok"},
            }, idempotency_key="cap-fit")
            self.assertIn("timer", result)


class ResourceWarningTests(unittest.TestCase):
    def test_resource_conflict_returns_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)

            svc.timer_store.create_timer({
                "name": "slack-poller",
                "comment": "Polls slack",
                "enabled": True,
                "recurrence": {"frequency": "daily", "time": "00:00"},
                "command": {"mode": "shell", "shell": "echo ok"},
                "resources": [{"name": "slack-api", "capacity": "50 req/min", "estimated_usage": "6 req/min"}],
            })

            result = svc.create_timer({
                "name": "slack-notifier",
                "comment": "Sends slack notifications",
                "enabled": True,
                "recurrence": {"frequency": "daily", "time": "00:00"},
                "command": {"mode": "shell", "shell": "echo ok"},
                "resources": [{"name": "slack-api", "capacity": "50 req/min", "estimated_usage": "12 req/min"}],
            }, idempotency_key="res-test")

            self.assertIn("warnings", result)
            self.assertTrue(len(result["warnings"]) > 0)
            self.assertIn("slack-api", result["warnings"][0])
            self.assertIn("slack-poller", result["warnings"][0])


class OnceTimerPastDateValidationTests(unittest.TestCase):
    """Reject one-time timers with past dates at creation/update time."""

    def _once_timer(self, date_str, enabled=True):
        return {
            "name": "once-test",
            "comment": "One-time timer test",
            "enabled": enabled,
            "recurrence": {
                "frequency": "once",
                "date": date_str,
                "time": "10:00",
            },
            "command": {
                "mode": "shell",
                "shell": "echo ok",
                "workingDirectory": str(Path.home()),
            },
        }

    def test_create_once_timer_past_date_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            with self.assertRaises(ValueError) as ctx:
                store.create_timer(self._once_timer("2020-01-01"))
            self.assertIn("past date", str(ctx.exception))

    def test_create_once_timer_yesterday_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            from datetime import date, timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            store = ts.TimerStore()
            with self.assertRaises(ValueError) as ctx:
                store.create_timer(self._once_timer(yesterday))
            self.assertIn("past date", str(ctx.exception))

    def test_create_once_timer_today_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            from datetime import date
            store = ts.TimerStore()
            timer = store.create_timer(self._once_timer(date.today().isoformat()))
            self.assertEqual(timer["name"], "once-test")

    def test_create_once_timer_future_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            timer = store.create_timer(self._once_timer("2030-06-15"))
            self.assertEqual(timer["name"], "once-test")

    def test_create_once_timer_past_date_disabled_accepted(self):
        """Disabled timers with past dates are allowed (edge case)."""
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            timer = store.create_timer(self._once_timer("2020-01-01", enabled=False))
            self.assertEqual(timer["enabled"], False)

    def test_update_enable_past_once_timer_rejected(self):
        """Re-enabling a past once-timer should be rejected."""
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            # Create disabled (allowed)
            timer = store.create_timer(self._once_timer("2020-01-01", enabled=False))
            # Try to enable (rejected)
            with self.assertRaises(ValueError) as ctx:
                store.update_timer(timer["id"], {"enabled": True})
            self.assertIn("past date", str(ctx.exception))


class EventDrivenSchedulerTests(unittest.TestCase):
    def test_compute_next_wake_no_timers(self):
        """With no timers, should sleep for MAX_SLEEP."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            from datetime import datetime, timedelta
            svc._last_slow_tick_at = datetime.now()
            sleep_secs = svc._compute_next_wake(datetime.now())
            self.assertGreaterEqual(sleep_secs, 10.0)
            self.assertLessEqual(sleep_secs, 15.0)

    def test_compute_next_wake_interval_timer(self):
        """Should return remaining seconds for an interval timer."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            from datetime import datetime, timedelta

            timer = svc.timer_store.create_timer({
                "name": "interval-wake",
                "comment": "10s interval",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "command": {"mode": "shell", "shell": "echo ok"},
            })

            now = datetime.now()
            svc._last_slow_tick_at = now
            # Set last_fired to 3s ago -> should return ~7s
            svc.state.set_meta(f"interval.last_fired.{timer['id']}", (now - timedelta(seconds=3)).isoformat())
            sleep_secs = svc._compute_next_wake(now)
            self.assertGreater(sleep_secs, 5.0)
            self.assertLess(sleep_secs, 9.0)

    def test_compute_next_wake_never_fired_returns_immediate(self):
        """An interval timer that never fired should return near-zero."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            from datetime import datetime

            svc.timer_store.create_timer({
                "name": "never-fired",
                "comment": "10s interval, never fired",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "command": {"mode": "shell", "shell": "echo ok"},
            })

            svc._last_slow_tick_at = datetime.now()
            sleep_secs = svc._compute_next_wake(datetime.now())
            self.assertLessEqual(sleep_secs, 0.2)

    def test_wake_signal_fires_timer_promptly(self):
        """Creating a timer + wake signal should fire it within ~2s, not 15s."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=60)  # slow tick = 60s
            outfile = os.path.join(td, "wake_test.txt")

            svc.start()
            try:
                # Create timer after start — wake signal should make it fire promptly
                svc.timer_store.create_timer({
                    "name": "wake-interval",
                    "comment": "Should fire promptly",
                    "enabled": True,
                    "recurrence": {"frequency": "interval", "every": "10s"},
                    "command": {"mode": "shell", "shell": f"echo fired >> {outfile}"},
                })
                svc._signal_wake()
                time.sleep(2.5)
            finally:
                svc.stop()

            lines = Path(outfile).read_text().strip().split("\n") if Path(outfile).exists() else []
            self.assertGreaterEqual(len(lines), 1, "Timer should fire within 2.5s of wake signal")


class UntilConditionTests(unittest.TestCase):
    """WL-5: Auto-delete timer when until condition is met."""

    def _until_timer(self, name, shell, on_success="continue", on_failure="continue"):
        return {
            "name": name,
            "comment": f"Until test: {name}",
            "enabled": True,
            "recurrence": {"frequency": "interval", "every": "10s"},
            "command": {"mode": "shell", "shell": shell},
            "until": {"on_success": on_success, "on_failure": on_failure},
        }

    def test_until_success_deletes_on_success(self):
        """Timer with on_success=delete should be deleted after exit 0."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                self._until_timer("until-success", "echo ok", on_success="delete")
            )
            timer_id = timer["id"]

            svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            # Timer should have been deleted
            self.assertIsNone(svc.timer_store.get_timer(timer_id))
            # Run should exist in history
            runs = svc.list_runs(limit=10, timer_id=timer_id)
            self.assertTrue(runs)
            self.assertEqual(runs[0]["status"], "success")

    def test_until_success_keeps_on_failure(self):
        """Timer with on_success=delete, on_failure=continue should survive when command fails."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                self._until_timer("until-success-fail", "exit 1", on_success="delete")
            )
            timer_id = timer["id"]

            svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            # Timer should still exist
            self.assertIsNotNone(svc.timer_store.get_timer(timer_id))

    def test_until_failure_deletes_on_failure(self):
        """Timer with on_failure=delete should be deleted after non-zero exit."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                self._until_timer("until-failure", "exit 1", on_failure="delete")
            )
            timer_id = timer["id"]

            svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            self.assertIsNone(svc.timer_store.get_timer(timer_id))

    def test_until_failure_keeps_on_success(self):
        """Timer with on_failure=delete, on_success=continue should survive when command succeeds."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                self._until_timer("until-failure-ok", "echo ok", on_failure="delete")
            )
            timer_id = timer["id"]

            svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            self.assertIsNotNone(svc.timer_store.get_timer(timer_id))

    def test_until_with_max_runs_until_wins(self):
        """When both until and max_runs are set, until should delete before max_runs disables."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            payload = self._until_timer("until-maxruns", "echo ok", on_success="delete")
            payload["max_runs"] = 5
            timer = svc.timer_store.create_timer(payload)
            timer_id = timer["id"]

            svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            # until condition met on first run — timer should be deleted (not just disabled)
            self.assertIsNone(svc.timer_store.get_timer(timer_id))

    def test_until_with_max_runs_maxruns_wins(self):
        """When until never triggers, max_runs should disable normally."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            outfile = os.path.join(td, "maxruns_wins.txt")
            payload = self._until_timer("maxruns-wins", f"echo tick >> {outfile} && exit 1", on_success="delete")
            payload["max_runs"] = 2
            timer = svc.timer_store.create_timer(payload)
            timer_id = timer["id"]

            svc.start()
            try:
                time.sleep(25)
            finally:
                svc.stop()

            # Timer should still exist but be disabled (max_runs disables, not deletes)
            t = svc.timer_store.get_timer(timer_id)
            self.assertIsNotNone(t, "Timer should still exist (max_runs disables, not deletes)")
            self.assertFalse(t["enabled"])

    def test_until_validation_invalid_action_value(self):
        """Invalid action value (not delete/continue) should be rejected."""
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            with self.assertRaises(ValueError) as ctx:
                store.create_timer({
                    "name": "bad-until",
                    "comment": "Invalid until value",
                    "recurrence": {"frequency": "daily", "time": "00:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                    "until": {"on_success": "maybe", "on_failure": "continue"},
                })
            self.assertIn("until.on_success", str(ctx.exception))

    def test_until_validation_missing_field(self):
        """Both on_success and on_failure are required."""
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            with self.assertRaises(ValueError) as ctx:
                store.create_timer({
                    "name": "bad-until-partial",
                    "comment": "Only on_success set",
                    "recurrence": {"frequency": "daily", "time": "00:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                    "until": {"on_success": "delete"},
                })
            self.assertIn("until.on_failure", str(ctx.exception))

    def test_until_validation_not_dict(self):
        """until must be a dict, not a string."""
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts
            store = ts.TimerStore()
            with self.assertRaises(ValueError) as ctx:
                store.create_timer({
                    "name": "bad-until-str",
                    "comment": "Until is a string",
                    "recurrence": {"frequency": "daily", "time": "00:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                    "until": "success",
                })
            self.assertIn("until", str(ctx.exception))

    def test_until_slack_notification_on_delete(self):
        """WL-11: Slack DM sent when until auto-deletes a timer."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                self._until_timer("slack-notify-test", "echo ok", on_success="delete")
            )
            timer_id = timer["id"]

            with patch.object(svc.notifier, "notify_slack") as mock_slack:
                svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(1.5)

                # Timer deleted
                self.assertIsNone(svc.timer_store.get_timer(timer_id))
                # Slack called 3x: daily thread parent + started reply + auto-delete reply
                self.assertEqual(mock_slack.call_count, 3)
                # First call creates daily thread parent
                daily_msg = mock_slack.call_args_list[0][0][0]
                self.assertIn("Timer activity", daily_msg)
                # Second call is "started" reply
                started_msg = mock_slack.call_args_list[1][0][0]
                self.assertIn("slack-notify-test", started_msg)
                self.assertIn("started", started_msg.lower())
                # Third call is auto-delete with thread_ts
                delete_msg = mock_slack.call_args_list[2][0][0]
                self.assertIn("slack-notify-test", delete_msg)
                self.assertIn("on_success", delete_msg)
                self.assertIn("thread_ts", mock_slack.call_args_list[2][1])

    def test_until_no_slack_when_timer_survives(self):
        """WL-11: No until-delete Slack DM when until condition is NOT met.

        Timer still gets thread parent + thread close (for the failed run),
        but no auto-delete message.
        """
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                self._until_timer("no-slack-test", "exit 1", on_success="delete")
            )

            with patch.object(svc.notifier, "notify_slack") as mock_slack:
                svc._schedule_occurrence(timer, "2026-03-01T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(1.5)

                # Timer survives (failed, but on_success=delete only)
                self.assertIsNotNone(svc.timer_store.get_timer(timer["id"]))
                # Daily thread parent + started reply + thread close (no auto-delete)
                self.assertEqual(mock_slack.call_count, 3)
                msgs = [c[0][0] for c in mock_slack.call_args_list]
                self.assertFalse(any("auto-deleted" in m for m in msgs))


class WaitingStatusTests(unittest.TestCase):
    """Exit code 75 = waiting (EX_TEMPFAIL)."""

    def test_exit_75_produces_waiting_status(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _basic_timer("waiter", "exit 75")
            )
            svc._schedule_occurrence(timer, "2026-03-02T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            runs = svc.list_runs(limit=5, timer_id=timer["id"])
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "waiting")
            self.assertEqual(runs[0]["exit_code"], 75)

    def test_waiting_does_not_trigger_until_delete(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer({
                "name": "wait-until",
                "comment": "waiting should not trigger until",
                "enabled": True,
                "recurrence": {"frequency": "interval", "every": "10s"},
                "command": {"mode": "shell", "shell": "exit 75"},
                "until": {"on_success": "delete", "on_failure": "delete"},
            })
            timer_id = timer["id"]

            svc._schedule_occurrence(timer, "2026-03-02T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            # Timer should NOT be deleted -- waiting doesn't trigger either condition
            self.assertIsNotNone(svc.timer_store.get_timer(timer_id))

    def test_waiting_does_not_count_as_completed(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _basic_timer("wait-count", "exit 75")
            )
            svc._schedule_occurrence(timer, "2026-03-02T10:00:00", is_catchup=False, queued_reason=None)
            time.sleep(1.5)

            completed = svc.state.count_completed_runs(timer["id"])
            self.assertEqual(completed, 0)


def _callback_timer(name: str, shell: str = "echo callback-ok", pane_id: int = 42, session_id: str = "sess-123"):
    t = _basic_timer(name, shell)
    t["callback"] = {"type": "wezterm", "pane_id": pane_id, "session_id": session_id}
    return t


def _ghostty_callback_timer(name: str, shell: str = "echo callback-ok",
                            terminal_id: str = "ABCD-1234-UUID", session_id: str = "sess-456"):
    t = _basic_timer(name, shell)
    t["callback"] = {"type": "ghostty", "terminal_id": terminal_id, "session_id": session_id}
    return t


class CallbackTests(unittest.TestCase):
    """Tests for the WezTerm callback feature."""

    def test_callback_fires_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _callback_timer("cb-success", "echo hello-from-timer")
            )

            pane_list_json = json.dumps([{"pane_id": 42, "title": "test"}])
            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack"):
                # Mock wezterm cli list returning our pane
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout=pane_list_json, stderr=""
                )
                svc._schedule_occurrence(timer, "2026-03-03T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(4.0)

                # Verify wezterm cli send-text was called
                send_calls = [c for c in mock_run.call_args_list if "send-text" in str(c)]
                self.assertTrue(len(send_calls) >= 1, f"Expected send-text calls, got: {mock_run.call_args_list}")

    def test_callback_fires_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _callback_timer("cb-failure", "exit 1")
            )

            pane_list_json = json.dumps([{"pane_id": 42, "title": "test"}])
            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack"):
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout=pane_list_json, stderr=""
                )
                svc._schedule_occurrence(timer, "2026-03-03T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(4.0)

                send_calls = [c for c in mock_run.call_args_list if "send-text" in str(c)]
                self.assertTrue(len(send_calls) >= 1, f"Expected send-text calls for failed run")

    def test_callback_skips_waiting(self):
        """Exit 75 (waiting) should NOT trigger callback."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _callback_timer("cb-waiting", "exit 75")
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack"):
                svc._schedule_occurrence(timer, "2026-03-03T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(2.0)

                # No wezterm calls should be made for exit 75
                wezterm_calls = [c for c in mock_run.call_args_list if "wezterm" in str(c)]
                self.assertEqual(len(wezterm_calls), 0, f"Exit 75 should not trigger callback, got: {wezterm_calls}")

    def test_callback_skips_when_not_configured(self):
        """Timer without callback field should make no subprocess calls."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _basic_timer("no-callback", "echo ok")
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack"):
                svc._schedule_occurrence(timer, "2026-03-03T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(2.0)

                wezterm_calls = [c for c in mock_run.call_args_list if "wezterm" in str(c)]
                self.assertEqual(len(wezterm_calls), 0)

    def test_callback_fallback_when_pane_gone(self):
        """When pane is gone, should fall back to Slack + spawn."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _callback_timer("cb-fallback", "echo fallback-test", pane_id=999)
            )

            def mock_run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                result = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                if "list" in cmd:
                    # Return empty pane list — pane 999 not found
                    result.stdout = json.dumps([{"pane_id": 1, "title": "other"}])
                elif "spawn" in cmd:
                    result.stdout = "55"  # new pane id
                return result

            with patch("wakelite.service.subprocess.run", side_effect=mock_run_side_effect) as mock_run, \
                 patch.object(svc.notifier, "notify_slack") as mock_slack:
                svc._schedule_occurrence(timer, "2026-03-03T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(2.5)

                # Slack should have been called with fallback message
                mock_slack.assert_called()
                slack_msg = mock_slack.call_args[0][0]
                self.assertIn("fallback", slack_msg.lower())

                # spawn should have been called with claude --resume
                spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
                self.assertTrue(len(spawn_calls) >= 1, f"Expected spawn call, got: {mock_run.call_args_list}")


class GhosttyCallbackTests(unittest.TestCase):
    """Tests for the Ghostty callback feature."""

    def test_ghostty_callback_fires_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _ghostty_callback_timer("ghostty-success", "echo hello-ghostty")
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack"):
                # Mock osascript returning "true" for exists check
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout="true\n", stderr=""
                )
                svc._schedule_occurrence(timer, "2026-04-07T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(4.0)

                # Verify osascript was called with Ghostty commands
                osascript_calls = [c for c in mock_run.call_args_list if "osascript" in str(c)]
                self.assertTrue(len(osascript_calls) >= 1, f"Expected osascript calls, got: {mock_run.call_args_list}")

    def test_ghostty_callback_fallback_when_terminal_gone(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _ghostty_callback_timer("ghostty-fallback", "echo fallback", terminal_id="GONE-UUID")
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack") as mock_slack:
                # Mock osascript returning "false" for exists check
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout="false\n", stderr=""
                )
                svc._schedule_occurrence(timer, "2026-04-07T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(3.0)

                # Slack fallback should fire
                mock_slack.assert_called()
                slack_msg = mock_slack.call_args[0][0]
                self.assertIn("fallback", slack_msg.lower())

    def test_ghostty_validation_accepts_type(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _ghostty_callback_timer("ghostty-valid")
            )
            self.assertEqual(timer["callback"]["type"], "ghostty")
            self.assertEqual(timer["callback"]["terminal_id"], "ABCD-1234-UUID")

    def test_signal_file_includes_run_id(self):
        """Signal file name should include run_id to prevent overwrites."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            timer = svc.timer_store.create_timer(
                _ghostty_callback_timer("signal-runid", "echo signal-test")
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack"):
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout="true\n", stderr=""
                )
                svc._schedule_occurrence(timer, "2026-04-07T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(4.0)

                # Check signal file was written with run_id in name
                signal_dir = Path.home() / ".claude" / "session-signals"
                signal_files = list(signal_dir.glob("sess-456.*.wakelite-callback.json"))
                self.assertTrue(len(signal_files) >= 1,
                                f"Expected signal file with run_id, found: {list(signal_dir.glob('*'))}")
                # Verify run_id is in the JSON payload
                data = json.loads(signal_files[0].read_text())
                self.assertIn("run_id", data)


class AutoCaptureTerminalTests(unittest.TestCase):
    """Tests for the shared auto_capture_terminal helper."""

    def test_captures_ghostty_from_env(self):
        from wakelite.config import auto_capture_terminal
        cb = {"type": "ghostty"}
        with patch.dict(os.environ, {"GHOSTTY_TERMINAL_ID": "uuid-123"}, clear=False):
            auto_capture_terminal(cb)
        self.assertEqual(cb["terminal_id"], "uuid-123")

    def test_captures_wezterm_from_env(self):
        from wakelite.config import auto_capture_terminal
        cb = {"type": "wezterm"}
        with patch.dict(os.environ, {"WEZTERM_PANE": "42"}, clear=False):
            # Remove GHOSTTY_TERMINAL_ID if present to test wezterm path
            env = {"WEZTERM_PANE": "42"}
            with patch.dict(os.environ, env, clear=False):
                if "GHOSTTY_TERMINAL_ID" in os.environ:
                    del os.environ["GHOSTTY_TERMINAL_ID"]
                auto_capture_terminal(cb)
        self.assertEqual(cb["pane_id"], 42)

    def test_ghostty_takes_precedence(self):
        from wakelite.config import auto_capture_terminal
        cb = {}
        with patch.dict(os.environ, {"GHOSTTY_TERMINAL_ID": "uuid-456", "WEZTERM_PANE": "99"}, clear=False):
            auto_capture_terminal(cb)
        self.assertEqual(cb.get("type"), "ghostty")
        self.assertEqual(cb.get("terminal_id"), "uuid-456")
        self.assertNotIn("pane_id", cb)

    def test_does_not_overwrite_explicit_values(self):
        from wakelite.config import auto_capture_terminal
        cb = {"type": "ghostty", "terminal_id": "explicit-id"}
        with patch.dict(os.environ, {"GHOSTTY_TERMINAL_ID": "env-id"}, clear=False):
            auto_capture_terminal(cb)
        self.assertEqual(cb["terminal_id"], "explicit-id")


class CloneTimerTests(unittest.TestCase):
    def test_clone_creates_copy_with_new_id(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            original = svc.timer_store.create_timer(_basic_timer("original"))
            result = svc.clone_timer(original["id"], {}, "clone-key-1")
            cloned = result["timer"]
            self.assertNotEqual(cloned["id"], original["id"])
            self.assertEqual(cloned["name"], "original (copy)")
            self.assertEqual(cloned["command"]["shell"], original["command"]["shell"])
            self.assertEqual(cloned["recurrence"]["frequency"], "daily")

    def test_clone_applies_name_override(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            original = svc.timer_store.create_timer(_basic_timer("original"))
            result = svc.clone_timer(original["id"], {"name": "custom-clone"}, "clone-key-2")
            self.assertEqual(result["timer"]["name"], "custom-clone")

    def test_clone_applies_patch_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            original = svc.timer_store.create_timer(_basic_timer("original"))
            result = svc.clone_timer(
                original["id"],
                {"name": "patched", "command": {"mode": "shell", "shell": "echo patched"}},
                "clone-key-3",
            )
            self.assertEqual(result["timer"]["command"]["shell"], "echo patched")

    def test_clone_nonexistent_timer_raises(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            with self.assertRaises(KeyError):
                svc.clone_timer("nonexistent-id", {}, "clone-key-4")

    def test_clone_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            original = svc.timer_store.create_timer(_basic_timer("original"))
            r1 = svc.clone_timer(original["id"], {}, "clone-idem-key")
            r2 = svc.clone_timer(original["id"], {}, "clone-idem-key")
            self.assertEqual(r1["timer"]["id"], r2["timer"]["id"])
            # Should still be only 2 timers (original + 1 clone)
            self.assertEqual(len(svc.timer_store.list_timers()), 2)


class CreateFromTemplateTests(unittest.TestCase):
    def test_create_from_reminder_template(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            result = svc.create_from_template(
                "reminder",
                {
                    "name": "test-reminder",
                    "comment": "test",
                    "recurrence": {"date": "2030-01-01", "time": "09:00"},
                    "command": {"shell": "echo remind"},
                },
                "tpl-key-1",
            )
            timer = result["timer"]
            self.assertEqual(timer["name"], "test-reminder")
            self.assertEqual(timer["recurrence"]["frequency"], "once")
            self.assertTrue(timer["wake"]["enabled"])
            self.assertEqual(timer["until"]["on_success"], "delete")

    def test_create_from_unknown_template_raises(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            with self.assertRaises(ValueError) as ctx:
                svc.create_from_template("nonexistent", {"name": "x", "comment": "x"}, "tpl-key-2")
            self.assertIn("Unknown template", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
