import importlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import unittest
import unittest.mock
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
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

    # Prevent tests from posting real Slack messages or firing real macOS
    # notifications. Fixture timers are deliberately named and deliberately
    # fail (e.g. "daemon-restart", which exits 1 to exercise restart backoff),
    # so an unpatched notifier delivers a convincing but fake "WakeLite
    # failure" alert to the developer's desktop on every test run.
    # Tests that need to assert on Slack calls should still use
    # patch.object(svc.notifier, "notify_slack") for explicit control.
    _real_init = service.WakeLiteService.__init__

    def _patched_init(self, *a, **kw):
        _real_init(self, *a, **kw)
        self.notifier.notify = unittest.mock.MagicMock()
        self.notifier.notify_slack = unittest.mock.MagicMock(return_value="fake-ts-1234")
        def _fake_daily_thread_ts():
            self.notifier.notify_slack(":calendar: Timer activity")
            return "fake-thread-ts"
        self.notifier.get_daily_thread_ts = _fake_daily_thread_ts

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


class TimerStoreIsolationTests(unittest.TestCase):
    def test_list_and_get_return_copies(self):
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.timer_store as ts

            store = ts.TimerStore()
            timer = store.create_timer(_basic_timer("copy-isolation"))

            listed = store.list_timers()
            listed[0]["name"] = "mutated"
            listed[0]["command"]["shell"] = "echo mutated"

            stored = store.get_timer(timer["id"])
            self.assertEqual(stored["name"], "copy-isolation")
            self.assertEqual(stored["command"]["shell"], "echo ok")

            stored["name"] = "mutated-again"
            self.assertEqual(store.get_timer(timer["id"])["name"], "copy-isolation")


class ServiceTests(unittest.TestCase):
    def test_health_reports_monotonic_uptime_and_incident_contract(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            import wakelite.service as service_module

            with patch.object(
                service_module.time,
                "monotonic",
                side_effect=[1000.0, 1005.0, 1065.25],
            ):
                svc = WakeLiteService(tick_seconds=1)
                incident_ids = [
                    svc.state.add_incident(
                        "warn", "test", f"unacknowledged incident {index}"
                    )
                    for index in range(1001)
                ]
                first_health = svc.health()
                svc.state.ack_incident(incident_ids[0])
                second_health = svc.health()

            self.assertEqual(first_health["uptime_seconds"], 5.0)
            self.assertEqual(second_health["uptime_seconds"], 65.25)
            self.assertGreaterEqual(
                second_health["uptime_seconds"], first_health["uptime_seconds"]
            )
            self.assertEqual(first_health["unacked_incidents"], 1001)
            self.assertEqual(second_health["unacked_incidents"], 1000)
            self.assertNotIn("unacknowledged_incidents", second_health)
            index_names = {
                row["name"]
                for row in svc.state._connect()
                .execute("PRAGMA index_list('incidents')")
                .fetchall()
            }
            self.assertIn("idx_incidents_acknowledged", index_names)

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
    def test_process_daemons_clears_ghost_run_with_correct_finish_run_signature(self):
        """Ghost cleanup must not raise when closing a dead daemon run."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer({
                "name": "daemon-ghost",
                "comment": "Synthetic ghost daemon",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "command": {"mode": "shell", "shell": "sleep 999"},
            })
            scheduled_at = "2026-05-14T10:00:00"
            svc.state.reserve_occurrence(timer["id"], scheduled_at, False)
            run = svc.state.create_run(
                timer_id=timer["id"],
                timer_name=timer["name"],
                scheduled_at=scheduled_at,
                is_catchup=False,
                queued_reason="daemon_start",
                timer_snapshot=json.dumps(timer),
            )
            run_id = run["run_id"]
            svc.state.set_runtime_running(timer["id"], run_id, scheduled_at, pid=999999)

            with patch.object(svc, "_spawn_run") as mock_spawn:
                svc._process_daemons(datetime.now())

            finished = svc.state.get_run(run_id)
            self.assertEqual(finished["status"], "failed")
            self.assertIn("ghost run", finished["message"])
            self.assertFalse(svc.state.get_runtime(timer["id"]).is_running)
            mock_spawn.assert_called_once()

    def test_is_daemon_process_alive_uses_spawn_grace_for_young_pidless_run(self):
        """A submitted daemon run without a PID is alive during the spawn grace window."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer({
                "name": "daemon-spawning",
                "comment": "Synthetic daemon still being spawned",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "command": {"mode": "shell", "shell": "sleep 999"},
            })
            scheduled_at = datetime.now(timezone.utc).isoformat()
            svc.state.reserve_occurrence(timer["id"], scheduled_at, False)
            run = svc.state.create_run(
                timer_id=timer["id"],
                timer_name=timer["name"],
                scheduled_at=scheduled_at,
                is_catchup=False,
                queued_reason="daemon_start",
                timer_snapshot=json.dumps(timer),
            )
            run_id = run["run_id"]
            svc.state.set_runtime_running(timer["id"], run_id, scheduled_at)

            self.assertTrue(svc._is_daemon_process_alive(timer["id"], run_id))

            old = (datetime.now(timezone.utc) - timedelta(seconds=svc.DAEMON_SPAWN_GRACE_SECONDS + 5)).isoformat()
            with svc.state._connect() as conn:
                conn.execute(
                    "UPDATE run_history SET started_at = ?, created_at = ? WHERE run_id = ?",
                    (old, old, run_id),
                )
                conn.execute("UPDATE active_runs SET started_at = ? WHERE run_id = ?", (old, run_id))

            self.assertFalse(svc._is_daemon_process_alive(timer["id"], run_id))

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

    def test_shutdown_terminated_daemon_is_not_reported_as_failure(self):
        """A daemon we SIGTERM during stop() is a planned teardown, not a crash.

        Every runner restart tears down its supervised daemons. Before this
        contract those SIGTERMs landed as status="failed" plus a run_failed
        incident plus a desktop alert, so a routine restart was
        indistinguishable from a real crash in run_history.
        """
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer({
                "name": "daemon-shutdown",
                "comment": "Long-lived daemon; killed by stop(), not by its own fault",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "execution": {
                    "restart_on_failure": True,
                    "restart_delay_seconds": 1,
                    "restart_max_backoff_seconds": 300,
                },
                "command": {"mode": "shell", "shell": "sleep 60"},
            })
            timer_id = timer["id"]

            svc.start()
            try:
                run = None
                for _ in range(60):
                    runs = svc.list_runs(limit=5, timer_id=timer_id)
                    if runs and runs[0]["status"] == "started":
                        run = runs[0]
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(run, "daemon never reached status=started")
            finally:
                svc.stop()

            terminal = None
            for _ in range(50):
                runs = svc.list_runs(limit=5, timer_id=timer_id)
                if runs and runs[0]["status"] in (
                    "success", "failed", "aborted", "shutdown", "uncertain_crash", "skipped", "waiting"
                ):
                    terminal = runs[0]
                    break
                time.sleep(0.1)

            self.assertIsNotNone(terminal, "shutdown-terminated run never reached a terminal status")
            self.assertEqual(terminal["status"], "shutdown")
            self.assertIn("shutdown", (terminal.get("message") or "").lower())

            # No run_failed incident for a planned teardown.
            run_failed = svc.state.list_incidents(limit=50, incident_type="run_failed")
            self.assertEqual(
                run_failed, [], f"planned shutdown filed run_failed incidents: {run_failed}"
            )

            # And no desktop failure alert. _bootstrap mocks notifier.notify, so
            # this asserts the no-notification half of the contract without
            # actually posting anything.
            self.assertEqual(
                svc.notifier.notify.call_args_list,
                [],
                "planned shutdown fired a desktop notification",
            )

            # The backoff must not be inflated by a planned stop, otherwise
            # repeated runner restarts pin a healthy daemon at max backoff.
            ds = svc.state.get_daemon_state(timer_id)
            self.assertEqual(ds.current_backoff_seconds, 0)


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

    def test_many_once_timers_different_days_all_fit_at_max_workers_2(self):
        """WL-12: time-axis projection — once-timers at different future dates
        do not share a bucket and must not block each other."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=2)

            from datetime import date, timedelta
            base = date.today() + timedelta(days=1)
            for i in range(12):
                when = base + timedelta(days=i % 6)  # 2 per day, 06:00 and 18:00
                hour = 6 if i % 2 == 0 else 18
                result = svc.create_timer({
                    "name": f"once-{i}",
                    "comment": f"Once {i}",
                    "enabled": True,
                    "recurrence": {"frequency": "once", "date": when.isoformat(), "time": f"{hour:02d}:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                }, idempotency_key=f"once-{i}")
                self.assertIn("timer", result)

    def test_three_dailies_same_hour_blocks_at_max_workers_2(self):
        """WL-12: three daily-06:00 timers share the same bucket → 3rd blocked."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=2)

            for i in range(2):
                svc.timer_store.create_timer({
                    "name": f"dawn-{i}",
                    "comment": f"Dawn {i}",
                    "enabled": True,
                    "recurrence": {"frequency": "daily", "time": "06:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                })

            from wakelite.service import CapacityExceededError
            with self.assertRaises(CapacityExceededError):
                svc.create_timer({
                    "name": "dawn-overflow",
                    "comment": "Third daily at 06:00 should be blocked",
                    "enabled": True,
                    "recurrence": {"frequency": "daily", "time": "06:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                }, idempotency_key="dawn-overflow")

    def test_user_resource_capacity_gates_simultaneous_timers(self):
        """WL-12: user-declared resource with parseable capacity participates
        in the gate. Two 30 req/min dailies at 06:00 would peak at 60 — above
        the 50 req/min capacity — so the second must be blocked."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=16)

            svc.timer_store.create_timer({
                "name": "slack-poller",
                "comment": "First Slack consumer",
                "enabled": True,
                "recurrence": {"frequency": "daily", "time": "06:00"},
                "command": {"mode": "shell", "shell": "echo ok"},
                "resources": [{"name": "slack-api", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
            })

            from wakelite.service import CapacityExceededError
            with self.assertRaises(CapacityExceededError) as ctx:
                svc.create_timer({
                    "name": "slack-notifier",
                    "comment": "Second Slack consumer at the same minute",
                    "enabled": True,
                    "recurrence": {"frequency": "daily", "time": "06:00"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                    "resources": [{"name": "slack-api", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
                }, idempotency_key="slack-overflow")
            self.assertIn("slack-api", str(ctx.exception))

    def test_twenty_once_timers_at_default_max_workers_succeed(self):
        """WL-12 AC #1 (strict): 20 one-shot timers scheduled at different
        dates — all succeed at the default max_workers=16."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)  # default max_workers=16

            from datetime import date, timedelta
            base = date.today() + timedelta(days=1)
            for i in range(20):
                when = base + timedelta(days=i % 7)
                hour = 3 + (i % 16)  # stagger hours
                result = svc.create_timer({
                    "name": f"once-strict-{i}",
                    "comment": f"Once {i}",
                    "enabled": True,
                    "recurrence": {"frequency": "once", "date": when.isoformat(), "time": f"{hour:02d}:{(i * 5) % 60:02d}"},
                    "command": {"mode": "shell", "shell": "echo ok"},
                }, idempotency_key=f"once-strict-{i}")
                self.assertIn("timer", result)

    def test_twenty_daemons_blocked_past_max_workers(self):
        """WL-12 AC #2 (strict): adding many daemons is capped by max_workers,
        not by a separate "max number of configured timers" budget."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=3)

            # First 3 succeed.
            for i in range(3):
                svc.create_timer({
                    "name": f"daemon-{i}",
                    "comment": f"Daemon {i}",
                    "enabled": True,
                    "timer_type": "daemon",
                    "recurrence": {"frequency": "interval", "every": "0s"},
                    "command": {"mode": "shell", "shell": "sleep 999"},
                }, idempotency_key=f"daemon-{i}")

            # Attempts 4..20 all fail on the executor-slot resource.
            from wakelite.service import CapacityExceededError
            for i in range(3, 20):
                with self.assertRaises(CapacityExceededError) as ctx:
                    svc.create_timer({
                        "name": f"daemon-{i}",
                        "comment": f"Daemon {i}",
                        "enabled": True,
                        "timer_type": "daemon",
                        "recurrence": {"frequency": "interval", "every": "0s"},
                        "command": {"mode": "shell", "shell": "sleep 999"},
                    }, idempotency_key=f"daemon-{i}")
                self.assertIn("_executor.slot", str(ctx.exception))

    def test_user_resource_at_different_times_coexist(self):
        """WL-12: same resource, different clock times → no peak overlap → admit."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15, max_workers=16)

            svc.timer_store.create_timer({
                "name": "slack-morning",
                "comment": "Morning Slack consumer",
                "enabled": True,
                "recurrence": {"frequency": "daily", "time": "06:00"},
                "command": {"mode": "shell", "shell": "echo ok"},
                "resources": [{"name": "slack-api", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
            })

            result = svc.create_timer({
                "name": "slack-evening",
                "comment": "Evening Slack consumer",
                "enabled": True,
                "recurrence": {"frequency": "daily", "time": "18:00"},
                "command": {"mode": "shell", "shell": "echo ok"},
                "resources": [{"name": "slack-api", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
            }, idempotency_key="slack-evening")
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


def _cmux_callback_timer(name: str, shell: str = "echo callback-ok",
                         workspace_id: str = "ws-123", surface_id: str = "sf-456",
                         session_id: str = "sess-cmux", cli_path: str = "/tmp/cmux",
                         amq: bool = False):
    t = _basic_timer(name, shell)
    t["callback"] = {
        "type": "cmux",
        "workspace_id": workspace_id,
        "surface_id": surface_id,
        "session_id": session_id,
        "cli_path": cli_path,
    }
    if amq:
        t["callback"]["amq"] = True
    return t


def _make_executable(path: Path) -> str:
    path.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _stdout_file(root: str, text: str = "cmux output") -> str:
    path = Path(root) / "stdout.log"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _cmux_signal_files(home: str, session_id: str):
    signal_dir = Path(home) / ".claude" / "session-signals"
    return list(signal_dir.glob(f"{session_id}.*.wakelite-callback.json"))


def _write_amq_callback_identity(
    home: str,
    session_id: str = "sess-cmux",
    workspace_id: str = "ws-fresh",
    surface_id: str = "sf-fresh",
    recipient: str = "claude",
) -> Path:
    """Write the two live identity sources WakeLite resolves at fire time."""
    cmux_dir = Path(home) / ".cmuxterm"
    cmux_dir.mkdir(parents=True, exist_ok=True)
    (cmux_dir / "claude-hook-sessions.json").write_text(
        json.dumps({
            "sessions": {session_id: {
                "sessionId": session_id,
                "workspaceId": workspace_id,
                "surfaceId": surface_id,
                "updatedAt": datetime.now(timezone.utc).timestamp(),
            }}
        }),
        encoding="utf-8",
    )
    (cmux_dir / "claude-hook-sessions.json.lock").write_text("", encoding="utf-8")

    root = Path(home) / "amq-root"
    (root / "agents" / recipient).mkdir(parents=True, exist_ok=True)
    registry_dir = Path(home) / ".amq-keepalive"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.json").write_text(
        json.dumps({
            "schema_version": 1,
            "entries": [{
                "adapter": "cmux",
                "target": f"cmux:surface:{surface_id}",
                "state": "active",
                "root": str(root),
                "agent": recipient,
            }],
        }),
        encoding="utf-8",
    )
    return root


def _is_amq_wake_check(args) -> bool:
    return args[:3] == ["/opt/homebrew/bin/amq", "wake", "check"]


def _amq_wake_check_result(
    args,
    *,
    returncode: int = 0,
    status: str = "valid",
    live: bool = True,
    stdout: Optional[str] = None,
    stderr: str = "",
):
    root = args[args.index("--root") + 1]
    recipient = args[args.index("--me") + 1]
    if stdout is None:
        stdout = json.dumps({
            "schema": 2,
            "root": root,
            "agent": recipient,
            "wake": {"status": status, "live": live},
        })
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


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


class CmuxCallbackTests(unittest.TestCase):
    """Tests for the cmux callback feature."""

    def test_new_resumable_cmux_callback_defaults_to_amq(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")

            timer = svc.timer_store.create_timer(
                _cmux_callback_timer("cmux-amq-default", cli_path=cmux)
            )

            self.assertIs(timer["callback"]["amq"], True)

    def test_new_cmux_callback_preserves_explicit_amq_opt_out(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            payload = _cmux_callback_timer("cmux-amq-opt-out", cli_path=cmux)
            payload["callback"]["amq"] = False

            timer = svc.timer_store.create_timer(payload)

            self.assertIs(timer["callback"]["amq"], False)

    def test_new_cmux_callback_without_session_id_does_not_default_to_amq(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            payload = _cmux_callback_timer("cmux-no-session", cli_path=cmux)
            payload["callback"].pop("session_id")

            timer = svc.timer_store.create_timer(payload)

            self.assertNotIn("amq", timer["callback"])

    def test_replace_all_preserves_legacy_cmux_callback_without_amq(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            legacy = _cmux_callback_timer("legacy-cmux", cli_path=cmux)
            legacy.update(
                {
                    "id": "legacy-cmux-id",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00",
                }
            )

            svc.timer_store.replace_all([legacy])

            stored = svc.timer_store.get_timer("legacy-cmux-id")
            self.assertNotIn("amq", stored["callback"])

            updated = svc.timer_store.update_timer(
                "legacy-cmux-id",
                {"name": "legacy-cmux-updated"},
            )
            self.assertNotIn("amq", updated["callback"])

    def test_callback_schema_accepts_cmux(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = svc.timer_store.create_timer(
                _cmux_callback_timer("cmux-valid", cli_path=cmux)
            )
            self.assertEqual(timer["callback"]["type"], "cmux")
            self.assertEqual(timer["callback"]["workspace_id"], "ws-123")
            self.assertEqual(timer["callback"]["surface_id"], "sf-456")

    def test_callback_schema_accepts_only_boolean_amq_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            accepted = svc.timer_store.create_timer(
                _cmux_callback_timer("cmux-amq-valid", cli_path=cmux, amq=True)
            )
            self.assertIs(accepted["callback"]["amq"], True)

            invalid = _cmux_callback_timer("cmux-amq-invalid", cli_path=cmux)
            invalid["callback"]["amq"] = "true"
            with self.assertRaisesRegex(ValueError, "callback.amq must be a boolean"):
                svc.timer_store.create_timer(invalid)

            explicit_null = _cmux_callback_timer("cmux-amq-null", cli_path=cmux)
            explicit_null["callback"]["amq"] = None
            with self.assertRaisesRegex(ValueError, "callback.amq must be a boolean"):
                svc.timer_store.create_timer(explicit_null)

            typo = _cmux_callback_timer("cmux-ammq-typo", cli_path=cmux)
            typo["callback"]["ammq"] = True
            with self.assertRaisesRegex(ValueError, "Unknown keys in callback"):
                svc.timer_store.create_timer(typo)

            for non_cmux in (
                _callback_timer("wezterm-amq-invalid"),
                _ghostty_callback_timer("ghostty-amq-invalid"),
            ):
                with self.subTest(callback_type=non_cmux["callback"]["type"]):
                    non_cmux["callback"]["amq"] = True
                    with self.assertRaisesRegex(
                        ValueError, "callback.amq is only valid for cmux callbacks"
                    ):
                        svc.timer_store.create_timer(non_cmux)

    def test_cmux_probe_uses_runtime_dead_surface_and_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = Path("/Applications/cmux.app/Contents/Resources/bin/cmux")
            if not cmux.is_file() or not os.access(cmux, os.X_OK):
                self.skipTest("cmux bundled CLI is unavailable")

            runtime_dead_surface = str(uuid.uuid4()).upper()
            self.assertEqual(
                svc._cmux_probe_liveness(str(cmux), os.environ.copy(), runtime_dead_surface),
                "dead",
            )
            with patch.object(svc, "_cmux_run", return_value=None):
                self.assertEqual(
                    svc._cmux_probe_liveness(str(cmux), os.environ.copy(), runtime_dead_surface),
                    "unknown",
                )

    def test_amq_send_acceptance_delivers_signal_body_and_skips_cmux(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            root = _write_amq_callback_identity(td)
            timer = svc.timer_store.create_timer(
                _cmux_callback_timer("cmux-amq-success", cli_path=cmux)
            )
            self.assertIs(timer["callback"]["amq"], True)
            observed = {}

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                if _is_amq_wake_check(args):
                    observed["wake_argv"] = args
                    observed["wake_kwargs"] = kwargs
                    return _amq_wake_check_result(args)
                if args[0] == "/opt/homebrew/bin/amq":
                    signal_arg = args[args.index("--body") + 1]
                    observed["argv"] = args
                    observed["kwargs"] = kwargs
                    observed["body"] = Path(signal_arg[1:]).read_bytes()
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout=json.dumps({
                            "id": "msg-sent-1",
                        }),
                        stderr="",
                    )
                raise AssertionError(f"cmux injection must not run after AMQ accepts the send: {args}")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect), \
                 self.assertLogs("wakelite.service", level="INFO") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-amq-sent", _stdout_file(td, "payload-from-run"), 1.0
                )

            signal_arg = observed["argv"][observed["argv"].index("--body") + 1]
            expected = [
                "/opt/homebrew/bin/amq", "send", "--root", str(root),
                "--ignore-session-pin",
                "--me", "claude", "--to", "claude", "--allow-self",
                "--body", signal_arg,
                "--strict", "--json",
            ]
            self.assertEqual(observed["argv"], expected)
            self.assertEqual(observed["kwargs"]["timeout"], 5)
            self.assertEqual(observed["wake_argv"], [
                "/opt/homebrew/bin/amq", "wake", "check",
                "--root", str(root), "--me", "claude",
                "--json", "--json-schema=2",
            ])
            self.assertEqual(observed["wake_kwargs"]["timeout"], 5)
            payload = json.loads(observed["body"])
            self.assertEqual(payload["run_id"], "run-amq-sent")
            self.assertEqual(payload["stdout_tail"], "payload-from-run")
            self.assertFalse(_cmux_signal_files(td, "sess-cmux"))
            route_log = "\n".join(logs.output)
            self.assertIn("route=amq", route_log)
            self.assertIn("outcome=amq-sent", route_log)
            self.assertIn("amq_message_id=msg-sent-1", route_log)

    def test_amq_session_store_miss_uses_live_captured_codex_surface(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            root = _write_amq_callback_identity(
                td, workspace_id="ws-codex", surface_id="sf-codex"
            )
            cmux_dir = Path(td) / ".cmuxterm"
            (cmux_dir / "claude-hook-sessions.json").unlink()
            (cmux_dir / "claude-hook-sessions.json.lock").unlink()
            timer = _cmux_callback_timer(
                "cmux-amq-codex-store-miss",
                workspace_id="ws-codex",
                surface_id="sf-codex",
                cli_path=cmux,
                amq=True,
            )

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                if _is_amq_wake_check(args):
                    return _amq_wake_check_result(args)
                if args[0] == "/opt/homebrew/bin/amq":
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout=json.dumps({"id": "msg-codex-live"}),
                        stderr="",
                    )
                raise AssertionError(f"direct cmux injection must not run: {args}")

            with patch(
                "wakelite.service.subprocess.run", side_effect=side_effect
            ) as mock_run, self.assertLogs("wakelite.service", level="INFO") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-codex-live", _stdout_file(td), 1.0
                )

            amq_call = next(
                call.args[0]
                for call in mock_run.call_args_list
                if call.args[0][0] == "/opt/homebrew/bin/amq"
            )
            self.assertEqual(amq_call[amq_call.index("--root") + 1], str(root))
            self.assertFalse(_cmux_signal_files(td, "sess-cmux"))
            log_text = "\n".join(logs.output)
            self.assertIn(
                "using live captured workspace=ws-codex surface=sf-codex",
                log_text,
            )
            self.assertIn("route=amq", log_text)
            self.assertIn("amq_message_id=msg-codex-live", log_text)

    def test_amq_session_store_miss_rejects_unproven_captured_surface(self):
        for liveness in ("dead", "unknown"):
            with self.subTest(liveness=liveness), tempfile.TemporaryDirectory() as td:
                WakeLiteService = _bootstrap(td)
                svc = WakeLiteService(tick_seconds=1)
                cmux = _make_executable(Path(td) / "cmux")
                _write_amq_callback_identity(
                    td, workspace_id="ws-codex", surface_id="sf-codex"
                )
                cmux_dir = Path(td) / ".cmuxterm"
                (cmux_dir / "claude-hook-sessions.json").unlink()
                (cmux_dir / "claude-hook-sessions.json.lock").unlink()
                timer = _cmux_callback_timer(
                    f"cmux-amq-codex-{liveness}",
                    workspace_id="ws-codex",
                    surface_id="sf-codex",
                    cli_path=cmux,
                    amq=True,
                )

                def side_effect(args, **kwargs):
                    if args[0] == "/opt/homebrew/bin/amq":
                        raise AssertionError("unproven captured target must not use AMQ")
                    if args[1:3] == ["rpc", "surface.read_text"]:
                        if liveness == "unknown":
                            raise subprocess.TimeoutExpired(args, 5)
                        return subprocess.CompletedProcess(
                            args, 1, stdout="", stderr="surface not found"
                        )
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

                with patch(
                    "wakelite.service.subprocess.run", side_effect=side_effect
                ) as mock_run:
                    svc._execute_callback(
                        timer,
                        "success",
                        0,
                        f"run-codex-{liveness}",
                        _stdout_file(td),
                        1.0,
                    )

                calls = [call.args[0] for call in mock_run.call_args_list]
                self.assertFalse(
                    any(args[0] == "/opt/homebrew/bin/amq" for args in calls)
                )
                self.assertTrue(
                    any(args[0] == cmux and args[1] == "send" for args in calls)
                )
                self.assertTrue(_cmux_signal_files(td, "sess-cmux"))

    def test_amq_dead_target_uses_existing_new_workspace_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            _write_amq_callback_identity(td)
            timer = _cmux_callback_timer("cmux-amq-dead", cli_path=cmux, amq=True)

            def side_effect(args, **kwargs):
                if args[0] == "/opt/homebrew/bin/amq":
                    raise AssertionError("a known-dead target must not be routed through AMQ")
                if args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="surface not found")
                if args[1] == "send" and "--surface" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="surface not found")
                if args[1] == "new-workspace":
                    return subprocess.CompletedProcess(args, 0, stdout="OK ws-new\n", stderr="")
                if args[1] == "read-screen":
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run, \
                 patch("wakelite.service.time.sleep"):
                svc._execute_callback(
                    timer, "success", 0, "run-amq-dead", _stdout_file(td), 1.0
                )

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertTrue(any(args[1] == "new-workspace" for args in calls))
            self.assertIn(
                [cmux, "send", "--workspace", "ws-new", "--", "claude --resume sess-cmux"],
                calls,
            )
            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))

    def test_amq_rejected_send_falls_back_and_preserves_signal(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            _write_amq_callback_identity(td)
            timer = _cmux_callback_timer("cmux-amq-rejected", cli_path=cmux, amq=True)

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                if _is_amq_wake_check(args):
                    return _amq_wake_check_result(args)
                if args[0] == "/opt/homebrew/bin/amq":
                    return subprocess.CompletedProcess(
                        args,
                        1,
                        stdout=json.dumps({
                            "id": "msg-rejected",
                        }),
                        stderr="",
                    )
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run, \
                 self.assertLogs("wakelite.service", level="INFO") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-amq-rejected", _stdout_file(td), 1.0
                )

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertIn(
                [cmux, "send", "--workspace", "ws-123", "--surface", "sf-456", "--",
                 "[WakeLite: cmux-amq-rejected completed (success)]"],
                calls,
            )
            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
            route_log = "\n".join(logs.output)
            self.assertIn("route=cmux_fallback", route_log)
            self.assertIn("outcome=fallback-delivered", route_log)
            self.assertIn("amq_message_id=msg-rejected", route_log)

    def test_amq_and_cmux_failure_returns_delivery_failed(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            _write_amq_callback_identity(td)
            timer = _cmux_callback_timer("cmux-amq-double-failure", cli_path=cmux, amq=True)

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                if _is_amq_wake_check(args):
                    return _amq_wake_check_result(args)
                if args[0] == "/opt/homebrew/bin/amq":
                    return subprocess.CompletedProcess(
                        args,
                        1,
                        stdout=json.dumps({
                            "id": "msg-double-failure",
                        }),
                        stderr="",
                    )
                if args[0] == cmux and args[1] == "send":
                    return subprocess.CompletedProcess(
                        args, 1, stdout="", stderr="socket unavailable"
                    )
                raise AssertionError(f"unexpected subprocess call: {args}")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect), \
                 self.assertLogs("wakelite.service", level="INFO") as logs:
                outcome = svc._cmux_callback(
                    timer,
                    "run-amq-double-failure",
                    "success",
                    0,
                    "1s",
                    "payload-from-run",
                    timer["callback"],
                )

            self.assertEqual(outcome, "delivery-failed")
            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
            route_log = "\n".join(logs.output)
            self.assertIn("route=cmux_fallback", route_log)
            self.assertIn("outcome=delivery-failed", route_log)
            self.assertIn("amq_message_id=msg-double-failure", route_log)

    def test_amq_rc_zero_without_nonempty_message_id_falls_back(self):
        cases = (
            ("missing-id", json.dumps({})),
            ("empty-id", json.dumps({"id": "  "})),
            ("malformed-json", "not-json"),
        )
        for case, stdout in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                WakeLiteService = _bootstrap(td)
                svc = WakeLiteService(tick_seconds=1)
                cmux = _make_executable(Path(td) / "cmux")
                _write_amq_callback_identity(td)
                timer = _cmux_callback_timer(
                    f"cmux-amq-{case}", cli_path=cmux, amq=True
                )

                def side_effect(args, **kwargs):
                    if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                        return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                    if _is_amq_wake_check(args):
                        return _amq_wake_check_result(args)
                    if args[0] == "/opt/homebrew/bin/amq":
                        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

                with patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run, \
                     self.assertLogs("wakelite.service", level="WARNING") as logs:
                    svc._execute_callback(
                        timer, "success", 0, f"run-{case}", _stdout_file(td), 1.0
                    )

                calls = [call.args[0] for call in mock_run.call_args_list]
                self.assertTrue(any(args[0] == cmux and args[1] == "send" for args in calls))
                self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
                log_text = "\n".join(logs.output)
                self.assertIn("AMQ callback send was not accepted", log_text)
                if case == "malformed-json":
                    self.assertIn("Unable to parse AMQ callback JSON response", log_text)

    def test_amq_missing_identity_fails_closed_to_cmux(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            _write_amq_callback_identity(td)
            (Path(td) / ".amq-keepalive" / "registry.json").unlink()
            timer = _cmux_callback_timer("cmux-amq-no-identity", cli_path=cmux, amq=True)

            with patch("wakelite.service.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
                svc._execute_callback(
                    timer, "success", 0, "run-amq-no-identity", _stdout_file(td), 1.0
                )

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertFalse(any(args and args[0] == "/opt/homebrew/bin/amq" for args in calls))
            self.assertTrue(any(args[0] == cmux and args[1] == "send" for args in calls))
            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))

    def test_amq_ambiguous_or_detached_identity_fails_closed_to_cmux(self):
        for case in ("ambiguous", "detached"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                WakeLiteService = _bootstrap(td)
                svc = WakeLiteService(tick_seconds=1)
                cmux = _make_executable(Path(td) / "cmux")
                _write_amq_callback_identity(td)
                registry = Path(td) / ".amq-keepalive" / "registry.json"
                payload = json.loads(registry.read_text(encoding="utf-8"))
                if case == "detached":
                    payload["entries"][0]["state"] = case
                else:
                    second_root = Path(td) / "amq-root-second"
                    (second_root / "agents" / "claude").mkdir(parents=True)
                    second = dict(payload["entries"][0])
                    second["root"] = str(second_root)
                    payload["entries"].append(second)
                registry.write_text(json.dumps(payload), encoding="utf-8")
                timer = _cmux_callback_timer(
                    f"cmux-amq-{case}-identity", cli_path=cmux, amq=True
                )

                with patch("wakelite.service.subprocess.run") as mock_run:
                    mock_run.return_value = subprocess.CompletedProcess(
                        [], 0, stdout="", stderr=""
                    )
                    svc._execute_callback(
                        timer, "success", 0, f"run-{case}-identity", _stdout_file(td), 1.0
                    )

                calls = [call.args[0] for call in mock_run.call_args_list]
                self.assertFalse(
                    any(args and args[0] == "/opt/homebrew/bin/amq" for args in calls)
                )
                self.assertTrue(any(args[0] == cmux and args[1] == "send" for args in calls))
                self.assertTrue(_cmux_signal_files(td, "sess-cmux"))

    def test_amq_attached_identity_with_live_official_wake_routes(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            root = _write_amq_callback_identity(td)
            registry = Path(td) / ".amq-keepalive" / "registry.json"
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["entries"][0].update({
                "state": "attached",
                "decision": "start_failed",
                "last_error": "owner-bound wake cannot be claimed by supervisor",
            })
            registry.write_text(json.dumps(payload), encoding="utf-8")
            timer = _cmux_callback_timer(
                "cmux-amq-attached-live", cli_path=cmux, amq=True
            )

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                if _is_amq_wake_check(args):
                    return _amq_wake_check_result(args)
                if args[:2] == ["/opt/homebrew/bin/amq", "send"]:
                    return subprocess.CompletedProcess(
                        args, 0, stdout=json.dumps({"id": "msg-attached-live"}), stderr=""
                    )
                raise AssertionError(f"direct cmux injection must not run: {args}")

            with patch(
                "wakelite.service.subprocess.run", side_effect=side_effect
            ) as mock_run:
                svc._execute_callback(
                    timer, "success", 0, "run-attached-live", _stdout_file(td), 1.0
                )

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertIn(
                [
                    "/opt/homebrew/bin/amq", "wake", "check",
                    "--root", str(root), "--me", "claude",
                    "--json", "--json-schema=2",
                ],
                calls,
            )
            self.assertTrue(
                any(args[:2] == ["/opt/homebrew/bin/amq", "send"] for args in calls)
            )
            self.assertFalse(any(args[0] == cmux and args[1] == "send" for args in calls))
            self.assertFalse(_cmux_signal_files(td, "sess-cmux"))

    def test_amq_wake_check_failure_falls_back_and_preserves_signal(self):
        cases = (
            "not-live",
            "nonzero",
            "non-object",
            "malformed",
            "wrong-identity",
            "timeout",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                WakeLiteService = _bootstrap(td)
                svc = WakeLiteService(tick_seconds=1)
                cmux = _make_executable(Path(td) / "cmux")
                _write_amq_callback_identity(td)
                timer = _cmux_callback_timer(
                    f"cmux-amq-wake-{case}", cli_path=cmux, amq=True
                )

                def side_effect(args, **kwargs):
                    if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                        return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                    if _is_amq_wake_check(args):
                        if case == "not-live":
                            return _amq_wake_check_result(
                                args, status="missing", live=False
                            )
                        if case == "nonzero":
                            return _amq_wake_check_result(
                                args,
                                returncode=1,
                                stdout="",
                                stderr="official wake check rejected the request",
                            )
                        if case == "non-object":
                            return _amq_wake_check_result(args, stdout="[]")
                        if case == "malformed":
                            return _amq_wake_check_result(args, stdout="not-json")
                        if case == "wrong-identity":
                            return _amq_wake_check_result(
                                args,
                                stdout=json.dumps({
                                    "schema": 2,
                                    "root": "/wrong/root",
                                    "agent": "wrong-agent",
                                    "wake": {"status": "valid", "live": True},
                                }),
                            )
                        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                    if args[:2] == ["/opt/homebrew/bin/amq", "send"]:
                        raise AssertionError("unusable wake must not receive AMQ send")
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

                with patch(
                    "wakelite.service.subprocess.run", side_effect=side_effect
                ) as mock_run, self.assertLogs(
                    "wakelite.service", level="WARNING"
                ) as logs:
                    svc._execute_callback(
                        timer,
                        "success",
                        0,
                        f"run-wake-{case}",
                        _stdout_file(td),
                        1.0,
                    )

                calls = [call.args[0] for call in mock_run.call_args_list]
                self.assertEqual(sum(_is_amq_wake_check(args) for args in calls), 1)
                self.assertFalse(
                    any(args[:2] == ["/opt/homebrew/bin/amq", "send"] for args in calls)
                )
                self.assertTrue(any(args[0] == cmux and args[1] == "send" for args in calls))
                self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
                if case == "nonzero":
                    self.assertIn(
                        "official wake check rejected the request",
                        "\n".join(logs.output),
                    )

    def test_amq_send_timeout_uses_send_scoped_backstop(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            _write_amq_callback_identity(td)
            timer = _cmux_callback_timer("cmux-amq-timeout", cli_path=cmux, amq=True)
            amq_timeouts = []

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    return subprocess.CompletedProcess(args, 0, stdout="$ ", stderr="")
                if _is_amq_wake_check(args):
                    return _amq_wake_check_result(args)
                if args[0] == "/opt/homebrew/bin/amq":
                    amq_timeouts.append(kwargs["timeout"])
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run, \
                 self.assertLogs("wakelite.service", level="WARNING") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-amq-timeout", _stdout_file(td), 1.0
                )

            self.assertEqual(amq_timeouts, [5])
            amq_call = next(
                args for args in (call.args[0] for call in mock_run.call_args_list)
                if args[:2] == ["/opt/homebrew/bin/amq", "send"]
            )
            self.assertNotIn("--wait-for", amq_call)
            self.assertNotIn("--wait-timeout", amq_call)
            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertTrue(any(args[1] == "send" for args in calls if args[0] == cmux))
            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
            self.assertIn("falling back to cmux", "\n".join(logs.output))

    def test_amq_unknown_liveness_still_routes(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            _write_amq_callback_identity(td)
            timer = _cmux_callback_timer("cmux-amq-unknown", cli_path=cmux, amq=True)

            def side_effect(args, **kwargs):
                if args[0] == cmux and args[1:3] == ["rpc", "surface.read_text"]:
                    raise subprocess.TimeoutExpired(args, 5)
                if _is_amq_wake_check(args):
                    return _amq_wake_check_result(args)
                if args[0] == "/opt/homebrew/bin/amq":
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout=json.dumps({"id": "msg-unknown"}),
                        stderr="",
                    )
                raise AssertionError(f"legacy cmux injection must not run: {args}")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run:
                svc._execute_callback(
                    timer, "success", 0, "run-amq-unknown", _stdout_file(td), 1.0
                )

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertTrue(any(args[0] == "/opt/homebrew/bin/amq" for args in calls))
            self.assertFalse(_cmux_signal_files(td, "sess-cmux"))

    def test_amq_kill_switch_skips_route_and_keeps_legacy_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer("cmux-amq-disabled", cli_path=cmux, amq=True)

            with patch.dict(os.environ, {"WAKELITE_AMQ_CALLBACK_ENABLED": "false"}, clear=False), \
                 patch.object(svc, "_deliver_cmux_callback_via_amq",
                              side_effect=AssertionError("kill switch must skip AMQ")), \
                 patch("wakelite.service.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
                svc._execute_callback(
                    timer, "success", 0, "run-amq-disabled", _stdout_file(td), 1.0
                )

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertEqual(calls[0][1], "send")
            self.assertEqual(calls[1][1], "send-key")
            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))

    def test_callback_normalizes_panel_id_alias_to_surface_id(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            payload = _basic_timer("cmux-panel-alias")
            payload["callback"] = {
                "type": "cmux",
                "workspace_id": "ws-alias",
                "panel_id": "sf-alias",
            }
            timer = svc.timer_store.create_timer(payload)
            self.assertEqual(timer["callback"]["surface_id"], "sf-alias")
            self.assertNotIn("panel_id", timer["callback"])

    def test_cmux_callback_calls_send_then_send_key(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer("cmux-send-order", cli_path=cmux)

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch("wakelite.service.time.sleep"):
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._execute_callback(timer, "success", 0, "run-cmux-1", _stdout_file(td), 1.0)

            calls = [call.args[0] for call in mock_run.call_args_list]
            self.assertEqual(calls[0], [cmux, "send", "--workspace", "ws-123", "--surface", "sf-456", "--", "[WakeLite: cmux-send-order completed (success)]"])
            self.assertEqual(calls[1], [cmux, "send-key", "--workspace", "ws-123", "--surface", "sf-456", "Enter"])

    def test_cmux_callback_send_key_failure_preserves_signal_file(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer("cmux-partial", cli_path=cmux)

            def side_effect(args, **kwargs):
                if "send-key" in args:
                    return unittest.mock.Mock(returncode=1, stdout="", stderr="key failed")
                return unittest.mock.Mock(returncode=0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect), \
                 patch("wakelite.service.time.sleep"), \
                 self.assertLogs("wakelite.service", level="WARNING") as logs:
                svc._execute_callback(timer, "success", 0, "run-cmux-2", _stdout_file(td), 1.0)

            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
            self.assertIn("partial injection", "\n".join(logs.output))

    def test_cmux_session_bound_skips_slack_start_thread(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = svc.timer_store.create_timer(
                _cmux_callback_timer("cmux-session-bound", "echo ok", cli_path=cmux)
            )

            with patch("wakelite.service.subprocess.run") as mock_run, \
                 patch.object(svc.notifier, "notify_slack") as mock_slack:
                mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
                svc._schedule_occurrence(timer, "2026-04-27T10:00:00", is_catchup=False, queued_reason=None)
                time.sleep(1.5)

            started_calls = [
                call for call in mock_slack.call_args_list
                if call.args and "started" in call.args[0].lower()
            ]
            self.assertEqual(started_calls, [])

    def test_cmux_stale_surface_falls_back_to_session_store(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer("cmux-store-fallback", cli_path=cmux)
            store = Path(td) / "claude-hook-sessions.json"
            lock = Path(td) / "claude-hook-sessions.lock"
            # CC-95 (HIGH#3): session-store entries are filtered against a
            # 24h cutoff. Use a relative timestamp so the test stays green
            # regardless of the wall clock at run time — a hardcoded
            # absolute date would pass on the day it's written and fail
            # one day later.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            recent_iso = (_dt.now(_tz.utc) - _td(minutes=5)).isoformat()
            store.write_text(json.dumps({
                "sessions": [{
                    "sessionId": "sess-cmux",
                    "workspaceId": "ws-fresh",
                    "surfaceId": "sf-fresh",
                    "updatedAt": recent_iso,
                }]
            }), encoding="utf-8")
            lock.write_text("", encoding="utf-8")

            def side_effect(args, **kwargs):
                if args[1] == "send" and args[3] == "ws-123":
                    return unittest.mock.Mock(returncode=1, stdout="", stderr="surface not found")
                return unittest.mock.Mock(returncode=0, stdout="", stderr="")

            env = {
                "WAKELITE_CMUX_SESSION_STORE_PATH": str(store),
                "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH": str(lock),
            }
            with patch.dict(os.environ, env, clear=False), \
                 patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run, \
                 patch("wakelite.service.time.sleep"):
                svc._execute_callback(timer, "success", 0, "run-cmux-3", _stdout_file(td), 1.0)

            send_calls = [call.args[0] for call in mock_run.call_args_list if call.args[0][1] == "send"]
            self.assertEqual(send_calls[1][3], "ws-fresh")
            self.assertEqual(send_calls[1][5], "sf-fresh")

    def test_cmux_missing_cli_logs_error_keeps_signal_file(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            missing = str(Path(td) / "missing-cmux")
            timer = _cmux_callback_timer("cmux-missing-cli", cli_path=missing)

            # Task #6 polish: pin subprocess.run so the test can't accidentally
            # depend on a real cmux binary on PATH bleeding through. The
            # missing-CLI path should NEVER reach subprocess.run because the
            # early-return triggers on the cli_path os.path.isfile check; this
            # mock asserts that contract by failing loudly if the early-return
            # is bypassed.
            with patch("wakelite.service.subprocess.run", side_effect=AssertionError(
                "subprocess.run must not be called when cli_path is missing"
            )) as mock_run, \
                 self.assertLogs("wakelite.service", level="ERROR") as logs:
                svc._execute_callback(timer, "success", 0, "run-cmux-4", _stdout_file(td), 1.0)

            self.assertTrue(_cmux_signal_files(td, "sess-cmux"))
            self.assertIn("cmux CLI not found", "\n".join(logs.output))
            mock_run.assert_not_called()

    def test_cmux_callback_writes_recovery_signal_when_session_id_missing(self):
        """CC-95 HIGH#4: a cmux-callback timer with no `session_id` must still
        persist its payload to disk before any early-return. Pre-fix, the
        signal-file write lived inside the ``if session_id:`` branch in
        ``_cmux_callback`` (service.py:967), so timers that were never bound
        to a Claude session lost their payload whenever cmux delivery failed
        (missing CLI / missing workspace_id+surface_id / stale target with no
        session-store fallback). Recovery files for unbound timers land under
        ``_no-session.<timer_id>.<run_id>.wakelite-callback.json`` and carry
        the full status payload so a future operator or tool can recover."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            missing = str(Path(td) / "missing-cmux")
            timer = _basic_timer("cmux-no-session", "echo hi")
            timer["callback"] = {
                "type": "cmux",
                "workspace_id": "ws-orphan",
                "surface_id": "sf-orphan",
                "cli_path": missing,
            }

            with self.assertLogs("wakelite.service", level="INFO") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-cmux-no-sess", _stdout_file(td), 1.0
                )

            signal_dir = Path(td) / ".claude" / "session-signals"
            recovery = list(
                signal_dir.glob(
                    "_no-session.*.run-cmux-no-sess.wakelite-callback.json"
                )
            )
            self.assertTrue(
                recovery,
                f"expected _no-session recovery file in {signal_dir}, got: "
                f"{list(signal_dir.iterdir()) if signal_dir.exists() else 'no dir'}",
            )

            payload = json.loads(recovery[0].read_text())
            self.assertEqual(payload["timer_name"], "cmux-no-session")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["run_id"], "run-cmux-no-sess")

            # Session-keyed file MUST NOT be created — there is no session.
            self.assertFalse(_cmux_signal_files(td, "sess-cmux"))

            log_text = "\n".join(logs.output)
            # The missing-CLI ERROR is preserved (durability is the only
            # behavior change — error semantics unchanged).
            self.assertIn("cmux CLI not found", log_text)
            # Recovery write was logged at INFO before the early-return.
            self.assertIn("_no-session.", log_text)

    def test_cmux_callback_recovery_slug_sanitizes_hostile_timer_id(self):
        """Task #6 polish: table-driven over multiple hostile inputs to exercise
        each substitution + strip + truncation rule independently. A single
        sample (the original test's input) only happens to cover three of the
        five rules; widening the matrix prevents regressions where one rule
        weakens silently.

        Cases cover:
        - Path-traversal + control chars + spaces + colons + newlines
        - Pure path-separator-only id
        - Empty string after substitution + strip (must collapse to "unknown")
        - Whitespace-only id (same fallback)
        - Long id that triggers the [:80] truncation
        - Long id whose 80th char is a separator (Task #6 finding #1: must
          re-rstrip after truncation so the slug never ends in `.` or `_`)
        """
        cases = [
            {
                "name": "mixed-hostile-chars",
                "timer_id": "../etc\x00 a:b\nfoo",
                "expect_slug_re": r"^[A-Za-z0-9_.-]+$",
                "expect_unknown": False,
                "expect_max_len": 80,
            },
            {
                "name": "slashes-only",
                "timer_id": "//\\\\//",
                "expect_slug_re": r"^(unknown|[A-Za-z0-9_.-]+)$",
                "expect_unknown": True,  # collapses to "unknown" after strip
                "expect_max_len": 80,
            },
            {
                "name": "whitespace-only",
                "timer_id": "   \t  \n  ",
                "expect_slug_re": r"^(unknown|[A-Za-z0-9_.-]+)$",
                "expect_unknown": True,
                "expect_max_len": 80,
            },
            {
                "name": "empty-string",
                "timer_id": "",
                "expect_slug_re": r"^unknown$",
                "expect_unknown": True,
                "expect_max_len": 80,
            },
            {
                "name": "long-id-truncates",
                "timer_id": "x" * 200,
                "expect_slug_re": r"^x{80}$",
                "expect_unknown": False,
                "expect_max_len": 80,
            },
            {
                "name": "long-id-with-separator-at-truncation-boundary",
                # 79 'x' + '_' + 50 'y' -> after substitute: same; after first
                # strip("._"): same (separator is not at edges); after [:80]:
                # 79 'x' + '_'. Without the post-slice rstrip, the slug ends
                # in '_'. With it, the slug should be 79 'x' (or "unknown" if
                # rstripping removed everything, which it shouldn't here).
                "timer_id": "x" * 79 + "_" + "y" * 50,
                "expect_slug_re": r"^x+$",
                "expect_unknown": False,
                "expect_max_len": 79,
                "must_not_endswith": ("_", "."),
            },
            {
                "name": "long-id-with-dot-at-truncation-boundary",
                "timer_id": "x" * 79 + "." + "y" * 50,
                "expect_slug_re": r"^x+$",
                "expect_unknown": False,
                "expect_max_len": 79,
                "must_not_endswith": ("_", "."),
            },
        ]

        for case in cases:
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as td:
                    WakeLiteService = _bootstrap(td)
                    svc = WakeLiteService(tick_seconds=1)
                    missing = str(Path(td) / "missing-cmux")
                    timer = _basic_timer(f"cmux-hostile-{case['name']}", "echo hi")
                    timer["id"] = case["timer_id"]
                    timer["callback"] = {
                        "type": "cmux",
                        "workspace_id": "ws-hostile",
                        "surface_id": "sf-hostile",
                        "session_id": None,
                        "cli_path": missing,
                    }
                    run_id = f"run-cmux-{case['name']}"

                    with self.assertLogs("wakelite.service", level="INFO"):
                        svc._execute_callback(
                            timer, "success", 0, run_id, _stdout_file(td), 1.0
                        )

                    signal_dir = Path(td) / ".claude" / "session-signals"
                    suffix = f".{run_id}.wakelite-callback.json"
                    recovery = [
                        p for p in signal_dir.glob(f"_no-session.*{suffix}")
                    ]
                    self.assertEqual(len(recovery), 1, f"expected 1 recovery file, got {len(recovery)}")

                    filename = recovery[0].name
                    self.assertTrue(filename.startswith("_no-session."))
                    self.assertTrue(filename.endswith(suffix))
                    prefix = "_no-session."
                    slug = filename[len(prefix):-len(suffix)]

                    self.assertRegex(slug, case["expect_slug_re"])
                    if case["expect_unknown"]:
                        self.assertEqual(slug, "unknown")
                    self.assertLessEqual(len(slug), case["expect_max_len"])
                    for unsafe in ("/", "\\", " ", "\x00", "\n", "\t", "..", ":"):
                        self.assertNotIn(unsafe, slug)
                    for end in case.get("must_not_endswith", ()):
                        self.assertFalse(
                            slug.endswith(end),
                            f"slug {slug!r} must not end with {end!r} after [:80] truncation",
                        )

                    payload = json.loads(recovery[0].read_text())
                    self.assertEqual(payload["timer_name"], f"cmux-hostile-{case['name']}")
                    self.assertEqual(payload["status"], "success")
                    self.assertEqual(payload["run_id"], run_id)

    def test_cmux_callback_colliding_slugs_dont_clobber_each_other(self):
        """CC-95 MEDIUM (claude-nyx, claude-artemis): two distinct hostile
        timer ids that the slug regex normalizes to the SAME slug must still
        produce TWO distinct recovery signal files when fired with different
        run_ids.

        The audit identified that ``a/b`` and ``a:b`` both substitute to
        ``a_b`` after the ``[^A-Za-z0-9_.-]+`` regex, so the slug component
        of the recovery filename is identical. The run_id segment is what
        prevents the second callback from clobbering the first. This test
        locks that guarantee in.
        """
        ids_with_same_slug = ("a/b", "a:b")
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            missing = str(Path(td) / "missing-cmux")
            signal_dir = Path(td) / ".claude" / "session-signals"

            for idx, raw_id in enumerate(ids_with_same_slug):
                timer = _basic_timer(f"cmux-collide-{idx}", "echo hi")
                timer["id"] = raw_id
                timer["callback"] = {
                    "type": "cmux",
                    "workspace_id": "ws-collide",
                    "surface_id": "sf-collide",
                    "session_id": None,
                    "cli_path": missing,
                }
                run_id = f"run-collide-{idx}"
                with self.assertLogs("wakelite.service", level="INFO"):
                    svc._execute_callback(
                        timer, "success", 0, run_id, _stdout_file(td), 1.0
                    )

            recovery_files = sorted(signal_dir.glob("_no-session.*.wakelite-callback.json"))
            self.assertEqual(
                len(recovery_files), 2,
                f"colliding slugs lost a recovery file. Files: {[p.name for p in recovery_files]}",
            )

            slug_substring = "_no-session.a_b."
            slug_prefixed = [p for p in recovery_files if slug_substring in p.name]
            self.assertEqual(
                len(slug_prefixed), 2,
                f"both files should share the normalized 'a_b' slug; got: {[p.name for p in recovery_files]}",
            )

            run_ids_seen = sorted(json.loads(p.read_text())["run_id"] for p in recovery_files)
            self.assertEqual(run_ids_seen, ["run-collide-0", "run-collide-1"])

            self.assertNotEqual(
                recovery_files[0].name, recovery_files[1].name,
                "two distinct recovery files must have distinct names — run_id is the disambiguator",
            )

    def test_cmux_callback_writes_recovery_signal_when_session_id_missing_and_workspace_id_missing(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer(
                "cmux-no-session-no-workspace",
                workspace_id=None,
                surface_id=None,
                session_id=None,
                cli_path=cmux,
            )

            with self.assertLogs("wakelite.service", level="ERROR") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-cmux-no-workspace", _stdout_file(td), 1.0
                )

            signal_dir = Path(td) / ".claude" / "session-signals"
            recovery = list(
                signal_dir.glob(
                    "_no-session.*.run-cmux-no-workspace.wakelite-callback.json"
                )
            )
            self.assertEqual(len(recovery), 1)

            payload = json.loads(recovery[0].read_text())
            self.assertEqual(payload["timer_name"], "cmux-no-session-no-workspace")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["run_id"], "run-cmux-no-workspace")

            log_text = "\n".join(logs.output)
            self.assertIn("missing workspace_id or surface_id", log_text)
            session_keyed = [
                path for path in signal_dir.glob("*.run-cmux-no-workspace.wakelite-callback.json")
                if not path.name.startswith("_no-session.")
            ]
            self.assertEqual(session_keyed, [])

    def test_cmux_callback_writes_recovery_signal_when_session_id_missing_and_target_stale(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer(
                "cmux-no-session-stale",
                workspace_id="ws-X",
                surface_id="sf-X",
                session_id=None,
                cli_path=cmux,
            )

            def side_effect(args, **kwargs):
                if args[:6] == [cmux, "send", "--workspace", "ws-X", "--surface", "sf-X"]:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="surface not found")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect), \
                 self.assertLogs("wakelite.service", level="ERROR") as logs:
                svc._execute_callback(
                    timer, "success", 0, "run-cmux-no-session-stale", _stdout_file(td), 1.0
                )

            signal_dir = Path(td) / ".claude" / "session-signals"
            recovery = list(
                signal_dir.glob(
                    "_no-session.*.run-cmux-no-session-stale.wakelite-callback.json"
                )
            )
            self.assertEqual(len(recovery), 1)

            payload = json.loads(recovery[0].read_text())
            self.assertEqual(payload["timer_name"], "cmux-no-session-stale")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["run_id"], "run-cmux-no-session-stale")

            log_text = "\n".join(logs.output)
            self.assertIn("no session_id is available for fallback", log_text)
            session_keyed = [
                path for path in signal_dir.glob("*.run-cmux-no-session-stale.wakelite-callback.json")
                if not path.name.startswith("_no-session.")
            ]
            self.assertEqual(session_keyed, [])

    def test_cmux_callback_recovery_write_failure_logs_error_and_continues(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer("cmux-recovery-write-fails", cli_path=cmux)

            with patch.object(svc, "_write_callback_signal", side_effect=OSError("disk full")), \
                 patch("wakelite.service.subprocess.run") as mock_run, \
                 self.assertLogs("wakelite.service", level="ERROR") as logs:
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
                svc._execute_callback(
                    timer, "success", 0, "run-cmux-write-fails", _stdout_file(td), 1.0
                )

            log_text = "\n".join(logs.output)
            self.assertIn("Recovery signal write failed for timer", log_text)
            self.assertIn("disk full", log_text)
            send_calls = [
                call.args[0]
                for call in mock_run.call_args_list
                if call.args and call.args[0][1] == "send"
            ]
            self.assertTrue(send_calls)

            # Task #6 polish: failure must surface as an incident row, not
            # only in the runner log. The incident type pins the failure
            # category so an oncall query can find it.
            incidents = svc.state.list_incidents(limit=10, include_acked=True)
            recovery_incidents = [
                i for i in incidents if i.get("type") == "callback_recovery_write_failed"
            ]
            self.assertEqual(len(recovery_incidents), 1, f"expected 1 incident, got: {incidents}")
            inc = recovery_incidents[0]
            self.assertEqual(inc.get("severity"), "warn")
            # _cmux_callback_timer dicts may or may not carry an id field;
            # the service falls back to "unknown" via timer.get("id", "unknown")
            # when absent. Mirror that here so the assertion stays robust.
            self.assertEqual(inc.get("timer_id"), timer.get("id", "unknown"))
            self.assertIn("disk full", inc.get("message", ""))

    def test_cmux_callback_recovery_write_failure_logs_error_and_continues_no_session(self):
        """Task #6 polish: cover the non-session-bound path of the
        recovery-write-failure handler. The session-bound path is exercised by
        the test above, but the no-session path produces a synthetic
        `_no-session.<slug>.<run_id>` filename — a different code path that
        could regress independently. Both paths must (a) keep the runner
        going, (b) log the ERROR, (c) record an incident."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _basic_timer("cmux-recovery-no-session", "echo hi")
            timer["callback"] = {
                "type": "cmux",
                "workspace_id": "ws-no-sess",
                "surface_id": "sf-no-sess",
                "cli_path": cmux,
                # No session_id — recovery-file path uses synthetic naming.
            }

            with patch.object(svc, "_write_callback_signal", side_effect=OSError("permission denied")), \
                 patch("wakelite.service.subprocess.run") as mock_run, \
                 self.assertLogs("wakelite.service", level="ERROR") as logs:
                mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
                svc._execute_callback(
                    timer, "success", 0, "run-cmux-no-sess-write-fails", _stdout_file(td), 1.0
                )

            log_text = "\n".join(logs.output)
            self.assertIn("Recovery signal write failed for timer", log_text)
            self.assertIn("permission denied", log_text)

            # Even with the recovery write failing, downstream cmux delivery
            # still proceeds — the runner does NOT abort.
            send_calls = [
                call.args[0]
                for call in mock_run.call_args_list
                if call.args and call.args[0][1] == "send"
            ]
            self.assertTrue(send_calls, "cmux send must still be attempted after recovery-write failure")

            # Incident recorded; timer_id mirrors the service's
            # `timer.get("id", "unknown")` fallback when no explicit id is set.
            incidents = svc.state.list_incidents(limit=10, include_acked=True)
            recovery_incidents = [
                i for i in incidents if i.get("type") == "callback_recovery_write_failed"
            ]
            self.assertEqual(len(recovery_incidents), 1)
            self.assertEqual(recovery_incidents[0].get("timer_id"), timer.get("id", "unknown"))
            self.assertIn("permission denied", recovery_incidents[0].get("message", ""))

    def test_cmux_new_workspace_fallback_polls_for_tty_ready(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)
            cmux = _make_executable(Path(td) / "cmux")
            timer = _cmux_callback_timer("cmux-new-workspace", cli_path=cmux)

            read_count = {"value": 0}

            def side_effect(args, **kwargs):
                command = args[1]
                if command == "send" and "--surface" in args:
                    return unittest.mock.Mock(returncode=1, stdout="", stderr="surface not found")
                if command == "new-workspace":
                    return unittest.mock.Mock(returncode=0, stdout="OK ws-new\n", stderr="")
                if command == "read-screen":
                    read_count["value"] += 1
                    stdout = "$ " if read_count["value"] == 3 else ""
                    return unittest.mock.Mock(returncode=0, stdout=stdout, stderr="")
                # send-key Enter for the new-workspace fallback (workspace-only, no --surface)
                return unittest.mock.Mock(returncode=0, stdout="", stderr="")

            with patch("wakelite.service.subprocess.run", side_effect=side_effect) as mock_run, \
                 patch("wakelite.service.time.sleep"):
                svc._execute_callback(timer, "success", 0, "run-cmux-5", _stdout_file(td), 1.0)

            calls = [call.args[0] for call in mock_run.call_args_list]
            new_workspace = [args for args in calls if args[1] == "new-workspace"][0]
            self.assertNotIn("--command", new_workspace)
            read_calls = [args for args in calls if args[1] == "read-screen"]
            self.assertEqual(len(read_calls), 3)
            # CC-95: resume must be sent in TWO argv calls — the command (no trailing escape)
            # followed by a separate send-key Enter. cmux send does not interpret \n / \r as
            # Enter, so a trailing escape leaves the command unsubmitted.
            resume_send = [args for args in calls if args[1] == "send" and "--surface" not in args][-1]
            self.assertEqual(resume_send, [cmux, "send", "--workspace", "ws-new", "--", "claude --resume sess-cmux"])
            send_key_calls = [args for args in calls if args[1] == "send-key" and "--surface" not in args]
            self.assertTrue(send_key_calls, "expected a workspace-scoped send-key Enter after resume send")
            self.assertEqual(send_key_calls[0], [cmux, "send-key", "--workspace", "ws-new", "Enter"])

    def test_unknown_callback_type_is_neutralized_with_warning(self):
        """CC-95: rolling-deploy compat. A timer with an unknown callback.type
        (e.g., a hypothetical future "kitty" written by a newer WakeLite) must
        be accepted by an older version's _validate_timer — the callback is
        neutralized to None and a WARNING is logged. Without this, rolling
        back to an older version after newer-version writes would brick
        update_timer / replace_all on every affected timer."""
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            payload = _basic_timer("future-callback-type")
            payload["callback"] = {
                "type": "kitty",
                "workspace_id": "ws-future",
                "surface_id": "sf-future",
            }

            with self.assertLogs("wakelite.timer_store", level="WARNING") as logs:
                timer = svc.timer_store.create_timer(payload)

            self.assertIsNone(timer["callback"], "callback should be neutralized to None")
            log_text = "\n".join(logs.output)
            self.assertIn("kitty", log_text)
            self.assertIn("future-callback-type", log_text)
            self.assertIn("neutralizing", log_text.lower())

            # update_timer must also tolerate the unknown type — this is the
            # rolling-deploy hot path: an older version trying to toggle
            # `enabled` on a timer whose persisted callback it doesn't know.
            persisted = dict(timer)
            persisted["callback"] = {"type": "kitty", "workspace_id": "ws-still"}
            with self.assertLogs("wakelite.timer_store", level="WARNING"):
                updated = svc.timer_store.update_timer(timer["id"], {"callback": persisted["callback"]})
            self.assertIsNone(updated["callback"])


class CmuxSurfaceNotFoundContractTests(unittest.TestCase):
    """CC-95: contract tests for `_cmux_surface_not_found`. The classifier
    decides whether a non-zero `cmux send` invocation indicates a stale
    workspace/surface (→ fall back to session-store / new-workspace) or a
    real send failure (→ log error + give up). The canonical phrases come
    from cmux's own `shouldIgnoreClaudeHookTeardownError` allowlist
    (cmux.swift:12755-12772). Pre-CC-95 the classifier substring-matched
    `"surface" AND ("not found"|"missing"|"unknown"|"invalid"|"gone")` —
    "missing/unknown/invalid/gone" never appear in cmux output and the
    "unknown" branch false-matched on unrelated errors like "Network
    unknown error". This suite locks the new behavior to cmux's real
    wording."""

    @staticmethod
    def _result(stderr: str, returncode: int = 1) -> subprocess.CompletedProcess:
        # cmux writes errors to stderr with returncode != 0; stdout is
        # generally empty for these.
        return subprocess.CompletedProcess(
            args=["cmux", "send"], returncode=returncode, stdout="", stderr=stderr
        )

    def test_returncode_zero_is_never_stale(self):
        from wakelite.service import WakeLiteService
        # Even if stderr would otherwise match (e.g., warning text), exit 0
        # means the call succeeded — never reclassify as stale.
        result = self._result("Surface not found", returncode=0)
        self.assertFalse(WakeLiteService._cmux_surface_not_found(result))

    def test_canonical_cmux_phrases_classify_as_stale(self):
        from wakelite.service import WakeLiteService
        # Mirror cmux.swift:12755-12772 — the relevant subset (stale handles,
        # not socket/transport faults). Each is the actual user-visible
        # wording from cmux's own ignore list, with the leading capital.
        canonical = [
            "Workspace not found",
            "Workspace ref not found",
            "Workspace index not found",
            "Workspace target not found: ws-stale",
            "Previous workspace not found",
            "Surface not found",
            "Surface ref not found",
            "Surface index not found",
            "Surface target not found",
            "Unable to resolve surface id",
            "Panel not found",
            "Tab not found",
            "No workspace selected",
            "TabManager not available",
        ]
        for stderr in canonical:
            with self.subTest(stderr=stderr):
                self.assertTrue(
                    WakeLiteService._cmux_surface_not_found(self._result(stderr)),
                    f"expected stale classification for cmux stderr: {stderr!r}",
                )

    def test_socket_and_transport_errors_are_not_classified_as_stale(self):
        from wakelite.service import WakeLiteService
        # These appear in cmux's broader ignore list but are infra faults,
        # not stale handles. WakeLite should NOT treat them as stale (they
        # warrant retry / hard-error, not new-workspace fallback).
        for stderr in (
            "failed to write to socket",
            "socket read error",
            "not connected",
        ):
            with self.subTest(stderr=stderr):
                self.assertFalse(
                    WakeLiteService._cmux_surface_not_found(self._result(stderr)),
                    f"socket-level error misclassified as stale: {stderr!r}",
                )

    def test_pre_cc95_false_positive_no_longer_classifies_as_stale(self):
        from wakelite.service import WakeLiteService
        # Pre-CC-95 substring check matched `"surface" AND "unknown"`, so a
        # message like this falsely classified as stale. The anchored phrase
        # check rejects it.
        result = self._result("Network unknown error on surface init")
        self.assertFalse(WakeLiteService._cmux_surface_not_found(result))

    def test_unrecognized_nonzero_exit_logs_warning_and_returns_false(self):
        from wakelite.service import WakeLiteService
        # When a non-zero exit produces stderr we don't recognize, log a
        # WARNING (so the operator can extend the allowlist if it's a new
        # stale-target phrasing) and return False (don't speculatively fall
        # back).
        result = self._result("Permission denied: keychain")
        with self.assertLogs("wakelite.service", level="WARNING") as logs:
            classified = WakeLiteService._cmux_surface_not_found(result)
        self.assertFalse(classified)
        log_text = "\n".join(logs.output)
        self.assertIn("permission denied: keychain", log_text.lower())
        self.assertIn("not classified as stale", log_text.lower())

    def test_empty_stderr_with_nonzero_exit_does_not_log_or_classify(self):
        from wakelite.service import WakeLiteService
        # cmux killed by signal, hung up, etc. — nothing to log, definitely
        # not a stale-target classification.
        result = self._result("")
        # `assertLogs` raises if nothing is logged at the requested level,
        # so we verify quietness via a direct check on the captured handler.
        with self.assertNoLogs("wakelite.service", level="WARNING"):
            self.assertFalse(WakeLiteService._cmux_surface_not_found(result))


class CmuxSessionStoreOrderingTests(unittest.TestCase):
    """CC-95: ordering and freshness guarantees for the cmux session-store
    fallback. Live cmux stores a session-id-keyed object with numeric epoch
    `updatedAt` values. List/ISO input remains supported for compatibility.
    Pre-CC-95 the resolver lex-sorted `updatedAt` strings, which
    (a) misorders mixed timezone formats — `+00:00` < `Z` lexically though
    they're the same instant — and (b) imposed no staleness cutoff, so a
    7-day-old entry could win over a stale-but-recent surface that just
    churned. Now: normalize numeric epochs or ISO strings to aware datetimes,
    drop entries older than 24h, and INFO-log the chosen entry's age."""

    @staticmethod
    def _setup_store(td: str, sessions: list) -> Tuple[Path, Path]:
        store = Path(td) / "claude-hook-sessions.json"
        lock = Path(td) / "claude-hook-sessions.lock"
        store.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
        lock.write_text("", encoding="utf-8")
        return store, lock

    def test_live_dict_shape_with_numeric_epoch_resolves(self):
        """The production store is keyed by sessionId and uses epoch seconds.

        This fails if the resolver regresses to list-only iteration or the
        timestamp parser regresses to accepting strings only.
        """
        with tempfile.TemporaryDirectory() as td:
            from datetime import datetime as _dt, timezone as _tz
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            cmux_dir = Path(td) / ".cmuxterm"
            cmux_dir.mkdir(parents=True, exist_ok=True)
            store = cmux_dir / "claude-hook-sessions.json"
            lock = cmux_dir / "claude-hook-sessions.json.lock"
            store.write_text(json.dumps({
                "sessions": {
                    "sess-live-shape": {
                        "sessionId": "sess-live-shape",
                        "workspaceId": "ws-live-shape",
                        "surfaceId": "sf-live-shape",
                        "updatedAt": _dt.now(_tz.utc).timestamp(),
                    }
                }
            }), encoding="utf-8")
            lock.write_text("", encoding="utf-8")

            env = {
                "WAKELITE_CMUX_SESSION_STORE_PATH": "",
                "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH": "",
            }
            with patch.dict(os.environ, env, clear=False):
                target = svc._resolve_cmux_target_via_session_store("sess-live-shape")

            self.assertEqual(target, ("ws-live-shape", "sf-live-shape"))

    def test_datetime_ordering_picks_latest_instant_across_tz_formats(self):
        """Two entries for the same session — one written `...+00:00`, the
        other `...Z` ten seconds later. Lex-sort would put `+00:00` last
        (because `+` < `Z` lexically). Datetime parsing collapses both to
        UTC and the actually-newer `Z` entry wins."""
        with tempfile.TemporaryDirectory() as td:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            now = _dt.now(_tz.utc)
            older_plus00 = (now - _td(seconds=10)).isoformat(timespec="seconds")  # `...+00:00`
            newer_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            store, lock = self._setup_store(td, [
                {
                    "sessionId": "sess-tz",
                    "workspaceId": "ws-older-plus00",
                    "surfaceId": "sf-older-plus00",
                    "updatedAt": older_plus00,
                },
                {
                    "sessionId": "sess-tz",
                    "workspaceId": "ws-newer-z",
                    "surfaceId": "sf-newer-z",
                    "updatedAt": newer_z,
                },
            ])

            env = {
                "WAKELITE_CMUX_SESSION_STORE_PATH": str(store),
                "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH": str(lock),
            }
            with patch.dict(os.environ, env, clear=False):
                target = svc._resolve_cmux_target_via_session_store("sess-tz")

            self.assertEqual(target, ("ws-newer-z", "sf-newer-z"))

    def test_skips_entries_older_than_24h_cutoff(self):
        """The single matching entry is 25h old. The resolver returns None
        rather than routing to a near-certainly-closed session, and logs
        why."""
        with tempfile.TemporaryDirectory() as td:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            stale_iso = (_dt.now(_tz.utc) - _td(hours=25)).isoformat()
            store, lock = self._setup_store(td, [{
                "sessionId": "sess-stale",
                "workspaceId": "ws-stale",
                "surfaceId": "sf-stale",
                "updatedAt": stale_iso,
            }])

            env = {
                "WAKELITE_CMUX_SESSION_STORE_PATH": str(store),
                "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH": str(lock),
            }
            with patch.dict(os.environ, env, clear=False), \
                 self.assertLogs("wakelite.service", level="INFO") as logs:
                target = svc._resolve_cmux_target_via_session_store("sess-stale")

            self.assertIsNone(target)
            self.assertIn("no fresh entry", "\n".join(logs.output).lower())
            self.assertIn("24h", "\n".join(logs.output))

    def test_route_logs_age_in_seconds(self):
        """When a fresh entry is routed, log includes `age=Ns` so operators
        can see how stale the chosen target was without having to inspect
        the session store."""
        with tempfile.TemporaryDirectory() as td:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            ten_min_ago = (_dt.now(_tz.utc) - _td(minutes=10)).isoformat()
            store, lock = self._setup_store(td, [{
                "sessionId": "sess-age",
                "workspaceId": "ws-aged",
                "surfaceId": "sf-aged",
                "updatedAt": ten_min_ago,
            }])

            env = {
                "WAKELITE_CMUX_SESSION_STORE_PATH": str(store),
                "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH": str(lock),
            }
            with patch.dict(os.environ, env, clear=False), \
                 self.assertLogs("wakelite.service", level="INFO") as logs:
                target = svc._resolve_cmux_target_via_session_store("sess-age")

            self.assertEqual(target, ("ws-aged", "sf-aged"))
            log_text = "\n".join(logs.output)
            # 10 minutes = 600s, allow a small slack for test wall-clock drift.
            self.assertRegex(log_text, r"age=(59[0-9]|60[0-9])s")

    def test_naive_timestamp_is_treated_as_utc_not_skipped(self):
        """An older session-store entry that lacks tzinfo (`...T10:00:00`
        with no `Z` / offset) is treated as UTC rather than dropped — cmux
        always emits UTC and silently failing on naive timestamps would
        produce mysterious "no fresh entry" misses if a future cmux build
        ever drops the suffix."""
        with tempfile.TemporaryDirectory() as td:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=1)

            naive_iso = (_dt.now(_tz.utc) - _td(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
            store, lock = self._setup_store(td, [{
                "sessionId": "sess-naive",
                "workspaceId": "ws-naive",
                "surfaceId": "sf-naive",
                "updatedAt": naive_iso,
            }])

            env = {
                "WAKELITE_CMUX_SESSION_STORE_PATH": str(store),
                "WAKELITE_CMUX_SESSION_STORE_LOCK_PATH": str(lock),
            }
            with patch.dict(os.environ, env, clear=False):
                target = svc._resolve_cmux_target_via_session_store("sess-naive")

            self.assertEqual(target, ("ws-naive", "sf-naive"))


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
        with patch.dict(os.environ, {"GHOSTTY_TERMINAL_ID": "uuid-456", "WEZTERM_PANE": "99"}, clear=True):
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

    def test_auto_capture_cmux_env(self):
        from wakelite.config import auto_capture_terminal
        with tempfile.TemporaryDirectory() as td:
            cmux = _make_executable(Path(td) / "cmux")
            cb = {}
            env = {
                "CMUX_WORKSPACE_ID": "ws-env",
                "CMUX_SURFACE_ID": "sf-env",
                "CMUX_PANEL_ID": "panel-env",
                "CMUX_SOCKET_PATH": str(Path(td) / "cmux.sock"),
                "CMUX_BUNDLED_CLI_PATH": cmux,
                "GHOSTTY_TERMINAL_ID": "ghostty-env",
                "WEZTERM_PANE": "99",
            }
            with patch.dict(os.environ, env, clear=False):
                auto_capture_terminal(cb)
            self.assertEqual(cb["type"], "cmux")
            self.assertEqual(cb["workspace_id"], "ws-env")
            self.assertEqual(cb["surface_id"], "sf-env")
            self.assertEqual(cb["socket_path"], str(Path(td) / "cmux.sock"))
            self.assertEqual(cb["cli_path"], cmux)

    def test_auto_capture_cmux_requires_both_workspace_and_surface(self):
        """CC-95 HIGH#5: auto-detect must NOT emit type=cmux when only one of
        CMUX_WORKSPACE_ID / CMUX_SURFACE_ID(_PANEL_ID) is set.

        Partial cmux env (e.g. stale CMUX_SURFACE_ID leaking from a parent
        process into a non-cmux subshell, or a buggy cmux config that drops
        one of the two) must fall through to ghostty/wezterm detection
        rather than produce a half-populated cmux callback that fails
        timer_store validation at create time.
        """
        from wakelite.config import auto_capture_terminal

        # Case 1: only CMUX_SURFACE_ID — must NOT auto-detect cmux
        cb1 = {}
        with patch.dict(os.environ, {"CMUX_SURFACE_ID": "sf-only"}, clear=True):
            auto_capture_terminal(cb1)
        self.assertNotEqual(
            cb1.get("type"), "cmux",
            "partial cmux env (surface only) must NOT trigger cmux mode"
        )
        self.assertNotIn("surface_id", cb1)
        self.assertNotIn("workspace_id", cb1)

        # Case 2: only CMUX_WORKSPACE_ID — must NOT auto-detect cmux
        cb2 = {}
        with patch.dict(os.environ, {"CMUX_WORKSPACE_ID": "ws-only"}, clear=True):
            auto_capture_terminal(cb2)
        self.assertNotEqual(
            cb2.get("type"), "cmux",
            "partial cmux env (workspace only) must NOT trigger cmux mode"
        )
        self.assertNotIn("workspace_id", cb2)
        self.assertNotIn("surface_id", cb2)

        # Case 3: only CMUX_PANEL_ID (alias for surface) — must NOT auto-detect cmux
        cb3 = {}
        with patch.dict(os.environ, {"CMUX_PANEL_ID": "panel-only"}, clear=True):
            auto_capture_terminal(cb3)
        self.assertNotEqual(
            cb3.get("type"), "cmux",
            "partial cmux env (panel-id alias only, no workspace) must NOT trigger cmux mode"
        )

        # Case 4: surface-only with GHOSTTY_TERMINAL_ID present — must fall
        # through cleanly to ghostty (not get stuck on partial cmux).
        cb4 = {}
        with patch.dict(
            os.environ,
            {"CMUX_SURFACE_ID": "sf-stray", "GHOSTTY_TERMINAL_ID": "ghostty-fallthrough"},
            clear=True,
        ):
            auto_capture_terminal(cb4)
        self.assertEqual(
            cb4.get("type"), "ghostty",
            "stale CMUX_SURFACE_ID without workspace must fall through to ghostty"
        )
        self.assertEqual(cb4.get("terminal_id"), "ghostty-fallthrough")

        # Case 5: workspace-only with WEZTERM_PANE present — must fall
        # through to wezterm.
        cb5 = {}
        with patch.dict(
            os.environ,
            {"CMUX_WORKSPACE_ID": "ws-stray", "WEZTERM_PANE": "42"},
            clear=True,
        ):
            auto_capture_terminal(cb5)
        self.assertEqual(
            cb5.get("type"), "wezterm",
            "stale CMUX_WORKSPACE_ID without surface must fall through to wezterm"
        )
        self.assertEqual(cb5.get("pane_id"), 42)

        # Case 6: BOTH present — happy path still works (regression guard).
        cb6 = {}
        with patch.dict(
            os.environ,
            {"CMUX_WORKSPACE_ID": "ws-both", "CMUX_SURFACE_ID": "sf-both"},
            clear=True,
        ):
            auto_capture_terminal(cb6)
        self.assertEqual(cb6.get("type"), "cmux")
        self.assertEqual(cb6.get("workspace_id"), "ws-both")
        self.assertEqual(cb6.get("surface_id"), "sf-both")


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


class DaemonSpawnStallTests(unittest.TestCase):
    def test_daemon_run_stalled_before_popen_is_not_declared_ghost(self):
        """A daemon run whose worker is still blocked before Popen must survive the ghost check.

        Regression for 2026-09-07: after a machine wake, the pre-spawn Slack call
        stalled ~35s on DNS, the ghost check fired at the 30s grace boundary,
        cleared the run, and the process that Popen'd 5s later became an untracked
        orphan holding the daemon's port. Every restart then failed with EADDRINUSE.
        """
        import threading

        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer({
                "name": "daemon-stalled-spawn",
                "comment": "Worker blocked in Slack before Popen",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "command": {"mode": "shell", "shell": "sleep 30"},
            })
            timer_id = timer["id"]
            scheduled_at = datetime.now(timezone.utc).isoformat()
            svc.state.reserve_occurrence(timer_id, scheduled_at, False)

            entered_slack = threading.Event()
            release_slack = threading.Event()

            def _stalling_daily_thread_ts():
                entered_slack.set()
                release_slack.wait(timeout=10)
                return "fake-thread-ts"

            svc.notifier.get_daily_thread_ts = _stalling_daily_thread_ts

            worker = threading.Thread(
                target=svc._run_occurrence,
                args=(timer, scheduled_at, False, "daemon_start", None),
                daemon=True,
            )
            worker.start()
            try:
                self.assertTrue(entered_slack.wait(timeout=5), "worker never reached the Slack stall")
                runtime = svc.state.get_runtime(timer_id)
                run_id = runtime.running_run_id
                self.assertTrue(runtime.is_running)
                self.assertIsNotNone(run_id)

                # Equivalent of "stalled longer than DAEMON_SPAWN_GRACE_SECONDS".
                with patch.object(svc, "DAEMON_SPAWN_GRACE_SECONDS", 0), \
                        patch.object(svc, "_spawn_run") as mock_spawn:
                    svc._process_daemons(datetime.now())

                run = svc.state.get_run(run_id)
                self.assertEqual(run["status"], "started", f"run was closed early: {run['message']!r}")
                self.assertTrue(svc.state.get_runtime(timer_id).is_running)
                self.assertEqual(svc.state.get_runtime(timer_id).running_run_id, run_id)
                mock_spawn.assert_not_called()
            finally:
                release_slack.set()
                deadline = time.time() + 5
                while time.time() < deadline and run_id not in svc._active_processes:
                    time.sleep(0.05)
                proc = svc._active_processes.get(run_id)
                if proc is not None:
                    proc.terminate()
                worker.join(timeout=10)


class AlertCollapseTests(unittest.TestCase):
    """One root cause must file one incident and a countable handful of alerts.

    2026-09-07: a single held TCP port produced 38 run_failed incidents and a
    Slack post per restart. Across 30 days the machine held 272 run_failed rows,
    142 still unacknowledged, which is exactly how a real failure goes unseen.
    """

    _STREAK_RE = re.compile(r"streak=(\d+)")

    @staticmethod
    def _run_once(svc, timer, index):
        svc._run_occurrence(
            timer,
            f"2026-09-07T10:{index:02d}:00+00:00",
            is_catchup=False,
            queued_reason=None,
            retry_of_run_id=None,
        )

    @staticmethod
    def _slack_messages(svc):
        return [call.args[0] for call in svc.notifier.notify_slack.call_args_list]

    def _sent_lines(self, captured, kind):
        return [line for line in captured.output if "notify.sent" in line and f"kind={kind}" in line]

    def test_ten_identical_failures_collapse_to_one_incident_and_three_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer(_basic_timer("collapse-storm", "exit 1"))

            with self.assertLogs("wakelite.notifier", level="INFO") as captured:
                for index in range(10):
                    self._run_once(svc, timer, index)

            incidents = svc.state.list_incidents(limit=50, incident_type="run_failed")
            self.assertEqual(
                len(incidents), 1, f"one root cause filed {len(incidents)} incident rows"
            )
            self.assertEqual(incidents[0]["count"], 10)
            self.assertIsNotNone(incidents[0]["last_seen_at"])
            self.assertGreater(incidents[0]["last_seen_at"], incidents[0]["created_at"])

            sent = self._sent_lines(captured, "run_failed")
            self.assertEqual(
                [self._STREAK_RE.search(line).group(1) for line in sent],
                ["1", "3", "10"],
                f"expected alerts at streak 1/3/10, got {sent}",
            )
            self.assertEqual(svc.notifier.notify.call_count, 3)

            started = [m for m in self._slack_messages(svc) if "started" in m.lower()]
            self.assertEqual(
                len(started), 2, f"a suppressed streak still posted {len(started)} start messages"
            )
            status_posts = [m for m in self._slack_messages(svc) if "*Failed*" in m]
            self.assertEqual(len(status_posts), 1, f"status replies were not collapsed: {status_posts}")

            runtime = svc.state.get_runtime(timer["id"])
            self.assertEqual(runtime.failure_streak, 10)
            self.assertEqual(runtime.streak_message, "exit code 1")
            self.assertIsNotNone(runtime.streak_started_at)

    def test_success_after_a_streak_sends_exactly_one_recovery_message(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            flag = Path(td) / "exit-code"
            flag.write_text("1")
            timer = svc.timer_store.create_timer(
                _basic_timer("collapse-recovery", f"exit $(cat {flag})")
            )

            for index in range(4):
                self._run_once(svc, timer, index)
            self.assertEqual(svc.state.get_runtime(timer["id"]).failure_streak, 4)

            before = svc.notifier.notify.call_count
            flag.write_text("0")
            with self.assertLogs("wakelite.notifier", level="INFO") as captured:
                self._run_once(svc, timer, 4)

            self.assertEqual(len(self._sent_lines(captured, "recovered")), 1)
            self.assertEqual(svc.notifier.notify.call_count - before, 1)
            recovery = svc.notifier.notify.call_args_list[-1].args[1].lower()
            self.assertIn("recovered after 4", recovery)

            runtime = svc.state.get_runtime(timer["id"])
            self.assertEqual(runtime.failure_streak, 0)
            self.assertIsNone(runtime.streak_message)
            self.assertEqual(runtime.last_notified_streak, 0)

            # A second success must not repeat the recovery message.
            with self.assertLogs("wakelite.notifier", level="INFO") as captured_again:
                logging.getLogger("wakelite.notifier").info("probe")
                self._run_once(svc, timer, 5)
            self.assertEqual(self._sent_lines(captured_again, "recovered"), [])

    def test_a_different_failure_message_starts_a_new_streak(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            flag = Path(td) / "exit-code"
            flag.write_text("1")
            timer = svc.timer_store.create_timer(
                _basic_timer("collapse-new-cause", f"exit $(cat {flag})")
            )

            with self.assertLogs("wakelite.notifier", level="INFO") as captured:
                for index in range(2):
                    self._run_once(svc, timer, index)
                flag.write_text("2")
                self._run_once(svc, timer, 2)

            incidents = svc.state.list_incidents(limit=50, incident_type="run_failed")
            self.assertEqual(len(incidents), 2, f"a new cause did not open its own incident: {incidents}")
            self.assertEqual(
                sorted((row["message"], row["count"]) for row in incidents),
                [
                    (f"Timer {timer['id']} failed: exit code 1", 2),
                    (f"Timer {timer['id']} failed: exit code 2", 1),
                ],
            )

            runtime = svc.state.get_runtime(timer["id"])
            self.assertEqual(runtime.failure_streak, 1)
            self.assertEqual(runtime.streak_message, "exit code 2")

            sent = self._sent_lines(captured, "run_failed")
            self.assertEqual(
                [self._STREAK_RE.search(line).group(1) for line in sent],
                ["1", "1"],
                f"a new cause must alert immediately, got {sent}",
            )

    def test_the_daily_slack_thread_survives_a_runner_restart(self):
        with tempfile.TemporaryDirectory() as td:
            _bootstrap(td)
            import wakelite.notifier as notifier_module
            import wakelite.state as state_module

            store = state_module.StateStore()
            first = notifier_module.Notifier(meta_store=store)
            with patch.object(first, "notify_slack", return_value="ts-1") as post:
                self.assertEqual(first.get_daily_thread_ts(), "ts-1")
                post.assert_called_once()

            # A fresh Notifier stands in for the next runner process.
            second = notifier_module.Notifier(meta_store=store)
            with patch.object(second, "notify_slack", return_value="ts-2") as post_again:
                self.assertEqual(second.get_daily_thread_ts(), "ts-1")
                post_again.assert_not_called()

            day = datetime.now().strftime("%Y-%m-%d")
            self.assertEqual(store.get_meta(f"slack.daily_thread_ts.{day}"), "ts-1")


class StalledWaitTests(unittest.TestCase):
    """A wait is fine; a wait nobody ends is a stuck poller nobody hears about.

    mcp-feature-catalog-protocol-health logged 1,238 silent waits over 30 days
    and reported nothing.
    """

    def _waiting_run(self, svc, timer, scheduled_at, created_at):
        run = svc.state.create_run(
            timer_id=timer["id"],
            timer_name=timer["name"],
            scheduled_at=scheduled_at,
            is_catchup=False,
            queued_reason=None,
        )
        svc.state.finish_run(
            run_id=run["run_id"],
            timer_id=timer["id"],
            scheduled_at=scheduled_at,
            status="waiting",
            exit_code=75,
            message="not ready yet (EX_TEMPFAIL)",
            stdout_path=None,
            stderr_path=None,
        )
        with svc.state._connect() as conn:
            conn.execute(
                "UPDATE run_history SET created_at = ?, started_at = ?, finished_at = ? WHERE run_id = ?",
                (created_at, created_at, created_at, run["run_id"]),
            )
        return run["run_id"]

    @staticmethod
    def _hours_ago(hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def test_a_day_of_unbroken_waiting_raises_one_info_incident(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer(_basic_timer("stuck-poller", "exit 75"))
            for hours in (30, 20, 1):
                self._waiting_run(svc, timer, f"2026-09-06T{hours:02d}:00:00+00:00", self._hours_ago(hours))

            svc._report_stalled_waits()
            svc._report_stalled_waits()

            incidents = svc.state.list_incidents(limit=50, incident_type="waiting_stalled")
            self.assertEqual(len(incidents), 1, f"a stalled wait filed {len(incidents)} rows")
            self.assertEqual(incidents[0]["severity"], "info")
            self.assertEqual(incidents[0]["timer_id"], timer["id"])
            self.assertEqual(incidents[0]["count"], 2)
            self.assertIn("waiting", incidents[0]["message"].lower())

    def test_a_short_wait_or_a_settled_timer_reports_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            fresh = svc.timer_store.create_timer(_basic_timer("fresh-wait", "exit 75"))
            self._waiting_run(svc, fresh, "2026-09-07T09:00:00+00:00", self._hours_ago(2))

            settled = svc.timer_store.create_timer(_basic_timer("settled-wait", "exit 75"))
            self._waiting_run(svc, settled, "2026-09-06T04:00:00+00:00", self._hours_ago(30))
            run = svc.state.create_run(
                timer_id=settled["id"],
                timer_name=settled["name"],
                scheduled_at="2026-09-07T09:00:00+00:00",
                is_catchup=False,
                queued_reason=None,
            )
            svc.state.finish_run(
                run_id=run["run_id"],
                timer_id=settled["id"],
                scheduled_at="2026-09-07T09:00:00+00:00",
                status="success",
                exit_code=0,
                message="completed",
                stdout_path=None,
                stderr_path=None,
            )

            svc._report_stalled_waits()

            self.assertEqual(svc.state.list_incidents(limit=50, incident_type="waiting_stalled"), [])


class DaemonFlapBreakerTests(unittest.TestCase):
    """A daemon that dies just past the old 60s floor must stay backed off.

    With the floor at 60, cmux-focus-server died every ~65s, cleared its backoff
    on every attempt and restarted 37 times in a row at full speed.
    """

    def _daemon(self, svc, name):
        return svc.timer_store.create_timer({
            "name": name,
            "comment": "Daemon under the flap breaker",
            "enabled": True,
            "timer_type": "daemon",
            "recurrence": {"frequency": "interval", "every": "0s"},
            "execution": {
                "restart_on_failure": True,
                "restart_delay_seconds": 5,
                "restart_max_backoff_seconds": 300,
            },
            "command": {"mode": "shell", "shell": "exit 1"},
        })

    @staticmethod
    def _run_with_uptime(svc, timer, uptime):
        """Drive one real daemon run whose measured uptime is `uptime` seconds."""
        import wakelite.service as service_module

        calls = []

        def fake_monotonic():
            calls.append(None)
            return 1000.0 if len(calls) == 1 else 1000.0 + uptime

        with patch.object(service_module.time, "monotonic", fake_monotonic):
            svc._run_occurrence(
                timer,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                is_catchup=False,
                queued_reason="daemon_start",
                retry_of_run_id=None,
            )

    def test_uptime_below_the_healthy_floor_keeps_the_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = self._daemon(svc, "daemon-fast-flap")
            svc.state.set_daemon_state(timer["id"], status="stopped", current_backoff_seconds=120)

            self._run_with_uptime(svc, timer, uptime=65.0)

            self.assertEqual(
                svc.state.get_daemon_state(timer["id"]).current_backoff_seconds,
                120,
                "a daemon that died after 65s was treated as healthy and reset its backoff",
            )

    def test_uptime_past_the_healthy_floor_clears_the_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = _bootstrap(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = self._daemon(svc, "daemon-long-lived")
            svc.state.set_daemon_state(timer["id"], status="stopped", current_backoff_seconds=120)

            self._run_with_uptime(svc, timer, uptime=400.0)

            self.assertEqual(
                svc.state.get_daemon_state(timer["id"]).current_backoff_seconds, 0
            )


if __name__ == "__main__":
    unittest.main()
