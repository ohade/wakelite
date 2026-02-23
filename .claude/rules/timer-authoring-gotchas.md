# Timer Authoring Gotchas

Hard-won lessons from building and debugging timers. Read before creating timers programmatically.

## Schema traps

- **Top-level key is `recurrence`, NOT `schedule`.** Using `"schedule"` is silently ignored — timer gets empty recurrence and null `next_run`. Validation now catches this with a hint, but older versions didn't.
- **Once-timers use separate `date` + `time` fields**, not a combined `at` field: `{"frequency": "once", "date": "2026-03-15", "time": "14:00"}`
- **`wake` and `leadMinutes` are top-level**, not nested inside `recurrence`.
- **`comment` is required.** Validation rejects timers without it.
- **Unknown top-level keys are rejected.** See `ALLOWED_PAYLOAD_KEYS` in `timer_store.py`. Adding `description`, `interval_seconds`, etc. causes an error.

## Interval timers

- **Single-unit only.** `5h2m` is rejected — convert to `302m`.
- **Minimum 10 seconds** for scheduled timers. Daemon timers use `0s` (special case).
- **Active hours** (`active_hours: {start, end}`) anchors interval fires to a daily window. Fires step from `start` by `every`. Fires outside the window are deferred to next day's `start`.

## Daemon timers

- **Recurrence must be `{"frequency": "interval", "every": "0s"}`.** Zero-second interval is only valid for daemons.
- **`restart_on_failure: true` does NOT restart after clean exit (code 0).** Exit 0 = intentional stop. A daemon killed by SIGTERM stays stopped.
- **Disable/re-enable resets backoff.** If a daemon is stuck in exponential backoff after repeated failures, toggle enable off then on.
- **Runner restarts accumulate backoff.** Each restart SIGTERMs running daemons → SIGTERM exit → backoff increments. Multiple quick restarts can push backoff to max.

## Once-timers

- **Past dates are rejected.** Creating or enabling a once-timer for a date before today raises `ValueError`.
- **Expired once-timers auto-delete** on the first scheduler tick after runner restart.

## Lifecycle controls

- **`until` deletes; `max_runs` disables.** First limit reached wins when both are set.
- **Exit code 75 = "waiting".** Polling scripts should `exit 75` (not `exit 1`) when the condition isn't met yet. Blue "Waiting" rows in dashboard, no failure notifications, no incidents, no `until` trigger.
- **Aborted runs don't trigger `until` conditions.** Neither does exit 75.

## CLI gotchas

- **Delete requires timer UUID, not name.** Look up ID from `timer list` output first.
- **Different idempotency keys = different operations.** Using a new key with the same timer name creates a duplicate. Reuse the key to get idempotent behavior, or delete before recreating.
- **`timer update` supports inline flags** (`--name`, `--comment`, `--shell`, `--enabled`) for quick edits without a JSON file.
