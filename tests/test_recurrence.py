import unittest
from datetime import datetime

from wakelite.recurrence import (
    RecurrenceError,
    interval_is_due,
    interval_window_occurrences,
    next_window_occurrence,
    occurrences_between,
    parse_interval,
    parse_recurrence,
)


class ParseIntervalTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_interval("10s"), 10)
        self.assertEqual(parse_interval("90s"), 90)

    def test_below_minimum_rejected(self):
        with self.assertRaises(RecurrenceError):
            parse_interval("1s")
        with self.assertRaises(RecurrenceError):
            parse_interval("9s")
        with self.assertRaises(RecurrenceError):
            parse_interval("0s")

    def test_below_minimum_allowed_with_allow_zero(self):
        self.assertEqual(parse_interval("0s", allow_zero=True), 0)
        self.assertEqual(parse_interval("1s", allow_zero=True), 1)

    def test_minutes(self):
        self.assertEqual(parse_interval("5m"), 300)
        self.assertEqual(parse_interval("1m"), 60)

    def test_hours(self):
        self.assertEqual(parse_interval("2h"), 7200)
        self.assertEqual(parse_interval("1h"), 3600)

    def test_zero_seconds_requires_allow_zero(self):
        with self.assertRaises(RecurrenceError):
            parse_interval("0s")
        self.assertEqual(parse_interval("0s", allow_zero=True), 0)

    def test_whitespace_stripped(self):
        self.assertEqual(parse_interval("  10s  "), 10)

    def test_invalid_format(self):
        with self.assertRaises(RecurrenceError):
            parse_interval("10")
        with self.assertRaises(RecurrenceError):
            parse_interval("abc")
        with self.assertRaises(RecurrenceError):
            parse_interval("10d")
        with self.assertRaises(RecurrenceError):
            parse_interval("")


class IntervalIsDueTests(unittest.TestCase):
    def test_never_fired_is_due(self):
        now = datetime(2026, 2, 24, 12, 0, 0)
        self.assertTrue(interval_is_due(None, now, 10))

    def test_enough_elapsed(self):
        last = datetime(2026, 2, 24, 12, 0, 0)
        now = datetime(2026, 2, 24, 12, 0, 15)
        self.assertTrue(interval_is_due(last, now, 10))

    def test_not_enough_elapsed(self):
        last = datetime(2026, 2, 24, 12, 0, 0)
        now = datetime(2026, 2, 24, 12, 0, 5)
        self.assertFalse(interval_is_due(last, now, 10))

    def test_exact_boundary(self):
        last = datetime(2026, 2, 24, 12, 0, 0)
        now = datetime(2026, 2, 24, 12, 0, 10)
        self.assertTrue(interval_is_due(last, now, 10))

    def test_zero_seconds_daemon_never_fired(self):
        now = datetime(2026, 2, 24, 12, 0, 0)
        self.assertTrue(interval_is_due(None, now, 0))

    def test_zero_seconds_daemon_already_fired(self):
        # With every_seconds=0, elapsed >= 0 is always true, so interval_is_due returns True.
        # In practice, daemon timers (which use 0s) skip _process_interval_due entirely
        # and are managed by _process_daemons instead.
        last = datetime(2026, 2, 24, 12, 0, 0)
        now = datetime(2026, 2, 24, 12, 0, 1)
        self.assertTrue(interval_is_due(last, now, 0))


class IntervalRecurrenceParseTests(unittest.TestCase):
    def test_parse_interval_recurrence(self):
        timer = {"recurrence": {"frequency": "interval", "every": "30s"}}
        rec = parse_recurrence(timer)
        self.assertEqual(rec.frequency, "interval")
        self.assertEqual(rec.every_seconds, 30)

    def test_interval_missing_every_raises(self):
        timer = {"recurrence": {"frequency": "interval"}}
        with self.assertRaises(RecurrenceError):
            parse_recurrence(timer)

    def test_interval_occurrences_between_returns_empty(self):
        timer = {"recurrence": {"frequency": "interval", "every": "10s"}}
        occ = occurrences_between(timer, datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual(occ, [])


class RecurrenceTests(unittest.TestCase):
    def test_once_occurs_exactly_once(self):
        timer = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "recurrence": {
                "frequency": "once",
                "date": "2026-02-27",
                "time": "06:55",
            },
        }

        occ = occurrences_between(timer, datetime(2026, 2, 1, 0, 0, 0), datetime(2026, 3, 1, 0, 0, 0))
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0].date().isoformat(), "2026-02-27")
        self.assertEqual((occ[0].hour, occ[0].minute), (6, 55))

        after = occurrences_between(timer, datetime(2026, 2, 28, 0, 0, 0), datetime(2026, 3, 31, 0, 0, 0))
        self.assertEqual(after, [])

    def test_monthly_day_of_month_falls_back_to_last_day(self):
        timer = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "recurrence": {
                "frequency": "monthly",
                "time": "01:55",
                "interval": 1,
                "monthly_mode": "day_of_month",
                "day_of_month": 31,
            },
        }

        occ = occurrences_between(timer, datetime(2026, 2, 1, 0, 0, 0), datetime(2026, 2, 28, 23, 59, 59))
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0].day, 28)
        self.assertEqual((occ[0].hour, occ[0].minute), (1, 55))

    def test_monthly_last_sunday(self):
        timer = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "recurrence": {
                "frequency": "monthly",
                "time": "06:55",
                "interval": 1,
                "monthly_mode": "nth_weekday",
                "nth": -1,
                "weekday": "Sun",
            },
        }

        occ = occurrences_between(timer, datetime(2026, 3, 1, 0, 0, 0), datetime(2026, 3, 31, 23, 59, 59))
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0].day, 29)
        self.assertEqual(occ[0].weekday(), 6)

    def test_weekly_multiple_days(self):
        timer = {
            "created_at": "2026-02-01T00:00:00+00:00",
            "recurrence": {
                "frequency": "weekly",
                "time": "07:00",
                "interval": 1,
                "weekly_days": ["Mon", "Wed", "Fri"],
            },
        }

        occ = occurrences_between(timer, datetime(2026, 2, 2, 0, 0, 0), datetime(2026, 2, 8, 23, 59, 59))
        weekdays = [o.weekday() for o in occ]
        self.assertEqual(weekdays, [0, 2, 4])


class OnceTimerPastDateTests(unittest.TestCase):
    """Test that one-time timers with past dates are handled correctly."""

    def test_once_past_date_no_future_occurrences(self):
        """A once-timer for yesterday has no future occurrences."""
        timer = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "recurrence": {
                "frequency": "once",
                "date": "2026-02-20",
                "time": "10:00",
            },
        }
        # Query from 2026-02-25 onward — should find nothing.
        occ = occurrences_between(
            timer,
            datetime(2026, 2, 25, 0, 0, 0),
            datetime(2026, 3, 25, 0, 0, 0),
        )
        self.assertEqual(occ, [])

    def test_once_today_still_has_occurrence(self):
        """A once-timer for today should still show in today's window."""
        timer = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "recurrence": {
                "frequency": "once",
                "date": "2026-02-25",
                "time": "14:00",
            },
        }
        occ = occurrences_between(
            timer,
            datetime(2026, 2, 25, 0, 0, 0),
            datetime(2026, 2, 25, 23, 59, 59),
        )
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0].hour, 14)


class IntervalWindowTests(unittest.TestCase):
    """Tests for active_hours windowed interval timers."""

    def _make_recurrence(self, every="302m", start="07:00", end="22:30"):
        timer = {
            "recurrence": {
                "frequency": "interval",
                "every": every,
                "active_hours": {"start": start, "end": end},
            }
        }
        return parse_recurrence(timer)

    def test_interval_window_occurrences_302m(self):
        """302m from 07:00 to 22:30 → 07:00, 12:02, 17:04, 22:06."""
        from datetime import date
        rec = self._make_recurrence()
        occs = interval_window_occurrences(rec, date(2026, 3, 1))
        times = [(o.hour, o.minute) for o in occs]
        self.assertEqual(times, [(7, 0), (12, 2), (17, 4), (22, 6)])

    def test_interval_window_occurrences_60m(self):
        """60m from 09:00 to 11:30 → 09:00, 10:00, 11:00."""
        from datetime import date
        rec = self._make_recurrence(every="60m", start="09:00", end="11:30")
        occs = interval_window_occurrences(rec, date(2026, 3, 1))
        times = [(o.hour, o.minute) for o in occs]
        self.assertEqual(times, [(9, 0), (10, 0), (11, 0)])

    def test_next_window_occurrence_today(self):
        """At 10:00, next fire is 12:02."""
        rec = self._make_recurrence()
        now = datetime(2026, 3, 1, 10, 0, 0)
        nxt = next_window_occurrence(rec, now)
        self.assertEqual(nxt, datetime(2026, 3, 1, 12, 2, 0))

    def test_next_window_occurrence_exact_match_skipped(self):
        """At exactly 12:02:00, next fire is 17:04 (not 12:02 itself)."""
        rec = self._make_recurrence()
        now = datetime(2026, 3, 1, 12, 2, 0)
        nxt = next_window_occurrence(rec, now)
        self.assertEqual(nxt, datetime(2026, 3, 1, 17, 4, 0))

    def test_next_window_occurrence_tomorrow(self):
        """At 23:00 (past window), next fire is tomorrow 07:00."""
        rec = self._make_recurrence()
        now = datetime(2026, 3, 1, 23, 0, 0)
        nxt = next_window_occurrence(rec, now)
        self.assertEqual(nxt, datetime(2026, 3, 2, 7, 0, 0))

    def test_next_window_occurrence_before_window(self):
        """At 05:00 (before window), next fire is today 07:00."""
        rec = self._make_recurrence()
        now = datetime(2026, 3, 1, 5, 0, 0)
        nxt = next_window_occurrence(rec, now)
        self.assertEqual(nxt, datetime(2026, 3, 1, 7, 0, 0))

    def test_parse_recurrence_active_hours(self):
        """active_hours fields are parsed correctly."""
        rec = self._make_recurrence()
        from datetime import time
        self.assertEqual(rec.active_hours_start, time(7, 0))
        self.assertEqual(rec.active_hours_end, time(22, 30))
        self.assertEqual(rec.every_seconds, 302 * 60)

    def test_active_hours_end_before_start_rejected(self):
        """end < start raises RecurrenceError."""
        with self.assertRaises(RecurrenceError):
            self._make_recurrence(start="22:00", end="07:00")

    def test_windowed_interval_occurrences_between(self):
        """occurrences_between returns windowed fires for wake intents."""
        timer = {
            "recurrence": {
                "frequency": "interval",
                "every": "302m",
                "active_hours": {"start": "07:00", "end": "22:30"},
            }
        }
        start = datetime(2026, 3, 1, 0, 0, 0)
        end = datetime(2026, 3, 1, 23, 59, 59)
        occs = occurrences_between(timer, start, end)
        times = [(o.hour, o.minute) for o in occs]
        self.assertEqual(times, [(7, 0), (12, 2), (17, 4), (22, 6)])

    def test_windowed_interval_occurrences_between_multi_day(self):
        """occurrences_between across 2 days returns 8 fires."""
        timer = {
            "recurrence": {
                "frequency": "interval",
                "every": "302m",
                "active_hours": {"start": "07:00", "end": "22:30"},
            }
        }
        start = datetime(2026, 3, 1, 0, 0, 0)
        end = datetime(2026, 3, 2, 23, 59, 59)
        occs = occurrences_between(timer, start, end)
        self.assertEqual(len(occs), 8)  # 4 per day × 2 days


if __name__ == "__main__":
    unittest.main()
