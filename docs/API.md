# WakeLite API (v1)

## REST

Base (TCP): `http://127.0.0.1:17341`

Base (UDS): `~/.wakelite/run/api.sock`

### Health
- `GET /v1/health`

The health response includes:

- `uptime_seconds` — finite non-negative number of seconds since the current runner process created its service instance. It resets whenever the runner process restarts and uses a monotonic clock, so wall-clock adjustments do not change it.
- `unacked_incidents` — exact non-negative count of currently unacknowledged incidents; it is not limited by the paginated incident-list endpoint.

### Timers
- `GET /v1/timers`
- `GET /v1/timers/{timer_id}`
- `POST /v1/timers` (requires `idempotency_key` in JSON body)
- `PATCH /v1/timers/{timer_id}` (requires `idempotency_key`)
- `DELETE /v1/timers/{timer_id}` (requires `idempotency_key`)
- `POST /v1/timers/{timer_id}/enable` (requires `idempotency_key`)
- `POST /v1/timers/{timer_id}/disable` (requires `idempotency_key`)
- `POST /v1/timers/{timer_id}/run-now` (requires `idempotency_key`)

### Runs
- `GET /v1/runs?limit=100&timer_id=<optional>`
- `GET /v1/runs/{run_id}/logs`
- `POST /v1/runs/{run_id}/abort` (requires `idempotency_key`)

### Incidents
- `GET /v1/incidents?limit=200&include_acked=true`
- `POST /v1/incidents/{incident_id}/ack` (requires `idempotency_key`)

### Notification Settings
- `GET /v1/settings/notifications` — returns the current mute state plus the most recent mute-change audit entries
- `POST /v1/settings/notifications` — body: `{"muted": true/false, "source": "optional caller label"}`; actual state changes record UTC time and source, while idempotent writes do not create duplicate audit entries

---

## Timer Payload Reference

All fields accepted by `POST /v1/timers` and `PATCH /v1/timers/{timer_id}`.

### Top-Level Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Timer display name |
| `comment` | string | yes | — | Human-readable description of what the timer does |
| `enabled` | bool | no | `true` | Whether the timer is active |
| `timezone` | string | no | system tz | IANA timezone (e.g. `US/Eastern`, `Europe/London`) |
| `timer_type` | string | no | `"scheduled"` | `"scheduled"` or `"daemon"` |
| `recurrence` | object | yes | — | When to run (see Recurrence below) |
| `command` | object | yes | — | What to run (see Command below) |
| `wake` | object | no | — | Wake-from-sleep scheduling |
| `notifications` | object | no | — | Per-timer notification toggles |
| `execution` | object | no | — | Overlap, concurrency, restart controls |
| `resources` | list | no | `[]` | Resource capacity requirements |
| `max_runs` | int/null | no | `null` | Auto-disable after N total runs |
| `until` | object/null | no | `null` | Auto-delete on success/failure outcome |
| `callback` | object/null | no | `null` | Reconnect results to originating terminal |

### `command`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mode` | string | yes | `"shell"` or `"exec"` |
| `shell` | string | if mode=shell | Shell command string |
| `executable` | string | if mode=exec | Path to executable |
| `args` | list | no | Arguments for exec mode |

### `recurrence`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `frequency` | string | yes | `"daily"`, `"weekly"`, `"monthly"`, `"once"`, `"interval"` |
| `time` | string | daily/weekly/monthly | `"HH:MM"` clock time |
| `interval` | int | no | Repeat every N periods (default: 1) |
| `weekly_days` | list | weekly | Day names: `["Mon","Wed","Fri"]` |
| `monthly_mode` | string | monthly | `"day_of_month"` or `"nth_weekday"` |
| `day_of_month` | int | if day_of_month | 1–31 (clamped to last day of month) |
| `nth` | int | if nth_weekday | 1–4 or -1 (last) |
| `weekday` | string | if nth_weekday | Day name (e.g. `"Sun"`) |
| `date` | string | once | `"YYYY-MM-DD"` target date |
| `every` | string | interval | Duration: `"10s"`, `"5m"`, `"2h"` (min 10s; `"0s"` for daemons only) |
| `active_hours` | object | no | Interval windowing (see below) |

#### `recurrence.active_hours` (interval frequency only)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `start` | string | yes | `"HH:MM"` window start |
| `end` | string | yes | `"HH:MM"` window end (must be after start) |

Fires are anchored to `start` and step by `every`. Outside the window, the timer does not fire.

#### Recurrence Examples

**Daily at 9am:**
```json
{"frequency": "daily", "time": "09:00", "interval": 1}
```

**Every other week on Monday and Thursday:**
```json
{"frequency": "weekly", "time": "08:00", "interval": 2, "weekly_days": ["Mon", "Thu"]}
```

**Monthly on the 15th:**
```json
{"frequency": "monthly", "time": "09:00", "interval": 1, "monthly_mode": "day_of_month", "day_of_month": 15}
```

**Last Sunday of every month:**
```json
{"frequency": "monthly", "time": "09:00", "interval": 1, "monthly_mode": "nth_weekday", "nth": -1, "weekday": "Sun"}
```

**One-off:**
```json
{"frequency": "once", "date": "2026-03-15", "time": "14:00"}
```

**Every 5 minutes between 7am and 10:30pm:**
```json
{"frequency": "interval", "every": "5m", "active_hours": {"start": "07:00", "end": "22:30"}}
```

**Every 30 seconds (no window):**
```json
{"frequency": "interval", "every": "30s"}
```

### `wake`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Schedule macOS wake-from-sleep before the timer fires |
| `action` | string | — | `pmset` action type |
| `leadMinutes` | int | — | Minutes before timer to schedule the wake event |

### `notifications`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `onSuccess` | bool | `false` | Notify on successful run (exit code 0) |
| `onFailure` | bool | `true` | Notify on failed run (non-zero exit, excluding 75) |
| `slackActivity` | bool | `true` | Post the Slack run-start/run-end thread and auto-delete message. Set `false` for local-only monitors. |

`onSuccess` and `onFailure` control macOS desktop notifications (`osascript`) and are gated by the global mute toggle. Slack activity is enabled by default for backward compatibility, including for existing timers that omit `slackActivity`. Setting it to `false` suppresses Slack lifecycle writes for successful, failed, waiting, and auto-deleted runs. Terminal callback fallback notifications are a separate delivery path.

### `execution`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `overlap` | string | `"skip"` | `"skip"` (drop), `"queue"` (run after current), `"allow"` (run in parallel) |
| `max_concurrent` | int | `1` | Max parallel runs (applies when `overlap: "allow"`) |
| `restart_on_failure` | bool | `false` | Auto-restart on non-zero exit |
| `restart_delay_seconds` | int | `5` | Initial delay before restart |
| `restart_max_backoff_seconds` | int | `300` | Maximum backoff delay (exponential) |

### `resources`

A list of resource requirements. Each item:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Resource identifier |
| `description` | string | no | Human-readable description |
| `capacity` | int | no | Total available capacity |
| `estimated_usage` | int | no | How much this timer consumes per run |

### `until`

Both fields are required when `until` is present:

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `on_success` | string | `"delete"`, `"continue"` | Action when run exits 0 |
| `on_failure` | string | `"delete"`, `"continue"` | Action when run exits non-zero (excluding 75) |

When `"delete"` is triggered, the timer is automatically removed.

### `callback`

Optional. Reconnects timer results to the originating terminal session.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `type` | string | yes | — | Callback mechanism: `"wezterm"`, `"ghostty"`, or `"cmux"` |
| `pane_id` | int | no | from `$WEZTERM_PANE` | WezTerm pane to inject results into. Auto-captured at creation if env var is set |
| `terminal_id` | string | no | from `$GHOSTTY_TERMINAL_ID` | Ghostty terminal to inject results into. Auto-captured at creation if env var is set |
| `workspace_id` | string | for cmux | from `$CMUX_WORKSPACE_ID` | cmux workspace ID |
| `surface_id` | string | for cmux | from `$CMUX_SURFACE_ID` or `$CMUX_PANEL_ID` | cmux surface ID. `panel_id` is accepted as an input alias and normalized to `surface_id` during validation |
| `socket_path` | string | no | from `$CMUX_SOCKET_PATH` | Optional cmux socket path passed to callback subprocesses |
| `cli_path` | string | no | detected cmux CLI | Optional cmux CLI path. Auto-capture checks `$CMUX_BUNDLED_CLI_PATH`, `/opt/homebrew/bin/cmux`, `/usr/local/bin/cmux`, then `/Applications/cmux.app/Contents/Resources/bin/cmux` |
| `session_id` | string | no | `null` | Claude Code session ID for `--resume` fallback when pane is gone |
| `amq` | bool | no | `true` for newly created cmux callbacks with a non-empty `session_id`; otherwise `false` | cmux only. Prefer AMQ delivery unless the target is known dead; alive and unknown targets may route through AMQ, with mailbox identity derived at fire time from `session_id`. Set `false` to opt out explicitly |

**Behavior:**
- Callback fires on `success` and `failed` status only. Never on `waiting` (exit 75) or `aborted`.
- Terminal identity is auto-captured when creating via CLI, MCP, or REST API if the server process has the relevant environment variables. Detection order is cmux, Ghostty, then WezTerm.
- Creation applies the AMQ default only to new resumable cmux callbacks. Clone and from-template are creation operations and therefore apply the same default. Explicit `"amq": false` is preserved, updates do not silently opt in an existing timer, and already-stored legacy timers remain unchanged.
- Callback objects reject unknown subkeys. A misspelling such as `"ammq": true` fails validation instead of silently disabling the route.
- For a cmux callback with `"amq": true`, WakeLite writes the recovery signal first, resolves the current surface and its AMQ wake registration from `session_id`, and self-delivers the full callback payload as the AMQ message body. Alive and unknown targets route through AMQ; only a canonical stale-target result proves the target dead. An AMQ send that exits successfully with a non-empty message ID proves that AMQ stored the body: WakeLite then deletes the local signal file and deliberately surrenders the cmux fallback. A dead target, missing identity, rejected/malformed send, or send timeout uses the existing cmux fallback. `WAKELITE_AMQ_CALLBACK_ENABLED=false` disables this route globally.
- The cmux callback path records a terminal outcome: `amq-sent`, `fallback-delivered`, or `delivery-failed`. Failed delivery keeps the signal file for recovery.
- AMQ-to-fallback delivery is at-least-once: if `amq send` exceeds the 5-second process timeout after storing the body, WakeLite falls back and the stored message may still be drained later, exposing the same `run_id` twice.
- **Terminal path:** WezTerm and Ghostty callbacks inject into their captured terminal. cmux callbacks use AMQ when requested and resolvable, otherwise inject directly into the captured surface.
- **Fallback:** A failed direct terminal callback sends a Slack notification and may resume `session_id` in a new terminal where that callback implementation supports it. A failed cmux AMQ route keeps the recovery signal and attempts the direct cmux fallback.

```json
{
  "callback": {
    "type": "wezterm",
    "pane_id": 11,
    "session_id": "abc-def-123"
  }
}
```

cmux example:

```json
{
  "callback": {
    "type": "cmux",
    "workspace_id": "workspace-uuid",
    "surface_id": "surface-uuid",
    "socket_path": "/path/to/cmux.sock",
    "cli_path": "/opt/homebrew/bin/cmux",
    "session_id": "abc-def-123",
    "amq": true
  }
}
```

`panel_id` is accepted for cmux compatibility with older callers:

```json
{
  "callback": {
    "type": "cmux",
    "workspace_id": "workspace-uuid",
    "panel_id": "surface-uuid"
  }
}
```

WakeLite stores this as `surface_id`; downstream service code does not use `panel_id`.

**Injected message format:**

```
[WakeLite callback] Timer "check-build" completed
Status: success | Exit code: 0 | Duration: 2m 15s
Stdout (last 50 lines):
─────────────────────────
<truncated stdout from run log file>
─────────────────────────
This timer was created during your session. Act on the results above.
```

**Auto-capture summary:**

| Creation path | Has terminal env? | Auto-capture works? |
|--------------|-------------------|-------------------|
| CLI from Claude Code terminal | Yes | Yes |
| MCP from Claude Code | Yes (inherited) | Yes |
| REST API from external tool | No | No (pass explicitly) |

### `max_runs`

Positive integer or `null`. When the timer reaches N total runs, it is automatically disabled (not deleted). When combined with `until`, the first limit reached wins.

### Timer Type: Daemon

Set `timer_type: "daemon"` for long-lived processes. Daemons use `"every": "0s"` recurrence (zero-second interval, only valid for daemon type). Combine with `execution.restart_on_failure` for automatic restarts with backoff:

```json
{
  "timer_type": "daemon",
  "recurrence": {"frequency": "interval", "every": "0s"},
  "execution": {
    "restart_on_failure": true,
    "restart_delay_seconds": 5,
    "restart_max_backoff_seconds": 300
  }
}
```

### Exit Code 75 — "Waiting"

Exit code 75 (`EX_TEMPFAIL`) receives special treatment:

- Dashboard shows blue "Waiting" row instead of red "Failed"
- No failure notification sent
- No incident created
- `until` conditions not triggered (neither `on_success` nor `on_failure`)

Use this in polling scripts to signal "condition not met yet, keep trying."

---

## Run Response Fields

`POST /v1/timers/{timer_id}/run-now` response includes:
- `queued` (boolean; `false` means a duplicate/previously queued occurrence was not enqueued again)

`GET /v1/runs` returns derived fields per run:
- `timer_name`
- `logs_url`
- `has_logs`

`GET /v1/runs/{run_id}/logs` returns:
- `stdout_available`
- `stderr_available`
- `logs_expired` (true when files are gone and retention has elapsed)

---

## MCP

Namespace: `wakelite.v1.*`

Transports:
- stdio: `bin/wakelite-mcp`
- HTTP JSON-RPC: `POST http://127.0.0.1:17342/mcp`

Tools:
- `wakelite.v1.health.get`
- `wakelite.v1.timer.list`
- `wakelite.v1.timer.create`
- `wakelite.v1.timer.update`
- `wakelite.v1.timer.delete`
- `wakelite.v1.timer.enable`
- `wakelite.v1.timer.disable`
- `wakelite.v1.timer.run_now`
- `wakelite.v1.run.list`
- `wakelite.v1.run.logs.get`
- `wakelite.v1.run.abort`
- `wakelite.v1.alert.ack`

Mutating tools require `idempotency_key`.

Fail-fast behavior:
- If runner/API is down, tool calls return `SERVICE_UNAVAILABLE` and do not auto-start services.

---

## Retention

- Run metadata: 30 days (in SQLite)
- Run stdout/stderr files: 7 days (pruned hourly by runner)
- Idempotency keys: 24 hours
