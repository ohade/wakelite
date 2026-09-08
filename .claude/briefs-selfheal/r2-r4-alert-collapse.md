# R2 + R4 — Alert and incident collapse, and the flap breaker

Read `_common.md` in this directory first; it holds the incident, the test conventions,
the acceptance criteria, and the scope boundaries.

## Problem

One root cause produced 38 incidents and a Slack alert per restart. Over 30 days the
machine accumulated 272 `run_failed` incidents, 145 of which were acknowledged by hand,
and 142 sit unacknowledged now. The noise makes a real failure invisible.

Note before you start: the alert storm is NOT only the failure notification. Each run
posts to Slack up to three times — "started" at `service.py:2215`, the failure thread at
`service.py:2372`, and the status reply at `service.py:2425`. Collapsing incidents alone
would still leave two Slack posts per failed restart, so you must handle all three.

## R2, part 1 — streak state

Add columns to `timer_runtime`: `failure_streak`, `streak_message`, `streak_started_at`,
`last_notified_streak`. Follow the existing additive migration pattern — `PRAGMA
table_info` then `ALTER TABLE` — at `state.py:202-212`, which is idempotent on restart.
`get_runtime` is at `state.py:282`.

## R2, part 2 — collapse the incidents

`add_incident` (`state.py:735`) is insert-only. Add a `bump_incident(timer_id, type,
message)` that instead updates the currently open incident's count and last-seen time.
Keep `add_incident`'s existing mute behaviour intact: it consults `matching_mute`
(`state.py:718`) and pre-acknowledges a muted incident.

## R2, part 3 — notification policy

For the same timer with the same failure message: notify at streak 1, 3, and 10, then at
most once per hour while the streak continues. While a streak of 2 or more is open,
suppress the per-run "started" and "status" Slack posts. When a success ends a streak of
3 or more, send exactly one "recovered after N failures" message.

## R2, part 4 — make notifications countable

Add one INFO log line per delivered notification, for example
`notify.sent timer=<id> kind=<k> streak=<n>`. Today `notifier.py` logs only delivery
failures, so there is no way to verify "we sent 3 alerts" from the logs. The lead needs
this line to verify the behaviour live.

## R2, part 5 — persist the Slack thread

`get_daily_thread_ts` caches the thread id in memory (`notifier.py:56` and `:200`), so
every runner restart opens a new daily thread. Persist it in the `meta` table keyed by
day and read it back on startup.

## R2, part 6 — surface silent waits

A timer that has been in status `waiting` (exit code 75, EX_TEMPFAIL) for more than 24
hours should raise one `info` incident. Today `mcp-feature-catalog-protocol-health` has
1,238 silent waits over 30 days and nothing reports it.

## R4 — the flap breaker, deliberately minimal

Do NOT introduce a new daemon status such as `flapping`. The restart gate at
`service.py:2597` is `if ds.status == "stopped"`, so any other status falls through to
the unconditional spawn on every tick and the daemon would spin.

Instead:

1. Raise `DAEMON_HEALTHY_UPTIME_SECONDS` (`config.py:37`) from 60 to 300. With 60, a
   daemon that dies every 65 seconds resets its backoff every time (reset arm at
   `service.py:2342-2349`) and restarts forever without ever being recognised as
   flapping.
2. Rely on the existing per-timer `restart_max_backoff_seconds`
   (`timer_store.py:21`) for the slow-retry behaviour, and let R2's streak rule supply
   the single incident and the single alert.

Changing timer JSON values on the live machine is the lead's job; your part is the
constant and any code that reads it.

## Tests

Place them above the `__main__` guard.

1. Ten consecutive failing runs of one timer produce ONE incident row carrying a count
   of 10, three failure notifications, and no "started" posts after the second run.
2. A success after those failures produces exactly one recovery notification and clears
   the streak.
3. A different failure message starts a new streak rather than extending the old one.
4. A daemon whose uptime is below the healthy threshold does not reset its backoff; one
   whose uptime exceeds it does.
