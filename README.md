# WakeLite

A reliability-first local scheduler for macOS that wakes your Mac from sleep to run your tasks. Web dashboard, CLI, and MCP integration for Claude/Codex. Zero dependencies.

![WakeLite hero — logo and add timer form](docs/screenshots/wakelite-hero.png)

![WakeLite dashboard — timers and recent runs](docs/screenshots/wakelite-dashboard-v3.png)

## What It Does

- **Schedules commands** on daily, weekly, monthly, interval, or one-off recurrences
- **Wakes your Mac from sleep** before execution via `pmset` — your 2am scripts actually run at 2am
- **Keeps daemons alive** with restart policies and exponential backoff
- **Tracks everything** — run history, stdout/stderr logs, incidents, resource usage
- **Notifies you** via macOS desktop and Slack DMs on success or failure

## Quick Start

```bash
git clone <repo-url> && cd wakelite

# Start the runner (REST API + scheduler + web dashboard)
PYTHONPATH=. ./bin/wakelitectl serve --with-mcp-http

# Open the dashboard
open http://127.0.0.1:17341/ui
```

## Create a Timer (CLI)

```bash
PYTHONPATH=. ./bin/wakelitectl timer create --file - --idempotency-key my-first-timer <<'EOF'
{
  "name": "morning-check",
  "comment": "Run health check every morning",
  "recurrence": {"frequency": "daily", "time": "09:00"},
  "command": {"mode": "shell", "shell": "curl -sf https://example.com/health"},
  "wake": {"enabled": true, "leadMinutes": 2},
  "notifications": {"onFailure": true, "slackActivity": true}
}
EOF
```

## Create a Timer (Web UI)

Or just use the dashboard — fill in the "Add Wakeup" form at the top and click **Create Wakeup**. No JSON required.

## MCP Integration

Give Claude or Codex full control over your timers:

```bash
PYTHONPATH=. ./bin/wakelitectl mcp install --targets claude,codex
```

Namespace: `wakelite.v1.*` — see [`docs/API.md`](docs/API.md) for the full tool list.

## Diagnose (`doctor`)

```bash
PYTHONPATH=. ./bin/wakelitectl doctor          # heartbeat, daemons, ports, failing timers, incidents
PYTHONPATH=. ./bin/wakelitectl doctor --json   # same report, machine-readable
PYTHONPATH=. ./bin/wakelitectl doctor --fix    # reclaim orphans; kickstart a stale runner
PYTHONPATH=. ./bin/wakelitectl doctor --quiet  # silent when healthy — safe to run on a schedule
```

Unlike every other subcommand, `doctor` falls back to reading `~/.wakelite/state.db`
directly when the REST API does not answer, because a hung runner is exactly the
case where the API stops answering. Exit code is `0` when healthy, `1` when the
report lists problems.

`--fix` does exactly two things and records an incident for each: reclaim orphaned
daemon children, and `launchctl kickstart -k` the runner when the heartbeat is stale.
The kick is capped at one per 30 minutes; after two kicks in two hours fail to
restore the heartbeat it raises one critical incident and stops kicking.

To have `doctor` check a daemon's port, declare it as a resource named
`port:17382`, or name the resource anything containing "port" and put the number
in its description.

## Features

- **Timer types**: Scheduled (one-shot on recurrence) and Daemon (long-lived, kept alive)
- **Recurrence**: daily, weekly, monthly, one-off, interval (with active-hours windowing)
- **Wake-from-sleep**: `pmset` scheduling with configurable lead time — your Mac wakes up before the timer fires
- **Execution controls**: overlap policy (skip/queue/allow), max concurrency, restart with exponential backoff
- **Run lifecycle**: `until` conditions (auto-delete on success/failure), `max_runs` limit (auto-disable)
- **Exit code 75**: BSD `EX_TEMPFAIL` — polling scripts return "waiting" (blue rows) instead of "failed" (red rows)
- **Notifications**: macOS desktop + Slack DMs, global mute toggle
- **Resources**: capacity tracking per timer to prevent overcommit
- **Clickable notifications**: desktop alerts open the timer or report they are about,
  posted by the small `notifier-app/` bundle (build with `notifier-app/build-app.sh`)
- **Incidents**: auto-detection of repeated failures, with a dashboard report at
  `/ui#incidents` — breakdown by type and timer, daily trend, per-incident resolve,
  filtered bulk resolve, and ignore rules for known-noisy sources
- **Web dashboard**: real-time status, inline editing, run-now, abort, log viewing — installable as a PWA (Chrome → "Install page as app")
- **MCP integration**: full Claude/Codex tool namespace for AI-driven scheduling
- **Idempotency**: every create/update/delete requires a caller-chosen key — if the same key is sent twice within 24 hours, the second call returns the original result instead of duplicating the action
- **Crash recovery**: missed-run catch-up, uncertain-run marking, at-least-once delivery
- **`doctor`**: one read-only command for runner heartbeat, daemon liveness and
  parentage, port ownership, failure streaks and open incidents — reads the state
  database directly so it still answers when the runner is hung
- **Zero dependencies**: Python stdlib only (optional: `rumps` for menu bar app)

## Installation

### Runner (user agent)

The runner handles scheduling, command execution, and the web dashboard. Install it as a launchd user agent:

```bash
PYTHONPATH=. ./bin/wakelitectl launchd install --load
```

### Wake Reconciler (system daemon)

For **wake-from-sleep** support, the reconciler syncs timer wake intents to `pmset schedule`. It runs as root so it can program hardware wakes.

```bash
# Install and start the system daemon (requires sudo)
PYTHONPATH=. ./bin/wakelitectl launchd install-system --load
```

The reconciler resolves the owning user's `WAKELITE_HOME` from the script file's owner — no hardcoded paths. It reads `~/.wakelite/wake-intents.json` (written by the runner) and reconciles with `pmset schedule` entries every 10 minutes.

**Verify it's working:**

```bash
pmset -g sched  # Should show wake entries by 'com.wakelite'
```

## Docs

- **[`docs/API.md`](docs/API.md)** — Full timer schema, field reference, recurrence types, execution controls, REST/MCP API reference
- **[`.claude/rules/`](.claude/rules/)** — Claude Code rules: timer authoring gotchas, notification conventions

## Testing

The full suite requires Python 3.9+ and Node.js 18+; Node executes the dashboard's
inline JavaScript contract tests.

```bash
PYTHONPATH=. python -m pytest tests/ -v
```

## Prerequisites

- macOS (tested on Apple Silicon)
- Python 3.9+
- Node.js 18+ (for the full test suite)
- Optional: `rumps` for menu bar app

## License

MIT. See [LICENSE](LICENSE).
