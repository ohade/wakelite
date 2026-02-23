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
  "notifications": {"onFailure": true}
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

## Features

- **Timer types**: Scheduled (one-shot on recurrence) and Daemon (long-lived, kept alive)
- **Recurrence**: daily, weekly, monthly, one-off, interval (with active-hours windowing)
- **Wake-from-sleep**: `pmset` scheduling with configurable lead time — your Mac wakes up before the timer fires
- **Execution controls**: overlap policy (skip/queue/allow), max concurrency, restart with exponential backoff
- **Run lifecycle**: `until` conditions (auto-delete on success/failure), `max_runs` limit (auto-disable)
- **Exit code 75**: BSD `EX_TEMPFAIL` — polling scripts return "waiting" (blue rows) instead of "failed" (red rows)
- **Notifications**: macOS desktop + Slack DMs, global mute toggle
- **Resources**: capacity tracking per timer to prevent overcommit
- **Incidents**: auto-detection of repeated failures, acknowledgement via API
- **Web dashboard**: real-time status, inline editing, run-now, abort, log viewing — installable as a PWA (Chrome → "Install page as app")
- **MCP integration**: full Claude/Codex tool namespace for AI-driven scheduling
- **Idempotency**: every create/update/delete requires a caller-chosen key — if the same key is sent twice within 24 hours, the second call returns the original result instead of duplicating the action
- **Crash recovery**: missed-run catch-up, uncertain-run marking, at-least-once delivery
- **Zero dependencies**: Python stdlib only (optional: `rumps` for menu bar app)

## Docs

- **[`docs/API.md`](docs/API.md)** — Full timer schema, field reference, recurrence types, execution controls, REST/MCP API reference
- **[`.claude/rules/`](.claude/rules/)** — Claude Code rules: timer authoring gotchas, notification conventions

## Testing

```bash
PYTHONPATH=. python -m pytest tests/ -v
```

## Prerequisites

- macOS (tested on Apple Silicon)
- Python 3.9+
- Optional: `rumps` for menu bar app
