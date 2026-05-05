# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Run all tests (pytest preferred over unittest discover)
PYTHONPATH=. python -m pytest tests/ -v

# Run a single test class or method
PYTHONPATH=. python -m pytest tests/test_service.py::UntilConditionTests -v
PYTHONPATH=. python -m pytest tests/test_service.py::UntilConditionTests::test_until_success_deletes_on_success -v

# Run recurrence tests only
PYTHONPATH=. python -m pytest tests/test_recurrence.py -v

# CLI commands (always need PYTHONPATH)
PYTHONPATH=. ./bin/wakelitectl health
PYTHONPATH=. ./bin/wakelitectl timer list
PYTHONPATH=. ./bin/wakelitectl launchd restart    # restart runner after code changes (purges __pycache__)
```

No dependencies to install — zero external packages for runtime (stdlib only). Optional: `rumps` for menubar app.

## Architecture

WakeLite is a local macOS scheduler with four independently running components:

```
┌─────────────────────────────────────────────────────────┐
│ runner (bin/wakelite-runner)                             │
│   WakeLiteService  ──  scheduler loop (threading)       │
│   ApiServer        ──  REST API on :17341 + Unix socket │
│   MCP HTTP         ──  JSON-RPC on :17342 (optional)    │
└─────────────────────────────────────────────────────────┘
  bin/wakelitectl ── CLI client (talks to REST API)
  bin/wakelite-mcp ── MCP stdio proxy (talks to REST API)
  bin/wakelite-reconciler ── system-level pmset wake scheduling (runs as root)
```

The **runner** is the single source of truth. Everything else is a client that talks to its REST API.

### Core modules (dependency order)

| Module | Role |
|--------|------|
| `config.py` | Paths, ports, constants. `WAKELITE_HOME` env overrides `~/.wakelite` |
| `utils.py` | `atomic_write_json`, ISO parsing |
| `recurrence.py` | Pure functions: parse recurrence specs, compute next occurrences. No I/O |
| `timer_store.py` | Timer CRUD on `~/.wakelite/timers.json`. Validates schema, thread-safe |
| `state.py` | SQLite (`~/.wakelite/state.db`) — run history, runtime state, daemon state, incidents |
| `notifier.py` | macOS notifications (`osascript`) + Slack DMs (`notify_slack()`) |
| `capacity.py` | Time-axis per-resource admission gate. Projects each timer's resource use onto N-minute buckets over a 7-day horizon, blocks create/update when any resource's peak > capacity. Pure functions, no I/O. `_executor.slot` is a well-known resource (capacity=MAX_WORKERS); user-declared `resources[]` participate when `capacity`/`estimated_usage` parse |
| `service.py` | **Core orchestrator.** Scheduler loop, run execution (ThreadPoolExecutor), timer lifecycle |
| `http_api.py` | REST API handler + embedded web UI (single-file HTML/CSS/JS in Python string) |
| `mcp_server.py` | MCP protocol bridge — translates MCP tool calls to REST API calls |
| `reconciler.py` | Reads timer wake intents, reconciles with `pmset schedule` entries |

### Data storage

- **Timers**: `~/.wakelite/timers.json` (JSON file, atomic writes)
- **State**: `~/.wakelite/state.db` (SQLite WAL mode — run_history, occurrences, daemon_state, incidents, meta)
- **Run logs**: `~/.wakelite/logs/{timer_id}/{date}/{run_id}.{out,err}.log` (7-day retention)
- **Runner log**: `~/.wakelite/logs/runner.log`

### Timer types

- **Scheduled** (`timer_type: "scheduled"`, default): one-shot commands on a recurrence (daily/weekly/monthly/once/interval)
- **Daemon** (`timer_type: "daemon"`): long-lived process kept alive with restart/backoff. Uses `"every": "0s"` recurrence

### Run lifecycle in service.py

```
_tick() → _schedule_occurrence() → _spawn_run() → _run_occurrence() [in thread pool]
  └→ subprocess.Popen (shell command)
  └→ finish_run() → until check → max_runs check → notifications → queue drain
```

Key invariant: `_run_occurrence()` runs in the ThreadPoolExecutor. It holds no scheduler lock during command execution.

### Idempotency

All mutating operations (create, update, delete, enable, disable, run-now, abort) require an `idempotency_key`. Keys are stored in `state.db` with 24-hour TTL. Duplicate keys return the original result.

## Testing patterns

Tests in `tests/test_service.py` use a `_bootstrap(temp_dir)` helper that redirects `WAKELITE_HOME` to a temp directory and reloads all modules. This gives each test a fresh timer store and state DB.

Timer execution tests call `svc._schedule_occurrence()` directly and `time.sleep()` to wait for the thread pool. Typical wait is 1.5s for simple commands.

The MCP test (`test_mcp_and_manifest.py`) is flaky — it tests that MCP fails fast when the runner is down, but passes when the runner is actually running.

## Web UI

The entire web UI is an HTML string embedded in `http_api.py` (served at `/ui`). There are no separate HTML/JS/CSS files. Edits to the UI mean editing the Python string in `http_api.py`.

## Deployment

The runner runs as a launchd user agent (`com.wakelite.runner`). After code changes:

1. `PYTHONPATH=. ./bin/wakelitectl launchd restart` — purges `__pycache__` and kickstarts the service
2. The plist sets `PYTHONDONTWRITEBYTECODE=1` to prevent stale bytecode

The reconciler runs as a system-level launchd daemon (requires `sudo` to install).

## Monitor script conventions

Monitor/polling scripts MUST produce stdout describing what they're doing. Silent scripts make debugging impossible — even when exit codes are correct, empty logs give zero visibility into what happened.

1. **Echo what you're checking**: `echo "Checking PR-98729 build #9 status..."`
2. **Echo the result**: `echo "Build status: $STATUS"`
3. **Echo exit reason before exit 75**: `echo "Not ready yet — status=$STATUS"`
4. **Exit codes**: `0` = done/success, `75` = not ready (waiting), `1` = error

Example:
```bash
#!/bin/bash
echo "Checking PR-98729 build #9..."
STATUS=$(jk get-build-status 98729 9)
echo "Build status: $STATUS"
if [ "$STATUS" = "SUCCESS" ]; then
    echo "Build completed successfully"
    exit 0
fi
echo "Build still running — will check again"
exit 75
```

Note: Even if a script produces no output, the web UI will show run context (command, exit code, timestamps) — but explicit logging is always preferred.

## Capacity gate (WL-12)

`service.check_capacity()` gates timer create/update via `capacity.py`, which
projects each timer's resource use onto a time axis and blocks when any
declared resource's peak concurrent use would exceed its capacity.

**Semantic change from the pre-WL-12 gate:** the old implementation summed
`_estimate_slots()` across every timer regardless of when they run, so a
daily 06:00 backup and a once-timer scheduled for 2026-05-04 09:00 competed
for the same slot budget even though they never coexist at runtime. The new
gate is strictly more permissive in that common case and strictly correct
when two timers actually overlap in time. HTTP contract is preserved:
capacity violations still return 409 with `code: CAPACITY_EXCEEDED`.

**Resources.** Every timer implicitly consumes `_executor.slot` (capacity
= `MAX_WORKERS` = 16). Daemons count as exactly 1 slot regardless of
`execution.overlap`/`max_concurrent` — the daemon runtime keeps a single
live process. Non-daemon timers with `overlap="allow"` consume
`max_concurrent` slots. User-declared `resources[]` (e.g. `slack-api`,
`ollama-gpu`) participate when their `capacity`/`estimated_usage` strings
parse (`"50 req/min"`, `"12 req/sec"`, `"1 req/h"`, bare integers). When
multiple timers declare the same resource with different capacities, the
MIN is used — deterministic and conservative.

**Interval phasing.** `service.check_capacity` enriches existing interval
timers with `_last_fired_at` from `state.db` meta before calling the
engine, so the projection uses the real scheduler phase rather than
collapsing every interval timer to `now`. Brand-new intervals without
history are projected from `now` (conservative).

**Env knobs.** All optional, positive integers (invalid values silently
fall back to defaults):

| Env var | Default | Effect |
|---|---|---|
| `WAKELITE_CAPACITY_ENABLED` | `true` | `false`/`0`/`no`/`off`/`disabled` bypasses the gate — admission-control rollback lever without code revert. |
| `WAKELITE_CAPACITY_HORIZON_DAYS` | `7` | How far forward the projection looks. |
| `WAKELITE_CAPACITY_BUCKET_SECONDS` | `60` | Bucket granularity. Smaller = tighter collision detection, larger = cheaper. |
| `WAKELITE_CAPACITY_RUN_DURATION_SECONDS` | `60` | Assumed duration of one run, used to decide which buckets a fire occupies. |

Bucket arithmetic uses `.timestamp()` on datetimes so DST transitions
don't skew the 7-day axis.

## Key gotchas

- **Interval format is single-unit only.** `5h2m` is rejected — use `302m` instead.
- **`scripts/` is gitignored.** Timer scripts live there but aren't tracked.
- **Timer JSON validation is strict.** Unknown top-level keys are rejected. See `ALLOWED_PAYLOAD_KEYS` in `timer_store.py`.
- **`until` deletes timers; `max_runs` disables them.** First limit reached wins when both are set.
- **Slack token** for `notifier.notify_slack()` is read from `~/.claude.json` → `mcpServers.slack.env.SLACK_BOT_TOKEN`.
- **Exit code 75 = "waiting/not ready yet".** Polling scripts should `exit 75` (not `exit 1`) when the condition isn't met yet. WakeLite shows these as blue "Waiting" rows instead of red "Failed". Exit 75 does not trigger failure notifications, incidents, or until conditions.
