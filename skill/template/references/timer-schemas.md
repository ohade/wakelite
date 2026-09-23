# Timer JSON Schemas

## Required top-level keys

`name`, `comment`, `recurrence`, `command`

Optional: `wake`, `notifications`, `enabled`, `timezone`, `max_runs`, `until`, `resources`, `callback`, `timer_type`, `execution`

**Unknown keys are rejected.** Only these top-level keys are valid: `name`, `comment`, `command`, `recurrence`, `timer_type`, `execution`, `resources`, `wake`, `notifications`, `enabled`, `timezone`, `id`, `max_runs`, `until`, `callback`. Adding `description`, `interval_seconds`, and similar keys fails validation.

---

## Scheduled timer (daily/weekly/monthly/once)

```json
{
  "name": "my-timer",
  "comment": "What this timer does",
  "recurrence": {
    "frequency": "daily|weekly|monthly|once",
    "time": "HH:MM",
    "date": "YYYY-MM-DD",
    "weekly_days": ["Mon", "Thu"]
  },
  "command": {
    "mode": "shell",
    "shell": "echo hello",
    "workingDirectory": "/path"
  },
  "wake": {
    "enabled": true,
    "action": "wake",
    "leadMinutes": 2
  }
}
```

`date` is for `once`. `weekly_days` is for `weekly`.

---

## Interval timer (polling / monitoring)

```json
{
  "name": "my-poll",
  "comment": "Check something every 2 min",
  "recurrence": {"frequency": "interval", "every": "2m"},
  "command": {"mode": "shell", "shell": "/path/to/script.sh"},
  "notifications": {"onFailure": true}
}
```

Supported `every` values: `10s`, `30s`, `1m`, `2m`, `5m`, `30m`, `1h`, and so on.

**Gotcha: the interval format is single-unit only.** Combined units like `5h2m` are rejected:
```
error: Invalid interval format: '5h2m'. Use Ns, Nm, or Nh (e.g. '10s', '5m', '2h')
```
Convert to total minutes instead: `302m` (= 5h2m).

**Minimum interval:** `10s`. Daemon timers use `"every": "0s"` to signal continuous operation.

---

## Interval timer with an active_hours window

A plain interval timer has no concept of time of day, so "every 5h starting at 7 AM" drifts
over days. `active_hours` constrains it to a daily window:

```json
{
  "name": "daytime-poll",
  "comment": "Run every 302m, only between 07:00 and 22:30",
  "recurrence": {
    "frequency": "interval",
    "every": "302m",
    "active_hours": {"start": "07:00", "end": "22:30"}
  },
  "command": {"mode": "shell", "shell": "/path/to/script.sh"}
}
```

- The timer is suppressed outside the `start`–`end` window.
- At the window `start` the timer fires immediately (anchored).
- Later fires are `every` after the previous fire, inside the window.
- A fire that would land outside the window is deferred to the next day's `start`.

---

## MANDATORY: Poll-Until-Done pattern for monitors

**Any timer that polls for a condition (build status, deploy health) must use this pattern:**

```json
{
  "until": {"on_success": "delete", "on_failure": "continue"},
  "command": {"mode": "shell", "shell": "/path/to/check.sh"}
}
```

**Script exit codes:** `0` = done (triggers delete), `75` = not ready (keep polling), `1` = error.

Without `until`, an interval timer keeps firing after success and repeats its actions and
notifications. `max_runs` alone is not a substitute, because it allows N duplicate successes
before it stops.

---

## max_runs and until

**`max_runs`** disables the timer after N successful completions:
```json
{"max_runs": 5}
```

**`until`** needs both fields when present:
```json
{"until": {"on_success": "delete", "on_failure": "continue"}}
```
- `"delete"` deletes the timer when this outcome occurs. `"continue"` does nothing.
- Aborted runs and waiting runs (exit 75) trigger neither condition.
- It composes with `max_runs`, and the first limit reached wins. `until` **deletes** the timer
  and `max_runs` **disables** it.
- Run history survives the delete.

---

## Resource declarations

Declare shared resources so WakeLite warns about conflicts:
```json
{"resources": [{"name": "claude-cli", "description": "Claude CLI process"}]}
```
`timer create` and `timer update` print a warning when another timer declares the same resource
name. The warning is advisory, and the write still succeeds.

---

## Terminal callback

```json
{
  "callback": {
    "type": "ghostty",
    "session_id": "abc-def-123"
  }
}
```

`type` is `wezterm`, `ghostty`, or `cmux`. The CLI and API auto-capture the matching terminal IDs
from the environment at creation. See `callbacks.md` for delivery rules.

---

## Daemon timer (`timer_type: "daemon"`)

```json
{
  "name": "my-daemon",
  "comment": "Keeps my-process running",
  "timer_type": "daemon",
  "recurrence": {"frequency": "interval", "every": "0s"},
  "command": {"mode": "shell", "shell": "/path/to/start-process.sh"},
  "execution": {
    "restart_on_failure": true,
    "restart_delay_seconds": 5,
    "restart_max_backoff_seconds": 300
  }
}
```

---

## Common validation errors

- **Weekly days:** use `"weekly_days": ["Mon"]` with `Mon`…`Sun`. The legacy `"day"` field is not
  read. A weekly timer without `weekly_days` defaults to Monday.
- `"schedule"` instead of `"recurrence"` fails with a hint.
- `"minutes_before"` instead of `"leadMinutes"` in `wake` fails with a hint.
- A missing `"comment"` fails validation.
- `command.mode` must be `shell` or `exec`. There is no `"claude"` mode.
- Creating or enabling a `once` timer for a past date fails. An expired `once` timer already in
  the store is deleted on the first scheduler tick. Disabled timers are exempt.

Examples: `{{WAKELITE_REPO}}/docs/*.timer.json`. Full schema: `{{WAKELITE_REPO}}/docs/API.md`.
