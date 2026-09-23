---
name: wakelite-scheduling
description: |
  WakeLite timers and launchd scheduling on macOS. Use for scheduled commands, timers, reminders,
  polling monitors, wake-from-sleep, terminal callbacks into a Claude Code session, daemon timers,
  and WakeLite runner or launchd service issues.
version: 2.0.0
installed: {{INSTALL_DATE}}
---

# WakeLite: Wake + Command Scheduling

WakeLite is a local macOS scheduler. A launchd runner executes timers, and an optional root
daemon programs `pmset` so the Mac wakes from sleep before a timer fires.

**Repo:** `{{WAKELITE_REPO}}`
**State directory:** `{{WAKELITE_HOME}}` (`timers.json`, `state.db`, `logs/runner.log`)

## Architecture

| Component | launchd label | Role |
|-----------|---------------|------|
| Runner | `com.wakelite.runner` | Always-on user agent; stores timers, runs commands, serves the HTTP API and dashboard |
| Wake reconciler | `com.wakelite.wakereconciler` | Optional root daemon; turns wake intents into `pmset schedule` entries (install with `sudo`) |
| Watchdog | `com.wakelite.watchdog` | Optional health watchdog for the runner |
| CLI | `wakelitectl` | Create, list, update, and delete timers; health; doctor; launchd install |

The runner is the source of truth. Always go through `wakelitectl` or the HTTP API, never edit
the timer store by hand.

## Key CLI Commands

The `bin/wakelitectl` wrapper sets `PYTHONPATH` and prefers the repo's `.venv` itself.

```bash
WL={{WAKELITE_REPO}}/bin/wakelitectl

$WL health                                   # runner status
$WL doctor                                   # first command for ANY anomaly; works when the runner is hung
$WL timer list                               # all timers + next run
$WL timer create --file <json> --idempotency-key <key>
$WL timer update <id> --name <n> --comment "x" --shell "/s.sh" --enabled false
$WL timer update <id> --file patch.json      # partial JSON patch
$WL timer delete <id> --idempotency-key <key>
$WL timer enable|disable|run-now <id> --idempotency-key <key>
$WL timer clone <id> --name "copy" --idempotency-key <key>
$WL template list                            # built-in + user templates
$WL timer from-template <name> --name "x" --shell "cmd" --time "HH:MM" --date "YYYY-MM-DD" --comment "y"
$WL runs                                     # run history
$WL incidents                                # failures awaiting acknowledgement
$WL launchd status|restart                   # restart is required after code changes
```

Every mutating call requires `--idempotency-key`. Reusing a key replays the first result.
A new key with the same name creates a **second** timer.

## Quick-Start: One-Time Reminder

```bash
$WL timer create --idempotency-key my-reminder-key --file /dev/stdin <<'EOF'
{
  "name": "my-reminder",
  "comment": "What this reminder is for",
  "recurrence": {"frequency": "once", "date": "2026-03-19", "time": "15:10"},
  "command": {"mode": "shell", "shell": "osascript -e 'display notification \"Do the thing\" with title \"Reminder\"'"},
  "wake": {"enabled": true, "action": "wake", "leadMinutes": 2},
  "max_runs": 1
}
EOF
```

**Common mistakes:** `command` must be `{"mode": "shell", "shell": "..."}`, not a bare string.
The date and time go in `recurrence`, not in `wake`. A `once` timer with a past date is rejected.
Shell timers run under `/bin/zsh -lc`, where `status` is read-only, so `status=$(...)` fails;
use a name such as `build_status`.
See `references/timer-schemas.md` for every format.

## Timer Templates

Built-in templates, extensible with `{{WAKELITE_HOME}}/templates/*.json`:

| Template | Use |
|----------|-----|
| `reminder` | One-time notification at a date and time |
| `callback` | Run a script and send the result back to the terminal |
| `health-check` | Interval check with a notification |
| `build-monitor` | Interval poller, for example a CI build |
| `daemon` | Keep a long-lived process alive |
| `recurring` | Daily or weekly command at a fixed time |

## Timer Types at a Glance

| Type | `frequency` | Typical use |
|------|-------------|-------------|
| Scheduled | `daily`, `weekly`, `monthly`, `once` | Run at a fixed time |
| Interval | `interval` + `every: "Nm"` | Poll or monitor on a cadence |
| Interval + window | `interval` + `active_hours` | Interval anchored to a daily time window |
| Daemon | `timer_type: "daemon"`, `every: "0s"` | Keep a long-lived process alive |

**Any polling timer must use `until`** so it stops after success. See the poll-until-done
pattern in `references/timer-schemas.md`.

## launchd Environment Gotchas

The runner does not inherit your interactive shell environment.

- **Google ADC:** scripts using `google-cloud-*` fail with `DefaultCredentialsError`. Run
  `gcloud auth application-default login` once. It writes a file that launchd can read.
- **PATH:** use absolute paths to tools in timer commands, or set `PATH` inside the script.

## Reference Files

Load these on demand:

| Topic | File |
|-------|------|
| Timer JSON schemas, intervals, `active_hours`, `until`, `max_runs`, validation errors | `references/timer-schemas.md` |
| Terminal callbacks, exit-code convention, `claude -p` in timer scripts | `references/callbacks.md` |
| Install, doctor, daemon gotchas, wake-from-sleep, idempotency, post-restart checks | `references/operations.md` |

The repo itself holds the deepest documentation: `{{WAKELITE_REPO}}/README.md`,
`{{WAKELITE_REPO}}/docs/API.md`, and `{{WAKELITE_REPO}}/docs/*.timer.json` examples.
