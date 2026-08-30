"""Incident triage: ignore rules, bulk resolve, and the summary report.

These cover the behaviour that lets the unacknowledged counter go down and
stay down. Before this, ack was one-incident-at-a-time and the pruner only
deleted already-acked rows, so the count could only ever grow.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wakelite.service import WakeLiteService
from wakelite.state import StateStore


class IncidentStateTests(unittest.TestCase):
    def _store(self, tmp: str) -> StateStore:
        return StateStore(Path(tmp) / "state.db")

    def test_bulk_ack_respects_type_filter_and_leaves_others_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for _ in range(5):
                store.add_incident("error", "run_failed", "boom", "timer-a")
            store.add_incident("warn", "crash_recovery", "recovered", "timer-a")

            acked = store.ack_incidents(incident_type="run_failed")

            self.assertEqual(acked, 5)
            self.assertEqual(store.count_unacked_incidents(), 1)
            self.assertEqual(
                store.count_incidents(include_acked=False, incident_type="crash_recovery"), 1
            )

    def test_bulk_ack_max_id_does_not_sweep_up_newer_incidents(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            old_ids = [store.add_incident("error", "run_failed", "old", "t") for _ in range(3)]
            boundary = old_ids[-1]
            fresh = store.add_incident("error", "run_failed", "arrived mid-request", "t")

            acked = store.ack_incidents(incident_type="run_failed", max_id=boundary)

            self.assertEqual(acked, 3)
            open_rows = store.list_incidents(include_acked=False)
            self.assertEqual([r["id"] for r in open_rows], [fresh])

    def test_bulk_ack_is_scoped_to_one_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_incident("error", "run_failed", "a", "timer-a")
            store.add_incident("error", "run_failed", "b", "timer-b")

            self.assertEqual(store.ack_incidents(timer_id="timer-a"), 1)
            remaining = store.list_incidents(include_acked=False)
            self.assertEqual([r["timer_id"] for r in remaining], ["timer-b"])

    def test_ack_records_source_and_ignores_already_resolved_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            incident_id = store.add_incident("error", "run_failed", "boom", "t")

            self.assertTrue(store.ack_incident(incident_id, source="ui"))
            self.assertFalse(store.ack_incident(incident_id, source="ui"))

            row = store.list_incidents()[0]
            self.assertEqual(row["ack_source"], "ui")
            self.assertIsNotNone(row["acked_at"])

    def test_unack_reopens_a_resolved_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            incident_id = store.add_incident("error", "run_failed", "boom", "t")
            store.ack_incident(incident_id)

            self.assertTrue(store.unack_incident(incident_id))
            self.assertEqual(store.count_unacked_incidents(), 1)
            self.assertFalse(store.unack_incident(incident_id))

    def test_ignore_rule_records_incident_but_keeps_counter_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_incident_mute(timer_id="noisy", incident_type="run_failed", reason="known")

            store.add_incident("error", "run_failed", "muted", "noisy")

            self.assertEqual(store.count_unacked_incidents(), 0)
            row = store.list_incidents()[0]
            self.assertEqual(row["acknowledged"], 1)
            self.assertTrue(row["ack_source"].startswith("mute:"))
            # Still present as evidence in the report.
            self.assertEqual(store.count_incidents(), 1)

    def test_ignore_rule_does_not_leak_to_other_timers_or_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_incident_mute(timer_id="noisy", incident_type="run_failed")

            store.add_incident("error", "run_failed", "other timer", "quiet")
            store.add_incident("warn", "crash_recovery", "other type", "noisy")

            self.assertEqual(store.count_unacked_incidents(), 2)

    def test_wildcard_ignore_rule_covers_every_timer_for_one_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_incident_mute(incident_type="overlap_queued")

            store.add_incident("info", "overlap_queued", "a", "timer-a")
            store.add_incident("info", "overlap_queued", "b", "timer-b")
            store.add_incident("error", "run_failed", "real", "timer-a")

            self.assertEqual(store.count_unacked_incidents(), 1)

    def test_removing_an_ignore_rule_restores_normal_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            mute = store.add_incident_mute(incident_type="run_failed")
            store.add_incident("error", "run_failed", "muted", "t")

            self.assertTrue(store.delete_incident_mute(mute["id"]))
            store.add_incident("error", "run_failed", "now visible", "t")

            self.assertEqual(store.count_unacked_incidents(), 1)

    def test_an_ignore_rule_must_pin_a_timer_or_a_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaises(ValueError):
                store.add_incident_mute()

    def test_duplicate_ignore_rules_collapse_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            first = store.add_incident_mute(incident_type="run_failed", reason="one")
            second = store.add_incident_mute(incident_type="run_failed", reason="two")

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(store.list_incident_mutes()), 1)

    def test_pagination_walks_the_filtered_set_without_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for index in range(25):
                store.add_incident("error", "run_failed", f"boom {index}", "t")

            first = store.list_incidents(limit=10, offset=0)
            second = store.list_incidents(limit=10, offset=10)
            third = store.list_incidents(limit=10, offset=20)

            self.assertEqual([len(first), len(second), len(third)], [10, 10, 5])
            ids = {r["id"] for r in first + second + third}
            self.assertEqual(len(ids), 25)
            self.assertEqual(store.count_incidents(), 25)

    def test_summary_separates_open_from_recorded_and_builds_a_trend(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for _ in range(4):
                store.add_incident("error", "run_failed", "boom", "timer-a")
            store.add_incident("warn", "crash_recovery", "recovered", "timer-b")
            store.ack_incidents(incident_type="run_failed")

            summary = store.incident_summary(days=30)

            self.assertEqual(summary["totals"]["total"], 5)
            self.assertEqual(summary["totals"]["unacked"], 1)
            by_type = {r["key"]: r for r in summary["by_type"]}
            self.assertEqual(by_type["run_failed"]["total"], 4)
            self.assertEqual(by_type["run_failed"]["unacked"], 0)
            self.assertEqual(by_type["crash_recovery"]["unacked"], 1)
            today = datetime.now(timezone.utc).date().isoformat()
            self.assertEqual([d["day"] for d in summary["trend"]], [today])
            self.assertEqual(summary["trend"][0]["total"], 5)

    def test_summary_window_excludes_incidents_older_than_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_incident("error", "run_failed", "recent", "t")
            stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            store._connect().execute(
                "UPDATE incidents SET created_at = ? WHERE id = 1", (stale,)
            )
            store.add_incident("error", "run_failed", "today", "t")

            summary = store.incident_summary(days=30)

            self.assertEqual(summary["totals"]["total"], 2)
            self.assertEqual(summary["recent"]["total"], 1)

    def test_prune_reclaims_resolved_incidents_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            resolved = store.add_incident("error", "run_failed", "old resolved", "t")
            still_open = store.add_incident("error", "run_failed", "old open", "t")
            store.ack_incident(resolved)
            store._connect().execute("UPDATE incidents SET created_at = ?", (old,))

            summary = store.prune_old_data(retention_days=30)

            self.assertEqual(summary["incidents"], 1)
            remaining = [r["id"] for r in store.list_incidents()]
            self.assertEqual(remaining, [still_open])


class IncidentServiceTests(unittest.TestCase):
    def _service(self, tmp: str) -> WakeLiteService:
        service = WakeLiteService.__new__(WakeLiteService)
        service.state = StateStore(Path(tmp) / "state.db")
        return service

    def test_bulk_ack_replays_the_stored_result_for_a_repeated_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            for _ in range(3):
                service.state.add_incident("error", "run_failed", "boom", "t")

            first = service.ack_incidents_bulk(idempotency_key="k1", incident_type="run_failed")
            service.state.add_incident("error", "run_failed", "later", "t")
            replay = service.ack_incidents_bulk(idempotency_key="k1", incident_type="run_failed")

            self.assertEqual(first["acknowledged_count"], 3)
            # A replay must not resolve the incident raised after the first call.
            self.assertEqual(replay["acknowledged_count"], 3)
            self.assertEqual(service.state.count_unacked_incidents(), 1)

    def test_summary_labels_timers_that_no_longer_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)

            class _Store:
                @staticmethod
                def list_timers():
                    return [{"id": "live-timer", "name": "plane-health"}]

            service.timer_store = _Store()
            service.state.add_incident("error", "run_failed", "a", "live-timer")
            service.state.add_incident("error", "run_failed", "b", "gone-timer-uuid")

            by_timer = {r["key"]: r for r in service.incident_summary()["by_timer"]}

            self.assertEqual(by_timer["live-timer"]["timer_name"], "plane-health")
            self.assertTrue(by_timer["live-timer"]["timer_exists"])
            self.assertFalse(by_timer["gone-timer-uuid"]["timer_exists"])
            self.assertIn("deleted", by_timer["gone-timer-uuid"]["timer_name"])


if __name__ == "__main__":
    unittest.main()
