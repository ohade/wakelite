import tempfile
import unittest
from pathlib import Path

from wakelite.notifier import Notifier
from wakelite.service import WakeLiteService
from wakelite.state import StateStore


class NotificationMuteAuditTests(unittest.TestCase):
    def _service(self, db_path: Path) -> WakeLiteService:
        service = WakeLiteService.__new__(WakeLiteService)
        service.state = StateStore(db_path)
        service.notifier = Notifier()
        return service

    def test_state_changes_record_time_and_source_without_idempotent_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary) / "state.db")

            muted = service.set_notifications_muted(True, source="http-api:web-ui")
            unchanged = service.set_notifications_muted(True, source="http-api:retry")
            unmuted = service.set_notifications_muted(False, source="maintenance-cli")

            self.assertTrue(muted["changed"])
            self.assertIsNotNone(muted["audit_entry"]["changed_at"])
            self.assertEqual(muted["audit_entry"]["source"], "http-api:web-ui")
            self.assertFalse(unchanged["changed"])
            self.assertIsNone(unchanged["audit_entry"])
            self.assertTrue(unmuted["changed"])

            settings = service.get_notification_settings()
            self.assertFalse(settings["notifications_muted"])
            self.assertEqual(
                [(entry["muted"], entry["source"]) for entry in settings["audit"]],
                [(False, "maintenance-cli"), (True, "http-api:web-ui")],
            )
            self.assertEqual(service.state.get_meta("notifications.muted"), "false")
            service.state._reset_connection()

    def test_invalid_source_cannot_change_mute_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary) / "state.db")

            with self.assertRaisesRegex(ValueError, "must not be empty"):
                service.set_notifications_muted(True, source="  ")

            self.assertFalse(service.notifier.muted)
            self.assertEqual(service.state.get_meta("notifications.muted"), None)
            self.assertEqual(service.state.list_notification_mute_changes(), [])
            service.state._reset_connection()


if __name__ == "__main__":
    unittest.main()
