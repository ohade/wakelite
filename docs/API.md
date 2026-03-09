# WakeLite API (v1)

## REST

Base (TCP): `http://127.0.0.1:17341`

Base (UDS): `~/.wakelite/run/api.sock`

### Health
- `GET /v1/health`

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
- `GET /v1/settings/notifications` — returns `{"notifications_muted": bool}`
- `POST /v1/settings/notifications` — body: `{"muted": true/false}`

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

Notifications use macOS desktop (`osascript`) and optionally Slack DMs. Desktop notifications are gated by the global mute toggle; Slack is always-on.

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
| `type` | string | yes | — | Callback mechanism. v1: `"wezterm"` only |
| `pane_id` | int | no | from `$WEZTERM_PANE` | WezTerm pane to inject results into. Auto-captured at creation if env var is set |
| `session_id` | string | no | `null` | Claude Code session ID for `--resume` fallback when pane is gone |

**Behavior:**
- Callback fires on `success` and `failed` status only. Never on `waiting` (exit 75) or `aborted`.
- `pane_id` is auto-captured from `$WEZTERM_PANE` when creating via CLI, MCP, or REST API (if the server process has the env var).
- **Happy path:** If the pane exists, results are injected via `wezterm cli send-text` and auto-submitted (Enter pressed). Claude Code receives it as a new prompt.
- **Fallback:** If the pane is gone, a Slack DM is sent. If `session_id` is set, a new WezTerm tab opens with `claude --resume <session_id>` and results are injected there.

```json
{
  "callback": {
    "type": "wezterm",
    "pane_id": 11,
    "session_id": "abc-def-123"
  }
}
```

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

| Creation path | Has `$WEZTERM_PANE`? | Auto-capture works? |
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
