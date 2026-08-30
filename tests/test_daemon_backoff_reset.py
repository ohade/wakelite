"""Restart backoff must recover after a daemon proves it is healthy.

Before this, current_backoff_seconds only ever grew. It was cleared in exactly
one place — the re-enable path — so a daemon that crash-looped once stayed
pinned at restart_max_backoff_seconds forever, waiting the full cap after every
later crash no matter how long it had run successfully in between. Measured on
2026-08-30, all four daemons on the dev machine were pinned at the 300s cap.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wakelite.config import DAEMON_HEALTHY_UPTIME_SECONDS
from wakelite.service import WakeLiteService


class HealthyUptimeThresholdTests(unittest.TestCase):
    def test_default_floor_applies_when_no_restart_delay_is_configured(self):
        threshold = WakeLiteService._healthy_uptime_threshold({})
        self.assertEqual(threshold, float(DAEMON_HEALTHY_UPTIME_SECONDS))

    def test_a_short_restart_delay_does_not_lower_the_bar(self):
        # A 5s restart delay must not mean "healthy after 5s" — a daemon dying
        # at 6s would then clear the backoff on every attempt and never back off.
        timer = {"execution": {"restart_delay_seconds": 5}}
        self.assertEqual(
            WakeLiteService._healthy_uptime_threshold(timer),
            float(DAEMON_HEALTHY_UPTIME_SECONDS),
        )

    def test_a_long_restart_delay_raises_the_bar_to_match(self):
        timer = {"execution": {"restart_delay_seconds": 600}}
        self.assertEqual(WakeLiteService._healthy_uptime_threshold(timer), 600.0)

    def test_a_malformed_restart_delay_falls_back_to_the_floor(self):
        timer = {"execution": {"restart_delay_seconds": "not-a-number"}}
        self.assertEqual(
            WakeLiteService._healthy_uptime_threshold(timer),
            float(DAEMON_HEALTHY_UPTIME_SECONDS),
        )

    def test_a_missing_execution_block_falls_back_to_the_floor(self):
        self.assertEqual(
            WakeLiteService._healthy_uptime_threshold({"execution": None}),
            float(DAEMON_HEALTHY_UPTIME_SECONDS),
        )


class BackoffResetIntegrationTests(unittest.TestCase):
    """Drive a real daemon through the runner and read the resulting state.

    The threshold is patched to a small value so the test does not have to wait
    the real 60s floor; the branch under test is the runner's own, not a copy.
    """

    def _service(self, td):
        from tests.test_service import _bootstrap

        return _bootstrap(td)

    def test_a_pinned_daemon_clears_after_it_stays_up_past_the_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = self._service(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer({
                "name": "daemon-healthy",
                "comment": "Stays up past the threshold, then exits",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "execution": {
                    "restart_on_failure": False,
                    "restart_delay_seconds": 1,
                    "restart_max_backoff_seconds": 300,
                },
                "command": {"mode": "shell", "shell": "sleep 2; exit 1"},
            })
            # Pinned at the cap, exactly like every daemon on the dev machine.
            svc.state.set_daemon_state(timer["id"], current_backoff_seconds=300, status="stopped")

            with patch.object(
                WakeLiteService, "_healthy_uptime_threshold", staticmethod(lambda t: 1.0)
            ):
                svc.start()
                try:
                    time.sleep(5)
                finally:
                    svc.stop()

            state = svc.state.get_daemon_state(timer["id"])
            self.assertEqual(
                state.current_backoff_seconds,
                0,
                "a daemon that stayed up past the threshold should clear its backoff",
            )

    def test_a_crash_looping_daemon_keeps_backing_off(self):
        with tempfile.TemporaryDirectory() as td:
            WakeLiteService = self._service(td)
            svc = WakeLiteService(tick_seconds=15)
            timer = svc.timer_store.create_timer({
                "name": "daemon-crashloop",
                "comment": "Exits immediately, should keep backing off",
                "enabled": True,
                "timer_type": "daemon",
                "recurrence": {"frequency": "interval", "every": "0s"},
                "execution": {
                    "restart_on_failure": True,
                    "restart_delay_seconds": 1,
                    "restart_max_backoff_seconds": 300,
                },
                "command": {"mode": "shell", "shell": "exit 1"},
            })

            # A high threshold no immediate crash can reach.
            with patch.object(
                WakeLiteService, "_healthy_uptime_threshold", staticmethod(lambda t: 300.0)
            ):
                svc.start()
                try:
                    time.sleep(5)
                finally:
                    svc.stop()

            state = svc.state.get_daemon_state(timer["id"])
            # stop() records the final run as "shutdown", which legitimately
            # zeroes the backoff, so assert on what the crashes did rather than
            # on the value left by teardown.
            self.assertGreaterEqual(
                state.restart_count,
                2,
                "a crash-looping daemon should have restarted repeatedly",
            )


if __name__ == "__main__":
    unittest.main()
