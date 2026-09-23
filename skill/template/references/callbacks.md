# Callbacks and Timer Scripts

## Terminal callbacks

A callback sends a timer's result back into the Claude Code session that created it, and
presses Enter so the session acts on it.

```json
// WezTerm
{ "callback": { "type": "wezterm", "session_id": "abc-def-123" } }

// Ghostty
{ "callback": { "type": "ghostty", "session_id": "abc-def-123" } }

// cmux
{ "callback": { "type": "cmux", "session_id": "abc-def-123", "amq": false } }
```

WakeLite auto-captures the terminal ID at creation:

| `type` | Captured field | From |
|--------|----------------|------|
| `wezterm` | `pane_id` | `$WEZTERM_PANE` |
| `ghostty` | `terminal_id` | `$GHOSTTY_TERMINAL_ID` |
| `cmux` | `workspace_id` + `surface_id` | `$CMUX_WORKSPACE_ID` + `$CMUX_SURFACE_ID` |

Rules:

- The full result goes to `~/.claude/session-signals/{session_id}.{run_id}.wakelite-callback.json`.
  The terminal only receives a short trigger line.
- `session_id` enables recovery. If the original pane is gone, WakeLite opens a new tab with
  `claude --resume <session_id>`.
- Callbacks fire on `success` and `failed` only, never on `waiting` (exit 75) or `aborted`.
- **Typo trap:** an unknown `callback.type` does not fail validation. WakeLite logs a WARNING and
  drops the callback, so the timer runs with no terminal delivery. Check
  `{{WAKELITE_HOME}}/logs/runner.log` for `Unknown callback.type`.
- **cmux and AMQ:** new cmux callbacks with a `session_id` default to `"amq": true`, which routes
  delivery through the AMQ agent-mail CLI. When AMQ is not installed WakeLite falls back to direct
  cmux injection, but set `"amq": false` explicitly to skip the attempt. Setting
  `WAKELITE_AMQ_CALLBACK_ENABLED=false` in the runner environment disables it globally.

### Typical pattern: "check the build, come back"

```json
{
  "name": "check-pr-build",
  "comment": "Poll PR build, callback when done",
  "recurrence": {"frequency": "interval", "every": "2m"},
  "command": {"mode": "shell", "shell": "/path/to/check-build.sh"},
  "until": {"on_success": "delete", "on_failure": "continue"},
  "callback": {"type": "ghostty", "session_id": "abc-def-123"}
}
```

The script exits 75 while the build runs, 0 when it passes (callback + auto-delete), and 1 when
it fails (callback, keeps polling).

---

## Exit-code convention for polling scripts

- `0` = success or done (triggers `on_success`)
- `75` = not ready yet, keep polling; shown as "Waiting" (EX_TEMPFAIL from sysexits.h)
- `1` = real failure (triggers `on_failure`)

Use exit 75 in monitors so a pending check does not show as "Failed".

---

## `claude -p` in timer scripts

1. **Choose a permission mode.** A non-interactive `claude -p` has nobody to approve tool calls.
   Pass `--allowedTools "Bash(git:*) Read Grep"` (least privilege, preferred) or
   `--permission-mode bypassPermissions` (sandboxed scripts only). Without one, the session stalls
   on the first approval and does nothing.
2. **Check which credentials the script uses.** The runner starts a login shell, so anything your
   shell profile exports, such as `ANTHROPIC_API_KEY`, is inherited. Unset it in the script if the
   run should use your subscription login instead.
3. **Prefer a plain script.** If the task is curl, an API call, formatting, or a notification, write
   bash. Use `claude -p` only when the task needs LLM reasoning.
4. **Put `claude -p` in a launcher script**, not inline in the timer JSON. It is easier to debug and
   to rerun by hand.

---

## Notifications

Prefer WakeLite's own `notifications` fields (`onFailure`, `onSuccess`). Slack lifecycle posts
(`notifications.slackActivity`) default to on; set it to `false` for local-only monitors. Slack
delivery needs the
runner configured with `WAKELITE_SLACK_CHANNEL` and a bot token in the Keychain; see the repo
README. With no channel configured, WakeLite posts nothing. Do not embed Slack tokens or direct
Slack API calls in timer scripts.
