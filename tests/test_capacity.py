"""Unit tests for wakelite.capacity — pure functions, no I/O bootstrap needed."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from wakelite.capacity import (
    DEFAULT_BUCKET_SECONDS,
    DEFAULT_HORIZON_DAYS,
    EXECUTOR_RESOURCE,
    ResourceUsage,
    check_capacity,
    collect_timer_usage,
    parse_rate,
    project_concurrent_usage,
    resource_capacity,
)


def _daemon(name, max_concurrent=1, overlap="queue", enabled=True, resources=None):
    t = {
        "id": name,
        "name": name,
        "enabled": enabled,
        "timer_type": "daemon",
        "recurrence": {"frequency": "interval", "every": "0s"},
        "command": {"mode": "shell", "shell": "sleep 999"},
    }
    if overlap == "allow":
        t["execution"] = {"overlap": "allow", "max_concurrent": max_concurrent}
    if resources:
        t["resources"] = resources
    return t


def _once(name, date_str, time_str="10:00", enabled=True, resources=None):
    t = {
        "id": name,
        "name": name,
        "enabled": enabled,
        "recurrence": {"frequency": "once", "date": date_str, "time": time_str},
        "command": {"mode": "shell", "shell": "echo ok"},
    }
    if resources:
        t["resources"] = resources
    return t


def _daily(name, time_str="06:00", enabled=True, resources=None):
    t = {
        "id": name,
        "name": name,
        "enabled": enabled,
        "recurrence": {"frequency": "daily", "time": time_str},
        "command": {"mode": "shell", "shell": "echo ok"},
    }
    if resources:
        t["resources"] = resources
    return t


class ParseRateTests(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(parse_rate(None))

    def test_int(self):
        self.assertEqual(parse_rate(50), 50)

    def test_float(self):
        self.assertEqual(parse_rate(12.7), 12)

    def test_negative(self):
        self.assertIsNone(parse_rate(-1))
        self.assertIsNone(parse_rate("-5 req/min"))

    def test_bare_digit_string(self):
        self.assertEqual(parse_rate("50"), 50)

    def test_per_minute(self):
        self.assertEqual(parse_rate("50 req/min"), 50)
        self.assertEqual(parse_rate("50 req/minute"), 50)
        self.assertEqual(parse_rate("50req/m"), 50)

    def test_per_second(self):
        self.assertEqual(parse_rate("12 req/sec"), 12 * 60)

    def test_per_hour(self):
        self.assertEqual(parse_rate("60 req/h"), 1)
        self.assertEqual(parse_rate("30 req/h"), 1)  # floor to 1

    def test_unitless_units(self):
        self.assertEqual(parse_rate("10 gb"), 10)

    def test_unparseable(self):
        self.assertIsNone(parse_rate("lots"))
        self.assertIsNone(parse_rate(""))
        self.assertIsNone(parse_rate(True))

    def test_float_ceiling(self):
        self.assertEqual(parse_rate("1.1 req/min"), 2)


class CollectTimerUsageTests(unittest.TestCase):
    def test_disabled_yields_nothing(self):
        self.assertEqual(collect_timer_usage(_daemon("d", enabled=False)), [])

    def test_daemon_consumes_one_slot(self):
        usages = collect_timer_usage(_daemon("d"))
        self.assertEqual(usages, [ResourceUsage(EXECUTOR_RESOURCE, 1)])

    def test_overlap_allow_reports_max_concurrent(self):
        usages = collect_timer_usage(_daemon("d", overlap="allow", max_concurrent=3))
        self.assertEqual(usages[0], ResourceUsage(EXECUTOR_RESOURCE, 3))

    def test_user_resource_with_estimated_usage_added(self):
        usages = collect_timer_usage(
            _daemon(
                "d",
                resources=[
                    {"name": "slack", "capacity": "50 req/min", "estimated_usage": "12 req/min"}
                ],
            )
        )
        names = {u.name: u.amount for u in usages}
        self.assertEqual(names[EXECUTOR_RESOURCE], 1)
        self.assertEqual(names["slack"], 12)

    def test_user_resource_with_unparseable_estimate_skipped(self):
        usages = collect_timer_usage(
            _daemon("d", resources=[{"name": "opaque", "estimated_usage": "many"}])
        )
        self.assertEqual([u.name for u in usages], [EXECUTOR_RESOURCE])


class ResourceCapacityTests(unittest.TestCase):
    def test_executor_returns_max_workers(self):
        self.assertEqual(resource_capacity(EXECUTOR_RESOURCE, [], 16), 16)

    def test_user_resource_first_parseable_capacity(self):
        timers = [
            _daemon("a", resources=[{"name": "slack", "capacity": "50 req/min"}]),
            _daemon("b", resources=[{"name": "slack", "capacity": "nope"}]),
        ]
        self.assertEqual(resource_capacity("slack", timers, 16), 50)

    def test_unknown_resource_returns_none(self):
        self.assertIsNone(resource_capacity("ghost", [], 16))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 4, 22, 0, 0, 0)

    def _future_date(self, days):
        return (self.now + timedelta(days=days)).date().isoformat()

    def test_daemon_covers_every_bucket(self):
        proj = project_concurrent_usage([_daemon("d")], self.now, horizon_days=1, bucket_seconds=60)
        arr = proj[EXECUTOR_RESOURCE]
        self.assertEqual(arr[0], 1)
        self.assertEqual(arr[-1], 1)
        self.assertEqual(sum(arr), len(arr))

    def test_two_once_timers_same_minute_sum(self):
        d1 = self._future_date(1)
        proj = project_concurrent_usage(
            [_once("a", d1, "10:00"), _once("b", d1, "10:00")],
            self.now,
            horizon_days=7,
            bucket_seconds=60,
        )
        self.assertEqual(max(proj[EXECUTOR_RESOURCE]), 2)

    def test_two_once_timers_different_days_do_not_overlap(self):
        proj = project_concurrent_usage(
            [_once("a", self._future_date(1), "10:00"), _once("b", self._future_date(2), "10:00")],
            self.now,
            horizon_days=7,
            bucket_seconds=60,
        )
        self.assertEqual(max(proj[EXECUTOR_RESOURCE]), 1)

    def test_disabled_contributes_nothing(self):
        proj = project_concurrent_usage(
            [_daemon("on"), _daemon("off", enabled=False)],
            self.now,
            horizon_days=1,
            bucket_seconds=60,
        )
        self.assertEqual(max(proj[EXECUTOR_RESOURCE]), 1)


class CheckCapacityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 4, 22, 0, 0, 0)

    def _future_date(self, days):
        return (self.now + timedelta(days=days)).date().isoformat()

    def test_twenty_once_timers_spread_all_succeed_at_max_workers_2(self):
        existing: list = []
        for i in range(20):
            new = _once(f"once-{i}", self._future_date(1 + (i % 7)), f"{10 + (i % 8):02d}:{(i * 7) % 60:02d}")
            ok, err = check_capacity(new, existing, max_workers=2, now=self.now)
            self.assertTrue(ok, f"once-{i} blocked: {err}")
            existing.append(new)

    def test_three_daemons_exceed_max_workers_2(self):
        existing = [_daemon("a"), _daemon("b")]
        ok, err = check_capacity(_daemon("c"), existing, max_workers=2, now=self.now)
        self.assertFalse(ok)
        self.assertIn(EXECUTOR_RESOURCE, err)
        self.assertIn("capacity 2", err)

    def test_daemon_at_max_workers_allowed(self):
        existing = [_daemon("a")]
        ok, err = check_capacity(_daemon("b"), existing, max_workers=2, now=self.now)
        self.assertTrue(ok, err)

    def test_user_resource_under_capacity_allowed(self):
        existing = [
            _daemon(
                "poller",
                resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "20 req/min"}],
            )
        ]
        new = _daemon(
            "notifier",
            resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "20 req/min"}],
        )
        ok, err = check_capacity(new, existing, max_workers=16, now=self.now)
        self.assertTrue(ok, err)

    def test_user_resource_over_capacity_blocked(self):
        existing = [
            _daemon(
                "poller",
                resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
            )
        ]
        new = _daemon(
            "notifier",
            resources=[{"name": "slack", "capacity": "50 req/min", "estimated_usage": "30 req/min"}],
        )
        ok, err = check_capacity(new, existing, max_workers=16, now=self.now)
        self.assertFalse(ok)
        self.assertIn("slack", err)

    def test_unparseable_user_resource_is_advisory(self):
        existing = [_daemon("a", resources=[{"name": "opaque", "estimated_usage": "lots"}])]
        new = _daemon("b", resources=[{"name": "opaque", "estimated_usage": "lots"}])
        ok, err = check_capacity(new, existing, max_workers=16, now=self.now)
        self.assertTrue(ok, err)

    def test_exclude_self_on_update(self):
        existing = [_daemon("a"), _daemon("b")]
        # Update existing 'a' without adding load — should be allowed even if cap is tight
        updated = dict(existing[0])
        ok, err = check_capacity(updated, existing, max_workers=2, now=self.now, exclude_timer_id="a")
        self.assertTrue(ok, err)

    def test_two_dailies_same_hour_blocked_at_executor_1(self):
        existing = [_daily("a", "06:00")]
        ok, err = check_capacity(_daily("b", "06:00"), existing, max_workers=1, now=self.now)
        self.assertFalse(ok)

    def test_two_dailies_different_hour_allowed_at_executor_1(self):
        existing = [_daily("a", "06:00")]
        ok, err = check_capacity(_daily("b", "09:00"), existing, max_workers=1, now=self.now)
        self.assertTrue(ok, err)


if __name__ == "__main__":
    unittest.main()
