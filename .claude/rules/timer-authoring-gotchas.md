# Timer Authoring Gotchas

Hard-won lessons from building and debugging timers. Read before creating timers programmatically.

## Schema traps

- **Top-level key is `recurrence`, NOT `schedule`.** Using `"schedule"` is silently ignored — timer gets empty recurrence and null `next_run`. Validation now catches this with a hint, but older versions didn't.
- **Once-timers use separate `date` + `time` fields**, not a combined `at` field: `{"frequency": "once", "date": "2026-03-15", "time": "14:00"}`
- **`wake` and `leadMinutes` are top-level**, not nested inside `recurrence`.
- **`comment` is required.** Validation rejects timers without it.
- **Unknown top-level keys are rejected.** See `ALLOWED_PAYLOAD_KEYS` in `timer_store.py`. Adding `description`, `interval_seconds`, etc. causes an error.
- **`command.shell` IS the command string, NOT the interpreter.** WakeLite executes `zsh -lc "<command.shell>"`. Writing `"shell": "/bin/bash", "run": "my-script arg1"` runs `/bin/bash` with no args (instant exit 0). The `"run"` field does not exist in the schema. Correct: `"command": {"mode": "shell", "shell": "my-script arg1"}`.

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

## Callback

- **`callback` is optional top-level field.** Reconnects timer results to the originating WezTerm terminal session.
- **`pane_id` is auto-captured** from `$WEZTERM_PANE` if not explicitly provided (CLI, MCP, REST API).
- **Callback only fires on `success`/`failed`.** Never on `waiting` (exit 75) or `aborted`.
- **Results are auto-submitted.** The message is pasted into the pane AND Enter is pressed, so Claude Code receives it as a prompt.
- **Fallback when pane is gone:** Slack DM + new WezTerm tab with `claude --resume <session_id>` (if `session_id` is set).
- **Supported types:** Only `"wezterm"` in v1. Extensible later to `"shell"`, `"webhook"`.
- **`--no-paste` with `\r` is critical for the Enter keypress.** `wezterm cli send-text` uses bracketed paste by default — pasted text does NOT trigger Enter in TUI apps like Claude Code. The submit must use `--no-paste` with `\r` (carriage return, 0x0d), NOT `\n` (line feed, 0x0a). Terminals send `\r` for Enter — `\n` gets silently dropped or creates a literal newline in the input. The message body itself should use default (paste) mode so embedded newlines render as text, not keypresses.
- **Best paired with `until`:** Use `"until": {"on_success": "delete", "on_failure": "continue"}` for poll-until-done patterns (build monitoring, deploy checks).

## CLI gotchas

- **Delete requires timer UUID, not name.** Look up ID from `timer list` output first.
- **Different idempotency keys = different operations.** Using a new key with the same timer name creates a duplicate. Reuse the key to get idempotent behavior, or delete before recreating.
- **`timer update` supports inline flags** (`--name`, `--comment`, `--shell`, `--enabled`) for quick edits without a JSON file.
