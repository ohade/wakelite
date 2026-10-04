"""Run grid: one row per timer, one cell per local day (the Airflow-style status view)."""
from __future__ import annotations

import importlib
import json
import os
import socket
import tempfile
import unittest
import urllib.request
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path


def _bootstrap(temp_home: str):
    os.environ["WAKELITE_HOME"] = temp_home
    import wakelite.config as config
    import wakelite.http_api as http_api
    import wakelite.service as service
    import wakelite.state as state
    import wakelite.timer_store as timer_store

    for module in (config, state, timer_store, service, http_api):
        importlib.reload(module)
    return service.WakeLiteService, http_api.ApiServer


def _timer(name: str, timer_type: str = "scheduled") -> dict:
    timer = {
        "name": name,
        "comment": f"Test timer: {name}",
        "enabled": True,
        "command": {"mode": "shell", "shell": "true"},
        "timer_type": timer_type,
    }
    if timer_type == "daemon":
        timer["recurrence"] = {"frequency": "interval", "every": "0s"}
    else:
        timer["recurrence"] = {"frequency": "daily", "time": "03:00"}
    return timer


def _local_noon(days_ago: int) -> str:
    day = datetime.now().astimezone().date() - timedelta(days=days_ago)
    return datetime.combine(day, time(12, 0)).astimezone().astimezone(timezone.utc).isoformat()


class RunGridTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        WakeLiteService, self.ApiServer = _bootstrap(self.temp.name)
        self.svc = WakeLiteService(tick_seconds=15, max_workers=1)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _add_runs(self, timer_id: str, days_ago: int, status: str, count: int = 1) -> None:
        created = _local_noon(days_ago)
        with self.svc.state._lock:
            conn = self.svc.state._connect()
            for _ in range(count):
                run_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO run_history(run_id, timer_id, timer_name, scheduled_at, attempt,"
                    " occurrence_key, started_at, status, is_catchup, created_at)"
                    " VALUES (?, ?, 'fixture', ?, 1, ?, ?, ?, 0, ?)",
                    (run_id, timer_id, created, f"{timer_id}:{run_id}", created, status, created),
                )

    def _row(self, grid: dict, name: str) -> dict:
        return next(row for row in grid["timers"] if row["name"] == name)

    def test_day_cells_classify_success_partial_failed_waiting_and_empty(self) -> None:
        timer = self.svc.timer_store.create_timer(_timer("mixed"))
        self._add_runs(timer["id"], 0, "success", 3)
        self._add_runs(timer["id"], 1, "success", 3)
        self._add_runs(timer["id"], 1, "failed", 1)
        self._add_runs(timer["id"], 2, "failed", 2)
        self._add_runs(timer["id"], 3, "waiting", 4)

        grid = self.svc.run_grid(days=5)

        self.assertEqual(len(grid["days"]), 5)
        states = [cell["state"] for cell in self._row(grid, "mixed")["days"]]
        # Oldest first: 4 days ago had no run.
        self.assertEqual(states, ["none", "waiting", "failed", "partial", "success"])
        yesterday = self._row(grid, "mixed")["days"][3]
        self.assertEqual(yesterday["counts"], {"success": 3, "failed": 1})

    def test_runs_older_than_the_window_are_not_counted(self) -> None:
        timer = self.svc.timer_store.create_timer(_timer("old-failure"))
        self._add_runs(timer["id"], 9, "failed", 5)

        grid = self.svc.run_grid(days=3)

        self.assertEqual([c["state"] for c in self._row(grid, "old-failure")["days"]], ["none"] * 3)

    def test_failing_timers_sort_first_and_daemons_last(self) -> None:
        healthy = self.svc.timer_store.create_timer(_timer("a-healthy"))
        failing = self.svc.timer_store.create_timer(_timer("z-failing"))
        daemon = self.svc.timer_store.create_timer(_timer("b-daemon", "daemon"))
        self._add_runs(healthy["id"], 0, "success")
        self._add_runs(failing["id"], 0, "failed")
        self.svc.state.bump_failure_streak(failing["id"], "exit code 1")
        self.svc.state.bump_failure_streak(daemon["id"], "stale daemon counter")

        names = [row["name"] for row in self.svc.run_grid(days=2)["timers"]]

        self.assertEqual(names, ["z-failing", "a-healthy", "b-daemon"])
        self.assertEqual(self._row(self.svc.run_grid(days=2), "z-failing")["failure_streak"], 1)

    def test_http_endpoint_returns_the_grid(self) -> None:
        timer = self.svc.timer_store.create_timer(_timer("over-http"))
        self._add_runs(timer["id"], 0, "failed")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = self.ApiServer(self.svc, host="127.0.0.1", port=port,
                                socket_path=Path(tempfile.mkdtemp()) / "wl.sock")
        server.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/runs/grid?days=2", timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        finally:
            server.stop()

        self.assertEqual(len(body["days"]), 2)
        self.assertEqual(self._row(body, "over-http")["days"][-1]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
